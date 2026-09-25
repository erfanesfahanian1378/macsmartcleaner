"""Uninstaller: remove an app together with everything it spread across the system.

Files are matched by the app's bundle id (com.vendor.app), which apps use to name
their data. The app's display name is only used for Application Support/Logs
folders and only on an exact match. Everything goes to the Trash (reversible);
system-level files (launch daemons, privileged helpers) are moved with your password.
"""
from __future__ import annotations

import calendar
import glob
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import safety
from .context import Context
from .sizes import Usage, measure_many

APP_ROOTS = ("/Applications", "~/Applications", "/Applications/Setapp")

# (folder, how to match inside it). {id} = bundle id, {name} = app name
USER_LOCATIONS = [
    ("~/Library/Application Support", ("{id}", "{name}")),
    ("~/Library/Caches", ("{id}", "{id}.*")),
    ("~/Library/Containers", ("{id}", "{id}.*")),
    ("~/Library/Group Containers", ("*.{id}", "group.{id}", "*.{id}.*", "group.{id}.*")),
    ("~/Library/Application Scripts", ("{id}", "{id}.*", "*.{id}")),
    ("~/Library/Preferences", ("{id}.plist", "{id}.*.plist")),
    ("~/Library/Preferences/ByHost", ("{id}.*.plist",)),
    ("~/Library/Saved Application State", ("{id}.savedState",)),
    ("~/Library/HTTPStorages", ("{id}", "{id}.binarycookies")),
    ("~/Library/WebKit", ("{id}",)),
    ("~/Library/Logs", ("{id}", "{name}")),
    ("~/Library/LaunchAgents", ("{id}.plist", "{id}.*.plist")),
]
SYSTEM_LOCATIONS = [
    ("/Library/Application Support", ("{id}", "{name}")),
    ("/Library/Caches", ("{id}",)),
    ("/Library/LaunchAgents", ("{id}.plist", "{id}.*.plist")),
    ("/Library/LaunchDaemons", ("{id}.plist", "{id}.*.plist")),
    ("/Library/PrivilegedHelperTools", ("{id}", "{id}.*")),
    ("/Library/Preferences", ("{id}.plist",)),
]


@dataclass
class App:
    name: str
    path: str
    bundle_id: str
    version: str = ""
    size: int = 0
    last_used: Optional[float] = None
    files: List[Tuple[str, int, bool]] = field(default_factory=list)  # (path, bytes, needs_admin)

    @property
    def total(self) -> int:
        return self.size + sum(f[1] for f in self.files)


def _info(app_path: str) -> Optional[dict]:
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as fh:
            return plistlib.load(fh)
    except Exception:  # noqa: BLE001 - damaged bundle
        return None


def list_apps(ctx: Context) -> List[App]:
    """Third-party apps in /Applications (one folder deep too) and ~/Applications."""
    apps: List[App] = []
    seen = set()
    for base in APP_ROOTS:
        root = ctx.path(base)
        for path in sorted(glob.glob(os.path.join(root, "*.app")) + glob.glob(os.path.join(root, "*", "*.app"))):
            real = os.path.realpath(path)
            if real in seen or os.path.islink(path):
                continue
            seen.add(real)
            info = _info(path) or {}
            bid = str(info.get("CFBundleIdentifier", "")).strip()
            if not bid or bid.lower().startswith("com.apple."):
                continue
            name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or
                       os.path.splitext(os.path.basename(path))[0])
            apps.append(App(name=name, path=path, bundle_id=bid,
                            version=str(info.get("CFBundleShortVersionString", ""))))
    return apps


def measure_apps(apps: Sequence[App], on_dir=None) -> None:
    sizes = measure_many([a.path for a in apps], on_dir=on_dir)
    for a in apps:
        a.size = sizes[a.path].bytes


def _parse_mdls_dates(raw: str, count: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for part in raw.split("\0")[:count]:
        part = part.strip()
        try:  # mdls prints UTC: "2025-05-01 10:00:00 +0000"
            out.append(float(calendar.timegm(time.strptime(part[:19], "%Y-%m-%d %H:%M:%S")))
                       if part and part != "(null)" else None)
        except ValueError:
            out.append(None)
    return out + [None] * (count - len(out))


def load_last_used(apps: Sequence[App], ctx: Context) -> None:
    """Spotlight's 'Last opened' date for each app (one mdls call), falling back to atime."""
    if not apps:
        return
    res = ctx.run(["mdls", "-name", "kMDItemLastUsedDate", "-raw"] + [a.path for a in apps], timeout=60)
    dates = _parse_mdls_dates(res.stdout, len(apps)) if res is not None and res.returncode == 0 else [None] * len(apps)
    for a, d in zip(apps, dates):
        if d is None:
            try:
                d = os.stat(a.path).st_atime
            except OSError:
                d = None
        a.last_used = d


def _matches(entry: str, patterns: Sequence[str], bid: str, name: str) -> bool:
    import fnmatch
    e = entry.lower()
    for pat in patterns:
        if "{name}" in pat:
            if len(name) >= 4 and e == pat.replace("{name}", name).lower():
                return True
            continue
        if fnmatch.fnmatchcase(e, pat.replace("{id}", bid).lower()):
            return True
    return False


def find_related(app: App, ctx: Context) -> List[Tuple[str, bool]]:
    """(path, needs_admin) for every file/folder that belongs to ``app``."""
    bid = app.bundle_id.lower()
    name = app.name
    out: List[Tuple[str, bool]] = []
    for locations, admin in ((USER_LOCATIONS, False), (SYSTEM_LOCATIONS, True)):
        for folder, patterns in locations:
            base = ctx.path(folder)
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for entry in entries:
                if _matches(entry, patterns, bid, name):
                    out.append((os.path.join(base, entry), admin and not ctx.is_root))
    return out


def collect(app: App, ctx: Context) -> None:
    related = find_related(app, ctx)
    sizes = measure_many([p for p, _ in related])
    app.files = [(p, sizes[p].bytes if sizes[p].exists else 0, admin) for p, admin in related]


def running(app: App, ctx: Context) -> bool:
    res = ctx.run(["pgrep", "-f", re.escape(app.path) + "/Contents/MacOS/"], timeout=5, as_user=False)
    return res is not None and res.returncode == 0


def quit_app(app: App, ctx: Context) -> None:
    ctx.run(["osascript", "-e", f'tell application id "{app.bundle_id}" to quit'], timeout=20)
    for _ in range(20):
        if not running(app, ctx):
            return
        time.sleep(0.25)


def uninstall(app: App, ctx: Context) -> Tuple[int, List[str]]:
    """Quit the app, unload its agents, move the app and its files to the Trash.

    Returns (bytes moved, problems). Needs ``collect`` first.
    """
    from .cleaner import move_to_trash

    problems: List[str] = []
    moved = 0
    if running(app, ctx):
        quit_app(app, ctx)
    for path, _size, _admin in app.files:
        if path.endswith(".plist") and "/LaunchAgents/" in path and path.startswith(ctx.home):
            ctx.run(["launchctl", "bootout", f"gui/{ctx.uid}", path], timeout=15, as_user=True)
    admin_paths: List[str] = []
    for path, size in [(app.path, app.size)] + [(p, s) for p, s, _a in app.files]:
        if not os.path.lexists(path):
            continue
        try:
            move_to_trash([path], ctx)
            moved += size
        except PermissionError:
            admin_paths.append(path)
        except (OSError, safety.UnsafePath) as e:
            problems.append(f"{path}: {getattr(e, 'strerror', None) or e}")
    if admin_paths:
        ok = _admin_move(admin_paths, ctx)
        if ok:
            moved += sum(s for p, s in [(app.path, app.size)] + [(p, s) for p, s, _a in app.files] if p in admin_paths)
        else:
            problems.append(f"{len(admin_paths)} item(s) need your password and were skipped")
    return moved, problems


def _admin_move(paths: Sequence[str], ctx: Context) -> bool:
    """Move root-owned items to the user's Trash with sudo (after the same safety check)."""
    trash = ctx.path("~/.Trash")
    targets = []
    for p in paths:
        try:
            targets.append(safety.check(p, ctx, trash=True))
        except safety.UnsafePath:
            return False
    if ctx.is_root:
        cmd_prefix: List[str] = []
    else:
        if subprocess.call(["sudo", "-v"]) != 0:
            return False
        cmd_prefix = ["sudo", "-n"]
    ok = True
    for p in targets:
        stem, ext = os.path.splitext(os.path.basename(p))
        dest = os.path.join(trash, os.path.basename(p))
        if os.path.lexists(dest):
            dest = os.path.join(trash, f"{stem} {time.strftime('%H.%M.%S')}{ext}")
        if subprocess.call(cmd_prefix + ["mv", p, dest]) != 0:
            ok = False
    return ok


def human_age(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return "never"
    days = int(((now or time.time()) - ts) / 86400)
    if days < 1:
        return "today"
    if days < 31:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def summary_by_kind(files: Sequence[Tuple[str, int, bool]]) -> Dict[str, int]:
    kinds: Dict[str, int] = {}
    for path, size, _a in files:
        parts = path.split(os.sep)
        kind = parts[-2] if len(parts) > 1 else path
        kinds[kind] = kinds.get(kind, 0) + size
    return kinds


__all__ = ["App", "list_apps", "measure_apps", "load_last_used", "find_related", "collect", "uninstall",
           "human_age", "Usage"]
