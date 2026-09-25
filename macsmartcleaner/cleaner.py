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
from .sizes import measure

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
    freed: int = 0
    ok: bool = True
    messages: List[str] = field(default_factory=list)


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


def _format_cmd(cmd: Sequence[str], root: Optional[str]) -> List[str]:
    return [c.replace("{root}", root or "") for c in cmd]


def running_apps(names: Sequence[str], ctx: Context) -> List[str]:
    out = []
    for n in names:
        res = ctx.run(["pgrep", "-xq", n], timeout=5, as_user=False)
        if res is not None and res.returncode == 0:
            out.append(n)
    return out


def execute(findings: Sequence[Finding], ctx: Context, dry_run: bool = True,
            log: Optional[Callable[[str], None]] = None) -> List[Outcome]:
    say = log or (lambda _m: None)
    outcomes: List[Outcome] = []
    for f in findings:
        o = Outcome(f)
        outcomes.append(o)
        rule = f.rule
        if rule.needs_root and not ctx.is_root:
            o.ok = False
            o.messages.append("needs sudo - rerun with `sudo msc clean ...` to include it")
            continue

        if rule.action == Action.COMMAND:
            for cmd in rule.commands:
                full = _format_cmd(cmd, f.root)
                if rule.command_needs_root and not ctx.is_root:
                    full = ["sudo"] + full
                if dry_run:
                    o.messages.append("would run: " + " ".join(full))
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
            continue

        for t in f.targets:
            try:
                safe_path = safety.check(t.path, ctx)
            except safety.UnsafePath as e:
                o.ok = False
                o.messages.append(str(e))
                continue
            if dry_run:
                o.freed += t.usage.bytes
                continue
            try:
                _remove(safe_path)
            except OSError as e:
                o.messages.append(f"{t.path}: {e.strerror or e}")
            remaining = measure(safe_path)
            o.freed += max(0, t.usage.bytes - (remaining.bytes if remaining.exists else 0))
            if remaining.exists and remaining.bytes:
                o.messages.append(f"partially removed {t.path} (some files are protected or in use)")
    if not dry_run:
        _write_history(ctx, outcomes)
    return outcomes


def history_path(ctx: Context) -> str:
    return ctx.path("~/.local/state/macsmartcleaner/history.jsonl")


def _write_history(ctx: Context, outcomes: Sequence[Outcome]) -> None:
    path = history_path(ctx)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for o in outcomes:
                fh.write(json.dumps({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "rule": o.finding.rule.id,
                    "root": o.finding.root, "freed": o.freed, "ok": o.ok, "messages": o.messages,
                }) + "\n")
    except OSError:
        pass


def move_to_trash(paths: Sequence[str], ctx: Context) -> List[str]:
    """Reversible manual removal for things `discover` flagged: move into ~/.Trash."""
    trash = ctx.path("~/.Trash")
    os.makedirs(trash, exist_ok=True)
    moved = []
    for p in paths:
        safe_path = safety.check(p, ctx)
        dest = os.path.join(trash, os.path.basename(safe_path.rstrip("/")))
        if os.path.lexists(dest):
            dest += time.strftime(" %H.%M.%S")
        shutil.move(safe_path, dest)
        moved.append(dest)
    return moved
