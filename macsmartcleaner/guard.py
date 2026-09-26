"""Auto-Protect: an hourly background check that keeps System Data from running away.

Every hour (as a LaunchDaemon, so it can see system folders) it:
  1. rebuilds the Spotlight index if it is bigger than the limit (a re-indexing loop)
  2. if free space is below the floor: deletes Time Machine local snapshots (they pin
     deleted data) and runs the safe cleanup (caches & logs, skipping open apps)
Everything it does is written to a log shown in the menu.
"""
from __future__ import annotations

import os
import plistlib
import pwd
import shutil
import sys
import time
from typing import List, Optional, Tuple

from .context import Context
from .sizes import cache_session, human

LABEL = "com.macsmartcleaner.guard"
OLD_LABEL = "com.macsmartcleaner.spotlightguard"
LOG = "/Library/Logs/macsmartcleaner-guard.log"
PLIST = f"/Library/LaunchDaemons/{LABEL}.plist"
DEFAULT_INDEX_MAX = 20_000_000_000
DEFAULT_MIN_FREE = 50_000_000_000


def build_plist(index_max: int, min_free: int, user: str, interval: int = 3600) -> dict:
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "macsmartcleaner", "-q", "guard", "check",
                             "--index", str(index_max), "--min-free", str(min_free), "--user", user],
        "EnvironmentVariables": {"PYTHONPATH": pkg_parent, "NO_COLOR": "1"},
        "StartInterval": interval,
        "RunAtLoad": True,
        "LowPriorityIO": True,
        "Nice": 10,
    }


def _launchctl(ctx: Context, *args: str) -> None:
    ctx.run(["launchctl", *args], timeout=30, as_user=False)


def install(ctx: Context, index_max: int, min_free: int, user: Optional[str] = None) -> Tuple[bool, str]:
    if not ctx.is_root:
        return False, "Auto-Protect needs admin rights: run `sudo msc guard on`"
    user = user or ctx.sudo_user or pwd.getpwuid(ctx.uid).pw_name
    for old in (ctx.path(f"/Library/LaunchDaemons/{OLD_LABEL}.plist"), ctx.path(PLIST)):
        if os.path.exists(old):
            _launchctl(ctx, "bootout", "system", old)
            os.remove(old)
    path = ctx.path(PLIST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(index_max, min_free, user), fh)
    os.chmod(path, 0o644)
    _launchctl(ctx, "bootstrap", "system", path)
    return True, (f"Auto-Protect on: every hour, rebuild Spotlight above {human(index_max)}, "
                  f"free up space below {human(min_free)} free")


def remove(ctx: Context) -> Tuple[bool, str]:
    paths = [ctx.path(PLIST), ctx.path(f"/Library/LaunchDaemons/{OLD_LABEL}.plist")]
    present = [p for p in paths if os.path.exists(p)]
    if not present:
        return True, "Auto-Protect is not installed"
    if not ctx.is_root:
        return False, "turning Auto-Protect off needs admin rights: run `sudo msc guard off`"
    for p in present:
        _launchctl(ctx, "bootout", "system", p)
        os.remove(p)
    return True, "Auto-Protect turned off"


def status(ctx: Context) -> dict:
    """What's installed and the latest log lines (readable without sudo)."""
    info = {"installed": False, "index_max": DEFAULT_INDEX_MAX, "min_free": DEFAULT_MIN_FREE, "log": []}
    try:
        with open(ctx.path(PLIST), "rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments", [])
        info["installed"] = True
        for flag, key in (("--index", "index_max"), ("--min-free", "min_free")):
            if flag in args:
                info[key] = int(args[args.index(flag) + 1])
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
        pass
    try:
        with open(ctx.path(LOG)) as fh:
            info["log"] = [ln.rstrip() for ln in fh.readlines()[-12:]]
    except OSError:
        pass
    return info


def context_for(user: Optional[str]) -> Context:
    """The daemon runs as root without SUDO_USER: act on the configured user's home."""
    if not user:
        return Context()
    pw = pwd.getpwnam(user)
    return Context(home=pw.pw_dir, sudo_user=user, uid=pw.pw_uid, gid=pw.pw_gid)


def check(ctx: Context, index_max: int, min_free: int, dry_run: bool = False) -> List[str]:
    """One Auto-Protect pass. Returns (and logs) what it found and did."""
    from . import cleaner, smart, spotlight
    from .rules import BUILTIN_RULES, _delete_tm_snapshots, _tm_snapshot_count

    stamp = time.strftime("%Y-%m-%d %H:%M")
    notes: List[str] = []
    size = spotlight.index_size(ctx)
    if size is None:
        notes.append("Spotlight index: can't measure (needs admin)")
    elif size > index_max:
        if dry_run:
            notes.append(f"Spotlight index {human(size)} > {human(index_max)}: would rebuild")
        else:
            ok, msg = spotlight.rebuild(ctx)
            notes.append(f"Spotlight index {human(size)} > {human(index_max)}: {msg}")
    else:
        notes.append(f"Spotlight index {human(size)}: ok")

    free = shutil.disk_usage(ctx.home).free
    if free >= min_free:
        notes.append(f"free space {human(free)}: ok")
    else:
        notes.append(f"free space {human(free)} < {human(min_free)}: freeing space")
        snaps = _tm_snapshot_count(ctx)
        if snaps > 0:
            if dry_run:
                notes.append(f"  would delete {snaps} Time Machine local snapshot(s)")
            else:
                _ok, msg = _delete_tm_snapshots(ctx, False)
                notes.append(f"  snapshots: {msg}")
        with cache_session():
            from .scanner import scan
            findings = scan(smart.smart_rules(BUILTIN_RULES), ctx)
        plan = smart.build_plan(findings, smart.running_apps(ctx), ctx)
        outcomes = cleaner.execute(plan.findings, ctx, dry_run=dry_run)
        freed = sum(o.freed for o in outcomes)
        notes.append(f"  safe cleanup: {human(freed)} {'would be freed' if dry_run else 'freed'}")
        notes.append(f"  free space now {human(shutil.disk_usage(ctx.home).free)}")

    if not dry_run:
        try:
            with open(ctx.path(LOG), "a") as fh:
                for n in notes:
                    fh.write(f"{stamp}  {n}\n")
        except OSError:
            pass
    return notes
