"""One-click maintenance tasks, each explained, each using Apple's own tools."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from . import ui
from .context import Context

LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    why: str
    commands: Tuple[Tuple[str, ...], ...]
    needs_root: bool = False
    recommended: bool = True
    note: str = ""


TASKS: List[Task] = [
    Task("dns", "Flush DNS cache",
         "Fixes websites that won't load or point to an old address after network or DNS changes.",
         (("dscacheutil", "-flushcache"), ("killall", "-HUP", "mDNSResponder")), needs_root=True),
    Task("memory", "Free up inactive memory",
         "Drops cached memory that nothing is using right now, so apps that need RAM get it immediately.",
         (("purge",),), needs_root=True,
         note="The Mac may feel slightly slower for a minute while caches refill."),
    Task("maintenance", "Run macOS maintenance scripts",
         "Runs the daily/weekly/monthly housekeeping (log rotation, temp cleanup) macOS schedules at night.",
         (("periodic", "daily", "weekly", "monthly"),), needs_root=True),
    Task("quicklook", "Reset Quick Look thumbnails",
         "Fixes wrong or missing file previews and thumbnails in Finder.",
         (("qlmanage", "-r", "cache"), ("qlmanage", "-r"))),
    Task("launchservices", "Rebuild the 'Open With' app list",
         "Removes duplicate or deleted apps from right-click > Open With. Your default apps are kept.",
         ((LSREGISTER, "-kill", "-r", "-domain", "local", "-domain", "system", "-domain", "user"),),
         recommended=False, note="Takes about a minute."),
    Task("fonts", "Clear font caches",
         "Fixes garbled text or fonts that won't show up. Restart apps afterwards.",
         (("atsutil", "databases", "-removeUser"),), recommended=False),
    Task("ui", "Restart Dock, Finder & menu bar",
         "Fixes a stuck Dock, frozen Finder windows or menu bar icons that won't respond.",
         (("killall", "Dock"), ("killall", "Finder"), ("killall", "SystemUIServer")), recommended=False,
         note="Open Finder windows will close."),
    Task("verify", "Check the startup disk for errors",
         "Read-only check of your disk's file system (like Disk Utility > First Aid, without repairs).",
         (("diskutil", "verifyVolume", "/"),), recommended=False, note="Takes a few minutes."),
    Task("spotlight", "Rebuild Spotlight index",
         "Fixes Spotlight not finding files, and can shrink a bloated index.",
         (("mdutil", "-E", "/"),), needs_root=True, recommended=False,
         note="Spotlight and the Mac will be busy for a while (up to hours on big disks)."),
]


def available(ctx: Context) -> List[Task]:
    return [t for t in TASKS if all(ctx.which(c[0]) for c in t.commands)]


@dataclass
class Result:
    task: Task
    ok: bool
    seconds: float
    message: str = ""


def run_tasks(tasks: Sequence[Task], ctx: Context, quiet: bool = False) -> List[Result]:
    """Run tasks one by one with an animated spinner line each."""
    if any(t.needs_root for t in tasks) and not ctx.is_root:
        if os.system("sudo -v") != 0:
            print("  No admin password given - skipping tasks that need it.")
            tasks = [t for t in tasks if not t.needs_root]
    results = []
    for task in tasks:
        t0 = time.time()
        msg = ""
        ok = True
        with ui.Spinner(task.name, quiet=quiet) as spin:
            for cmd in task.commands:
                full = list(cmd)
                if task.needs_root and not ctx.is_root:
                    full = ["sudo", "-n"] + full
                res = ctx.run(full, timeout=3600, as_user=not task.needs_root)
                if res is None:
                    ok, msg = False, f"{cmd[0]} is not available"
                    break
                if res.returncode != 0 and not (cmd[0] == "killall" and "No matching" in (res.stderr or "")):
                    ok = False
                    lines = (res.stderr or res.stdout or "").strip().splitlines()
                    msg = lines[-1] if lines else f"exit code {res.returncode}"
                    break
            spin.done(ok, msg or task.note)
        results.append(Result(task, ok, time.time() - t0, msg))
    return results
