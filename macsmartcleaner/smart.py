"""Smart Clean: one command that clears caches and junk without breaking anything.

"Smart" means:
  * only rules that are safe (plus Electron app caches) - nothing that is data
  * caches of apps that are running right now are left alone, so nothing you
    have open glitches (Slack, Chrome, Spotify... are skipped until you quit them)
  * rules tied to a running app (Xcode DerivedData while Xcode is open) are skipped
  * root-only areas are skipped unless you run with sudo
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple

from . import cleaner, sizes, ui
from .context import Context
from .rules import Rule, Safety
from .scanner import Finding, scan
from .sizes import human

EXTRA_RULES = {"electron-caches", "editor-vsix", "huggingface-xet"}  # caution-tier caches that are fine here


@dataclass
class RunningApps:
    names: Set[str] = field(default_factory=set)       # lower-case app names ("google chrome")
    bundle_ids: Set[str] = field(default_factory=set)  # lower-case ids ("com.google.chrome")
    display: List[str] = field(default_factory=list)

    def matches(self, folder: str) -> Optional[str]:
        """Return the running app a cache folder belongs to, if any."""
        n = folder.lower()
        if not n:
            return None
        for bid in self.bundle_ids:
            if n == bid or bid.startswith(n + ".") or n.startswith(bid + "."):
                return bid
        for name in self.names:
            squashed = name.replace(" ", "")
            if n in (name, squashed) or (len(n) > 3 and (name.startswith(n + " ") or squashed.startswith(n))):
                return name
        return None


def running_apps(ctx: Context) -> RunningApps:
    res = ctx.run(["ps", "-Axo", "comm="], timeout=10, as_user=False)
    apps = RunningApps()
    if res is None:
        return apps
    seen = set()
    for line in res.stdout.splitlines():
        m = re.search(r"^(.*?/([^/]+)\.app)/Contents/MacOS/", line.strip())
        if not m or m.group(1) in seen:
            continue
        # skip helper apps nested inside other apps; the outer app is what the user knows
        seen.add(m.group(1))
        if ".app/" in m.group(1):
            continue
        apps.names.add(m.group(2).lower())
        apps.display.append(m.group(2))
        try:
            with open(os.path.join(m.group(1), "Contents", "Info.plist"), "rb") as fh:
                bid = plistlib.load(fh).get("CFBundleIdentifier")
            if bid:
                apps.bundle_ids.add(str(bid).lower())
        except Exception:  # noqa: BLE001
            pass
    apps.display.sort(key=str.lower)
    return apps


def smart_rules(rules: Sequence[Rule]) -> List[Rule]:
    return [r for r in rules if r.safety == Safety.SAFE or r.id in EXTRA_RULES]


@dataclass
class Plan:
    findings: List[Finding]
    skipped_open: List[Tuple[str, int]]   # (what, bytes) left alone because the app is open
    skipped_root: int = 0

    @property
    def total(self) -> int:
        return sum(f.size for f in self.findings)


def build_plan(findings: Sequence[Finding], apps: RunningApps, ctx: Context) -> Plan:
    keep: List[Finding] = []
    skipped: List[Tuple[str, int]] = []
    skipped_root = 0
    for f in findings:
        if not f.cleanable:
            continue
        if f.rule.needs_root and not ctx.is_root:
            skipped_root += f.size
            continue
        busy = next((a for a in f.rule.quit_apps if a.lower() in apps.names), None)
        if busy and f.rule.id != "electron-caches":
            skipped.append((f"{f.rule.name} ({busy} is open)", f.size))
            continue
        if f.targets and f.rule.action.value == "delete-contents":
            owner_dir = f.rule.id == "electron-caches"
            kept = []
            for t in f.targets:
                # electron: .../Application Support/<App>/Cache -> the app folder is 2-3 levels up
                probe = _electron_app(f.root) if owner_dir else os.path.basename(t.path)
                app = apps.matches(probe or "")
                if app:
                    skipped.append((f"{os.path.basename(t.path) if not owner_dir else probe} ({app} is open)",
                                    t.usage.bytes))
                else:
                    kept.append(t)
            if not kept:
                continue
            f.targets = kept
            f.size = sum(t.usage.bytes for t in kept)
        keep.append(f)
    keep.sort(key=lambda f: f.size, reverse=True)
    return Plan(keep, skipped, skipped_root)


def _electron_app(root: Optional[str]) -> str:
    if not root:
        return ""
    parts = root.split(os.sep)
    try:
        i = parts.index("Application Support")
        return parts[i + 1]
    except (ValueError, IndexError):
        return ""


def run(ctx: Context, rules: Sequence[Rule], assume_yes: bool = False, quiet: bool = False,
        dry_run: bool = False) -> int:
    """The Smart Clean flow: quick scan -> plan -> confirm -> clean -> result."""
    if not quiet:
        if sys.stdout.isatty():
            sys.stdout.write("\033[H\033[2J")
        ui.banner("smart clean - caches & junk, skipping apps you have open")
    with ui.make_reporter([("probe", 3), ("rules", 97)], quiet=quiet, counter_label="scanned") as rep, \
            sizes.cache_session():
        findings = scan(smart_rules(rules), ctx, reporter=rep)
    apps = running_apps(ctx)
    plan = build_plan(findings, apps, ctx)

    color = ui.color_enabled(sys.stdout)
    B, D, G, Y, R = (ui.BOLD, ui.DIM, ui.GREEN, ui.YELLOW, ui.RESET) if color else ("",) * 5
    if not plan.findings:
        print(f"\n  {G}✨ Already clean{R} - nothing safe to remove right now.")
        return 0
    print()
    biggest = plan.findings[0].size or 1
    for f in plan.findings[:12]:
        bar_w = max(1, int(20 * f.size / biggest))
        bar = ui.gradient_text("█" * bar_w) if color else "#" * bar_w
        print(f"  {human(f.size):>9}  {bar}{' ' * (21 - bar_w)}{f.rule.name}")
    if len(plan.findings) > 12:
        rest = sum(f.size for f in plan.findings[12:])
        print(f"  {human(rest):>9}  {D}+ {len(plan.findings) - 12} smaller items{R}")
    if plan.skipped_open:
        names = sorted({s[0].split("(")[-1].replace(" is open)", "") for s in plan.skipped_open})
        print(f"\n  {Y}Skipping {human(sum(s[1] for s in plan.skipped_open))} of caches for open apps:{R} "
              f"{D}{', '.join(names[:8])}{'…' if len(names) > 8 else ''} - quit them and run again to include.{R}")
    if plan.skipped_root:
        print(f"  {D}{human(plan.skipped_root)} in system folders needs `sudo msc smart`.{R}")
    print(f"\n  {B}Ready to free {human(plan.total)}{R}")

    if dry_run:
        print("  (dry run - nothing deleted)")
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            print("  Not a terminal - rerun with --yes to clean.")
            return 1
        try:
            answer = input(f"  Clean it now? {B}[Y/n]{R} ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            print("  Nothing was deleted.")
            return 1

    before = shutil.disk_usage(ctx.home).free
    print()
    with ui.make_reporter([("clean", 1)], quiet=quiet, counter_label="freed") as rep:
        outcomes = cleaner.execute(plan.findings, ctx, dry_run=False, reporter=rep)
    after = shutil.disk_usage(ctx.home).free
    freed = sum(o.freed for o in outcomes)
    print(f"\n  {G}{B}✨ Freed {human(freed)}{R}   free space {human(before)} → {B}{human(after)}{R}")
    problems = [f"{o.finding.rule.name}: {m}" for o in outcomes for m in o.messages]
    if problems:
        print(f"  {D}{len(problems)} item(s) were partly skipped (in use or protected). Details in "
              f"~/.local/state/macsmartcleaner/history.jsonl{R}")
    return 0
