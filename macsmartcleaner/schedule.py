"""Weekly automatic cleanup of the *safe* tier via a launchd user agent."""
from __future__ import annotations

import os
import plistlib
import sys

from .context import Context

LABEL = "com.macsmartcleaner.weekly"


def plist_path(ctx: Context) -> str:
    return ctx.path(f"~/Library/LaunchAgents/{LABEL}.plist")


def build_plist(ctx: Context, weekday: int = 0, hour: int = 11) -> dict:
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log = ctx.path("~/.local/state/macsmartcleaner/scheduled.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "macsmartcleaner", "clean", "--tier", "safe",
                             "--yes", "--min-age", "7"],
        "EnvironmentVariables": {"PYTHONPATH": pkg_parent, "NO_COLOR": "1"},
        "StartCalendarInterval": {"Weekday": weekday, "Hour": hour, "Minute": 0},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "LowPriorityIO": True,
        "Nice": 10,
    }


def install(ctx: Context, weekday: int = 0, hour: int = 11) -> str:
    path = plist_path(ctx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(ctx.path("~/.local/state/macsmartcleaner"), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(ctx, weekday, hour), fh)
    uid = os.getuid()
    ctx.run(["launchctl", "bootout", f"gui/{uid}", path], timeout=30, as_user=False)
    res = ctx.run(["launchctl", "bootstrap", f"gui/{uid}", path], timeout=30, as_user=False)
    if res is not None and res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip()}")
    return path


def remove(ctx: Context) -> bool:
    path = plist_path(ctx)
    if not os.path.exists(path):
        return False
    ctx.run(["launchctl", "bootout", f"gui/{os.getuid()}", path], timeout=30, as_user=False)
    os.remove(path)
    return True
