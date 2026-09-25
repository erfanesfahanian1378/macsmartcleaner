"""Things that start automatically: login items, launch agents and daemons.

Sources:
  login items          System Settings > General > Login Items ("Open at Login")
  ~/Library/LaunchAgents     your user's background helpers
  /Library/LaunchAgents      helpers every user gets (installed by apps)
  /Library/LaunchDaemons     system-level background services (need admin to change)

Apple's own services live in /System and are never listed or touched.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .context import Context

KNOWN_UPDATERS = ("keystone", "autoupdate", "updater", "update", "adobe.agsservice", "adobegcclient",
                  "googlesoftwareupdate", "microsoft.update", "sparkle")

LOGIN_ITEMS_SCRIPT = '''
tell application "System Events"
  set out to ""
  repeat with li in login items
    set out to out & (name of li) & tab & (path of li) & linefeed
  end repeat
end tell
return out
'''


@dataclass
class StartupItem:
    kind: str                 # login-item | agent | global-agent | daemon
    label: str
    name: str
    vendor: str
    path: str                 # plist path, or the app path for login items
    program: Optional[str] = None
    enabled: Optional[bool] = True
    running: Optional[bool] = None
    broken: bool = False
    run_at_load: bool = False
    keep_alive: bool = False

    @property
    def needs_root(self) -> bool:
        return self.kind == "daemon"

    @property
    def kind_label(self) -> str:
        return {"login-item": "login item", "agent": "your agent", "global-agent": "all users",
                "daemon": "system daemon"}[self.kind]

    @property
    def advice(self) -> str:
        if self.broken:
            return "Leftover: its app is gone. Safe to remove."
        low = (self.label + " " + (self.program or "")).lower()
        if any(k in low for k in KNOWN_UPDATERS):
            return "Auto-updater. Optional: the app can still update itself when you open it."
        if self.kind == "daemon":
            return "System-level service. Keep it if you use the app that installed it (VPN, drivers, security)."
        if self.kind == "login-item":
            return "Opens when you log in. Remove it if you'd rather open the app yourself."
        return "Background helper for an app. Disable it if you don't use that app often."


def _friendly(label: str, program: Optional[str]) -> Tuple[str, str]:
    """(name, vendor) from a launchd label and program path."""
    if program:
        m = re.search(r"/([^/]+)\.app/", program + "/")
        if m:
            name = m.group(1)
            parts = label.split(".")
            vendor = parts[1].capitalize() if len(parts) > 2 and parts[0] in ("com", "org", "net", "io") else ""
            return name, vendor
    parts = label.split(".")
    if len(parts) >= 3 and parts[0] in ("com", "org", "net", "io", "de", "us", "co"):
        vendor = parts[1].capitalize()
        rest = " ".join(p for p in parts[2:] if p.lower() not in ("agent", "plist", "launcher", "helper"))
        return (f"{vendor} {rest}".strip() if rest else vendor), vendor
    return label, ""


def _program(plist: dict) -> Optional[str]:
    if isinstance(plist.get("Program"), str):
        return plist["Program"]
    args = plist.get("ProgramArguments")
    if isinstance(args, list) and args and isinstance(args[0], str):
        return args[0]
    return None


def _parse_disabled(text: str) -> Dict[str, bool]:
    """`launchctl print-disabled` -> {label: is_disabled}."""
    out = {}
    for m in re.finditer(r'"([^"]+)"\s*=>\s*(\w+)', text):
        out[m.group(1)] = m.group(2).lower() in ("disabled", "true")
    return out


def _parse_launchctl_list(text: str) -> Dict[str, bool]:
    """`launchctl list` -> {label: running}."""
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[2].strip()] = parts[0].strip() not in ("-", "")
    return out


def scan_launchd(ctx: Context) -> List[StartupItem]:
    uid = ctx.uid
    res = ctx.run(["launchctl", "print-disabled", f"gui/{uid}"], timeout=10, as_user=True)
    disabled_user = _parse_disabled(res.stdout) if res is not None else {}
    res = ctx.run(["launchctl", "print-disabled", "system"], timeout=10, as_user=False)
    disabled_sys = _parse_disabled(res.stdout) if res is not None else {}
    res = ctx.run(["launchctl", "list"], timeout=10, as_user=True)
    running = _parse_launchctl_list(res.stdout) if res is not None else {}

    items: List[StartupItem] = []
    for folder, kind in (("~/Library/LaunchAgents", "agent"), ("/Library/LaunchAgents", "global-agent"),
                         ("/Library/LaunchDaemons", "daemon")):
        base = ctx.path(folder)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".plist"):
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, "rb") as fh:
                    data = plistlib.load(fh)
            except Exception:  # noqa: BLE001 - unreadable/corrupt plist: still show it
                data = {}
            label = str(data.get("Label") or fn[:-6])
            program = _program(data)
            name, vendor = _friendly(label, program)
            broken = bool(program and program.startswith("/") and not os.path.exists(ctx.path(program)))
            disabled_map = disabled_sys if kind == "daemon" else disabled_user
            enabled = not disabled_map.get(label, bool(data.get("Disabled", False)))
            items.append(StartupItem(
                kind=kind, label=label, name=name, vendor=vendor, path=path, program=program,
                enabled=enabled, running=running.get(label) if kind != "daemon" else None, broken=broken,
                run_at_load=bool(data.get("RunAtLoad")), keep_alive=bool(data.get("KeepAlive")),
            ))
    return items


def scan_login_items(ctx: Context) -> List[StartupItem]:
    res = ctx.run(["osascript", "-e", LOGIN_ITEMS_SCRIPT], timeout=15, as_user=True)
    if res is None or res.returncode != 0:
        return []
    items = []
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        name, path = line.split("\t", 1)
        items.append(StartupItem(kind="login-item", label=name, name=name, vendor="", path=path.strip(),
                                 program=path.strip(), enabled=True, running=None,
                                 broken=bool(path.strip()) and not os.path.exists(path.strip())))
    return items


def scan(ctx: Context) -> List[StartupItem]:
    items = scan_login_items(ctx) + scan_launchd(ctx)
    order = {"login-item": 0, "agent": 1, "global-agent": 2, "daemon": 3}
    items.sort(key=lambda i: (not i.broken, order[i.kind], i.name.lower()))
    return items


# --------------------------------------------------------------------------- actions

def plan(item: StartupItem, action: str, ctx: Context) -> List[List[str]]:
    """Commands for ``action`` in (disable, enable, remove). Sudo is prefixed for system items."""
    uid = ctx.uid
    if item.kind == "login-item":
        if action != "remove":
            return []
        name = item.label.replace('"', '\\"')
        return [["osascript", "-e", f'tell application "System Events" to delete login item "{name}"']]
    domain = "system" if item.kind == "daemon" else f"gui/{uid}"
    sudo = ["sudo"] if item.needs_root and not ctx.is_root else []
    if action == "disable":
        return [sudo + ["launchctl", "bootout", domain, item.path],
                sudo + ["launchctl", "disable", f"{domain}/{item.label}"]]
    if action == "enable":
        return [sudo + ["launchctl", "enable", f"{domain}/{item.label}"],
                sudo + ["launchctl", "bootstrap", domain, item.path]]
    if action == "remove":
        cmds = [sudo + ["launchctl", "bootout", domain, item.path]]
        if item.kind == "agent":
            return cmds  # the plist is moved to the Trash by remove_plist()
        root_sudo = [] if ctx.is_root else ["sudo"]
        dest = os.path.join(ctx.path("~/.Trash"), os.path.basename(item.path))
        return cmds + [root_sudo + ["mv", item.path, dest]]
    raise ValueError(action)


def remove_plist(item: StartupItem, ctx: Context) -> str:
    """Move a user LaunchAgent plist to the Trash (reversible)."""
    agents = os.path.realpath(ctx.path("~/Library/LaunchAgents"))
    if item.kind != "agent" or os.path.dirname(os.path.realpath(item.path)) != agents:
        raise ValueError("only your own LaunchAgents can be removed without admin rights")
    trash = ctx.path("~/.Trash")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, os.path.basename(item.path))
    if os.path.exists(dest):
        dest = dest[:-6] + time.strftime(" %H.%M.%S") + ".plist"
    shutil.move(item.path, dest)
    return dest


def apply(item: StartupItem, action: str, ctx: Context) -> Tuple[bool, str]:
    """Run an action. Returns (ok, message). bootout errors are ignored (item may not be loaded)."""
    errors = []
    for cmd in plan(item, action, ctx):
        res = ctx.run(cmd, timeout=30, as_user=cmd[0] != "sudo")
        if res is None:
            errors.append(f"could not run {cmd[0]}")
        elif res.returncode != 0 and "bootout" not in cmd and "bootstrap" not in cmd:
            errors.append((res.stderr or res.stdout).strip().splitlines()[-1:] or [f"{cmd[0]} failed"])
    if action == "remove" and item.kind == "agent":
        try:
            remove_plist(item, ctx)
        except (OSError, ValueError) as e:
            errors.append(str(e))
    if errors:
        flat = [e if isinstance(e, str) else e[0] for e in errors]
        return False, "; ".join(dict.fromkeys(flat))
    verb = {"disable": "Disabled", "enable": "Enabled", "remove": "Removed"}[action]
    return True, f"{verb} {item.name}" + (" (moved to Trash)" if action == "remove" and item.kind != "login-item" else "")
