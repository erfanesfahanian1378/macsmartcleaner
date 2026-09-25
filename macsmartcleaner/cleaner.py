"""Select findings to clean and execute the cleanup, logging everything."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

from . import safety
from .context import Context
from .rules import Action, Safety
from .scanner import Finding
from .sizes import human, measure
from .ui import NULL, Reporter

TIER_LEVELS = {"safe": {Safety.SAFE}, "caution": {Safety.SAFE, Safety.CAUTION}}


def select(findings: Iterable[Finding], tier: str = "safe", only: Sequence[str] = (),
           skip: Sequence[str] = ()) -> List[Finding]:
    """Pick what to clean.

    ``only`` names rules explicitly and is the only way to clean *review* items.
    """
    chosen = []
    for f in findings:
        if not f.cleanable or f.rule.id in skip:
            continue
        if only:
            if f.rule.id in only or any(o.endswith("*") and f.rule.id.startswith(o[:-1]) for o in only):
                chosen.append(f)
        elif f.rule.safety in TIER_LEVELS[tier]:
            chosen.append(f)
    return chosen


@dataclass
class Outcome:
    finding: Finding
    freed: int = 0     # bytes actually released
    trashed: int = 0   # bytes moved to the Trash (released once the Trash is emptied)
    ok: bool = True
    skipped_root: bool = False
    messages: List[str] = field(default_factory=list)  # problems
    notes: List[str] = field(default_factory=list)     # informational (dry-run preview, "moved to Trash")


def _make_writable_and_retry(func, path, _exc):
    # Go module caches & some tool caches are read-only; make writable and retry once.
    try:
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR)
        if not os.path.islink(path):
            os.chmod(path, os.lstat(path).st_mode | stat.S_IWUSR)
        func(path)
    except OSError:
        pass


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=_make_writable_and_retry)
    else:
        os.unlink(path)


def _remove_files_only(path: str) -> None:
    """Delete every file below ``path`` but keep the folders (apps expect their log folders)."""
    if not os.path.isdir(path) or os.path.islink(path):
        os.unlink(path)
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass


def _format_cmd(cmd: Sequence[str], root: Optional[str]) -> List[str]:
    return [c.replace("{root}", root or "") for c in cmd]


def running_apps(names: Sequence[str], ctx: Context) -> List[str]:
    out = []
    for n in names:
        res = ctx.run(["pgrep", "-xq", n], timeout=5, as_user=False)
        if res is not None and res.returncode == 0:
            out.append(n)
    return out


def _order(findings: Sequence[Finding]) -> List[Finding]:
    # Empty the Trash before anything else is moved into it, so "reversible" items stay reversible.
    return sorted(findings, key=lambda f: 0 if f.rule.id == "trash" else 1)


def execute(findings: Sequence[Finding], ctx: Context, dry_run: bool = True,
            log: Optional[Callable[[str], None]] = None, reporter: Reporter = NULL) -> List[Outcome]:
    say = log or (lambda _m: None)
    outcomes: List[Outcome] = []
    findings = _order(findings)
    units = sum(len(f.rule.commands) if f.rule.action == Action.COMMAND else len(f.targets) or 1
                for f in findings)
    reporter.begin("clean", "Previewing cleanup" if dry_run else "Cleaning", total=units)
    for f in findings:
        o = Outcome(f)
        outcomes.append(o)
        rule = f.rule
        if rule.needs_root and not ctx.is_root and not dry_run:
            o.ok = False
            o.skipped_root = True
            o.messages.append("needs your admin password")
            reporter.step(max(len(f.targets), 1))
            continue

        if rule.handler is not None:
            reporter.step(current=rule.name)
            if dry_run:
                o.notes.append("would delete them with tmutil" if rule.id == "tm-snapshots" else "would run")
            else:
                o.ok, msg = rule.handler(ctx, dry_run)
                if msg:
                    (o.notes if o.ok else o.messages).append(msg)
            continue

        if rule.action == Action.COMMAND:
            for cmd in rule.commands:
                full = _format_cmd(cmd, f.root)
                if rule.command_needs_root and not ctx.is_root:
                    full = ["sudo"] + full
                reporter.step(current="running " + " ".join(full))
                if dry_run:
                    o.notes.append("would run: " + " ".join(full))
                    continue
                say(f"running: {' '.join(full)}")
                res = ctx.run(full, as_user=not rule.command_needs_root)
                if res is None:
                    o.ok = False
                    o.messages.append(f"`{full[0]}` not found or could not start - skipped")
                elif res.returncode != 0:
                    o.ok = False
                    err = (res.stderr or res.stdout or "").strip().splitlines()
                    o.messages.append(f"`{' '.join(full)}` failed: {err[-1] if err else res.returncode}")
            if not dry_run and f.size_known:
                after = sum(measure(t.path).bytes for t in f.targets) if f.targets else (
                    measure(f.root).bytes if f.root else 0)
                o.freed = max(0, f.size - after)
            elif dry_run:
                o.freed = f.size
            reporter.count(nbytes=o.freed)
            continue

        for t in f.targets:
            reporter.current(t.path)
            if not os.path.lexists(t.path):
                # already gone: removed with its parent folder, or macOS pruned it (log rotation)
                if len(f.targets) == 1:
                    o.notes.append("already removed (macOS or another step got to it first)")
                reporter.step()
                continue
            try:
                safe_path = safety.check(t.path, ctx)
            except safety.UnsafePath as e:
                o.ok = False
                o.messages.append(str(e))
                reporter.step()
                continue
            if dry_run:
                if rule.trash:
                    o.trashed += t.usage.bytes
                else:
                    o.freed += t.usage.bytes
                reporter.step()
                reporter.count(t.usage.files, t.usage.bytes)
                continue
            if rule.trash:
                try:
                    move_to_trash([safe_path], ctx)
                    o.trashed += t.usage.bytes
                except (OSError, safety.UnsafePath) as e:
                    o.ok = False
                    o.messages.append(f"{t.path}: {getattr(e, 'strerror', None) or e}")
                reporter.step()
                reporter.count(t.usage.files, t.usage.bytes)
                continue
            try:
                (_remove_files_only if rule.keep_dirs else _remove)(safe_path)
            except OSError as e:
                o.messages.append(f"{t.path}: {e.strerror or e}")
            remaining = measure(safe_path)
            freed = max(0, t.usage.bytes - (remaining.bytes if remaining.exists else 0))
            o.freed += freed
            reporter.step()
            reporter.count(t.usage.files, freed)
            if remaining.exists and remaining.files and remaining.bytes > 64 * 1024:
                o.messages.append(f"partly removed {t.path} (some files are protected or in use)")
        if o.trashed and not dry_run:
            o.notes.append("moved to the Trash - empty the Trash to free the space")
    total = sum(o.freed for o in outcomes)
    trashed = sum(o.trashed for o in outcomes)
    summary = f"{human(total)} {'would be freed' if dry_run else 'freed'}"
    if trashed:
        summary += f", {human(trashed)} {'would go' if dry_run else 'moved'} to the Trash"
    reporter.end(summary)
    if not dry_run:
        _write_history(ctx, outcomes)
    return outcomes


def root_findings(findings: Sequence[Finding], ctx: Context) -> List[Finding]:
    return [f for f in findings if f.rule.needs_root and not ctx.is_root]


def clean_as_admin(findings: Sequence[Finding], ctx: Context, quiet: bool = False) -> bool:
    """Clean root-only findings by re-running msc through sudo for exactly these items.

    Asks for the password once (like any Mac app asking for admin rights). Returns True if it ran.
    """
    import subprocess
    import sys

    items = [f for f in findings if f.root]
    if not items or not sys.stdin.isatty():
        return False
    if subprocess.call(["sudo", "-v"]) != 0:
        return False
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = ["sudo", "-n", "env", f"PYTHONPATH={pkg_parent}", sys.executable, "-m", "macsmartcleaner"]
    if quiet:
        cmd.append("-q")
    cmd += ["clean", "--yes", "--tier", "caution", "--only", ",".join(sorted({f.rule.id for f in items}))]
    for f in items:
        cmd += ["--root", f.root]  # type: ignore[list-item]
    return subprocess.call(cmd) == 0


def history_path(ctx: Context) -> str:
    return ctx.path("~/.local/state/macsmartcleaner/history.jsonl")


def _write_history(ctx: Context, outcomes: Sequence[Outcome]) -> None:
    path = history_path(ctx)
    try:
        ctx.makedirs(os.path.dirname(path))
        with open(path, "a") as fh:
            for o in outcomes:
                fh.write(json.dumps({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "rule": o.finding.rule.id,
                    "root": o.finding.root, "freed": o.freed, "trashed": o.trashed, "ok": o.ok,
                    "messages": o.messages, "notes": o.notes,
                }) + "\n")
        ctx.give_back(path)
    except OSError:
        pass


def move_to_trash(paths: Sequence[str], ctx: Context) -> List[str]:
    """Reversible manual removal for things `discover` flagged: move into ~/.Trash."""
    trash = ctx.path("~/.Trash")
    ctx.makedirs(trash)
    moved = []
    for p in paths:
        safe_path = safety.check(p, ctx, trash=True)
        name = os.path.basename(safe_path.rstrip("/"))
        dest = os.path.join(trash, name)
        if os.path.lexists(dest):  # Finder style: "name 12.03.44.ext"
            stem, ext = os.path.splitext(name)
            if os.path.isdir(safe_path) and not ext.lower() in (".app", ".bundle", ".plugin", ".savedstate"):
                stem, ext = name, ""
            dest = os.path.join(trash, f"{stem} {time.strftime('%H.%M.%S')}{ext}")
        shutil.move(safe_path, dest)
        moved.append(dest)
    return moved
