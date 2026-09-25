"""Turn the rule catalog into concrete findings on this machine.

Three phases:
  1. expand   - resolve globs/probes into root paths; list children of
                delete-contents roots (cheap, single thread)
  2. measure  - size every path in a thread pool (the slow part)
  3. assemble - build Findings, applying exclusions, min-age and de-duplication
                so a folder claimed by a specific rule (e.g. ~/Library/Caches/Homebrew)
                is never also counted/cleaned by a generic one (~/Library/Caches).
"""
from __future__ import annotations

import fnmatch
import glob
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .context import Context
from .rules import Action, Rule
from .sizes import Usage, measure_many
from .ui import NULL, Reporter


@dataclass
class Target:
    path: str
    usage: Usage


@dataclass
class Finding:
    rule: Rule
    root: Optional[str]
    size: int
    files: int = 0
    newest_mtime: float = 0.0
    targets: List[Target] = field(default_factory=list)
    note: str = ""
    size_known: bool = True
    errors: int = 0
    skipped_young: int = 0  # children skipped because they were used recently

    @property
    def cleanable(self) -> bool:
        if self.rule.action == Action.REPORT:
            return False
        if self.rule.action == Action.COMMAND:
            return self.size > 0 or not self.size_known
        return bool(self.targets)


def _key(path: str) -> str:
    p = os.path.normpath(path)
    return p.lower() if sys.platform == "darwin" else p  # APFS is case-insensitive


def _static_prefix(pattern: str) -> str:
    parts = pattern.split(os.sep)
    out = []
    for part in parts:
        if glob.has_magic(part):
            break
        out.append(part)
    return os.sep.join(out)


def _is_ancestor_or_same(a: str, b: str) -> bool:
    """True if ``a`` is ``b`` or a parent directory of ``b`` (both normalized keys)."""
    return b == a or b.startswith(a.rstrip(os.sep) + os.sep)


def _expand(rule: Rule, ctx: Context, probes: Dict[str, object]) -> List[str]:
    roots: List[str] = []
    for pattern in rule.paths:
        full = ctx.path(pattern)
        if glob.has_magic(full):
            roots.extend(sorted(glob.glob(full)))
        elif os.path.lexists(full):
            roots.append(full)
    probe = probes.get(rule.id)
    if probe is not None:
        roots.extend(probe.roots)  # type: ignore[attr-defined]
    # never follow a symlinked root anywhere
    return [r for r in roots if not os.path.islink(r)]


def measure_all(paths: Iterable[str], reporter: Reporter, workers: Optional[int] = None) -> Dict[str, Usage]:
    """Measure paths in parallel worker processes, feeding live counters and one step per path."""
    def on_dir(d: str, files: int, nbytes: int) -> None:
        reporter.count(files, nbytes)
        reporter.current(d)

    return measure_many(list(paths), workers=workers, on_dir=on_dir, on_root_done=lambda _p: reporter.step())


def scan(rules: Iterable[Rule], ctx: Context, reporter: Reporter = NULL, workers: Optional[int] = None) -> List[Finding]:
    rules = list(rules)

    # ---- probes (tmutil, simctl, getconf) --------------------------------
    probes: Dict[str, object] = {}
    probe_rules = [r for r in rules if r.probe is not None]
    reporter.begin("probe", "Checking Time Machine, simulators & system tools", total=len(probe_rules))
    for rule in probe_rules:
        reporter.current(rule.name)
        probes[rule.id] = rule.probe(ctx)
        reporter.step()
    snaps = getattr(probes.get("tm-snapshots"), "present", False)
    reporter.end("Time Machine snapshots found" if snaps else "")

    # ---- phase 1: expand ---------------------------------------------------
    rule_roots: Dict[str, List[str]] = {r.id: _expand(r, ctx, probes) for r in rules}

    # A root claimed by an exact (non-glob) pattern beats the same root reached via a glob.
    owner: Dict[str, Tuple[int, str]] = {}
    for rule in rules:
        exact = {_key(ctx.path(p)) for p in rule.paths if not glob.has_magic(p)}
        for root in rule_roots[rule.id]:
            k = _key(root)
            prio = 0 if k in exact else 1
            if k not in owner or prio < owner[k][0]:
                owner[k] = (prio, rule.id)
    for rule in rules:
        rule_roots[rule.id] = [r for r in rule_roots[rule.id] if owner[_key(r)][1] == rule.id]

    # claims used to carve specific rules out of generic delete-contents roots
    claims: List[Tuple[str, str]] = []  # (key, rule id)
    for rule in rules:
        for pattern in rule.paths:
            claims.append((_key(_static_prefix(ctx.path(pattern))), rule.id))
        for root in rule_roots[rule.id]:
            claims.append((_key(root), rule.id))

    def claimed_by_other(path: str, rule_id: str) -> bool:
        k = _key(path)
        return any(rid != rule_id and _is_ancestor_or_same(k, ck) for ck, rid in claims)

    jobs: Set[str] = set()
    children: Dict[Tuple[str, str], List[str]] = {}
    for rule in rules:
        for root in rule_roots[rule.id]:
            if rule.action == Action.DELETE_CONTENTS:
                kids: List[str] = []
                try:
                    with os.scandir(root) as it:
                        for e in it:
                            if any(fnmatch.fnmatch(e.name, pat) for pat in rule.exclude):
                                continue
                            if e.name in (".DS_Store", ".localized"):
                                continue
                            if claimed_by_other(e.path, rule.id):
                                continue
                            kids.append(e.path)
                except OSError:
                    pass
                children[(rule.id, root)] = kids
                jobs.update(kids)
            else:
                jobs.add(root)

    # ---- phase 2: measure --------------------------------------------------
    reporter.begin("rules", "Measuring known junk locations", total=len(jobs))
    usages = measure_all(sorted(jobs), reporter, workers)

    # ---- phase 3: assemble -------------------------------------------------
    findings: List[Finding] = []
    for rule in rules:
        probe = probes.get(rule.id)
        if probe is not None and getattr(probe, "virtual", False):
            if getattr(probe, "present", False):
                findings.append(Finding(rule, None, 0, note=probe.note, size_known=False))  # type: ignore[attr-defined]
            continue

        roots = rule_roots[rule.id]
        if rule.action == Action.COMMAND and probe is not None and roots:
            # probe-driven command (orphaned simulators): one finding for all roots
            total = Usage()
            for r in roots:
                total.add(usages[r])
            findings.append(Finding(rule, None, total.bytes, total.files, total.newest_mtime,
                                    [Target(r, usages[r]) for r in roots],
                                    note=probe.note, errors=total.errors))  # type: ignore[attr-defined]
            continue

        for root in roots:
            if rule.action == Action.DELETE_CONTENTS:
                f = Finding(rule, root, 0)
                for kid in children.get((rule.id, root), []):
                    u = usages[kid]
                    if not u.exists:
                        continue
                    f.errors += u.errors
                    if rule.min_age_days and ctx.days_since(u.newest_mtime) < rule.min_age_days:
                        f.skipped_young += 1
                        continue
                    f.targets.append(Target(kid, u))
                    f.size += u.bytes
                    f.files += u.files
                    f.newest_mtime = max(f.newest_mtime, u.newest_mtime)
                if f.size or f.skipped_young:
                    findings.append(f)
            else:
                u = usages[root]
                if not u.exists:
                    continue
                if rule.action == Action.DELETE and rule.min_age_days and \
                        ctx.days_since(u.newest_mtime) < rule.min_age_days:
                    continue
                targets = [Target(root, u)] if rule.action == Action.DELETE else []
                f = Finding(rule, root, u.bytes, u.files, u.newest_mtime, targets, errors=u.errors)
                if u.errors and not u.files and not ctx.is_root:
                    # a root-only folder we couldn't look inside: don't show a misleading 0
                    f.size_known = False
                    f.note = "run with sudo to measure"
                findings.append(f)

    _subtract_nested(findings)
    findings.sort(key=lambda f: f.size, reverse=True)
    reporter.end(f"{len(jobs):,} places checked")
    return findings


def _subtract_nested(findings: List[Finding]) -> None:
    """When one finding deletes a folder that contains another finding (an app leftover
    containing its sandbox cache), count the inner bytes only once - on the inner finding."""
    outer = [f for f in findings if f.rule.action == Action.DELETE and f.root]
    if not outer:
        return
    for o in outer:
        prefix = _key(o.root) + os.sep  # type: ignore[arg-type]
        inner = 0
        for f in findings:
            if f is o:
                continue
            for t in f.targets or ([Target(f.root, Usage(bytes=f.size))] if f.root else []):
                if _key(t.path).startswith(prefix):
                    inner += t.usage.bytes
        if inner:
            o.size = max(0, o.size - inner)
