"""msc - find and clean what's inflating macOS "System Data"."""
from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import sys
from typing import List, Optional, Sequence

from . import __version__, cleaner, discover, report, schedule
from .context import Context
from .rules import Action, Rule, default_user_rules_path, load_rules
from .scanner import Finding, scan
from .sizes import human, parse_size


def _progress(quiet: bool):
    if quiet or not sys.stderr.isatty():
        return None

    def say(msg: str) -> None:
        sys.stderr.write(f"\r\033[K  {msg}...")
        sys.stderr.flush()
    return say


def _clear_progress(quiet: bool) -> None:
    if not quiet and sys.stderr.isatty():
        sys.stderr.write("\r\033[K")


def _rules(ctx: Context, args) -> List[Rule]:
    rules = load_rules(getattr(args, "rules_file", None) or default_user_rules_path(ctx))
    min_age = getattr(args, "min_age", None)
    if min_age:
        rules = [dataclasses.replace(r, min_age_days=max(r.min_age_days, min_age))
                 if r.action == Action.DELETE_CONTENTS else r for r in rules]
    return rules


def _split(values: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


# --------------------------------------------------------------------------- commands

def cmd_scan(args, ctx: Context) -> int:
    say = _progress(args.quiet)
    findings = scan(_rules(ctx, args), ctx, progress=say)
    projects: List[Finding] = []
    if not args.no_projects:
        if say:
            say("looking for project build artifacts")
        projects = discover.find_project_artifacts(ctx, args.projects or None)
    hogs = []
    if not args.no_discover:
        if say:
            say("looking for other big folders (this is the slow part)")
        hogs = discover.find_space_hogs(ctx, findings + projects, min_size=parse_size(args.hog_min))
    _clear_progress(args.quiet)
    report.print_report(ctx, findings, hogs, projects, min_size=parse_size(args.min_size))
    data = report.to_dict(ctx, findings, hogs, projects)
    if args.json:
        report.write_json(args.json, data)
        print(f"JSON report written to {args.json}")
    if args.html:
        report.write_html(args.html, data)
        print(f"HTML report written to {args.html}  (open it with: open '{args.html}')")
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_clean(args, ctx: Context) -> int:
    say = _progress(args.quiet)
    only, skip = _split(args.only), _split(args.skip)
    findings = scan(_rules(ctx, args), ctx, progress=say)
    chosen = cleaner.select(findings, tier=args.tier, only=[o for o in only if not o.startswith("project")],
                            skip=skip)
    if args.projects is not None or any(o.startswith("project") for o in only):
        if say:
            say("looking for project build artifacts")
        projects = discover.find_project_artifacts(ctx, args.projects or None)
        projects = [p for p in projects if ctx.days_since(p.newest_mtime) >= args.older_than]
        if only and not any(o in ("project", "projects", "project:*") for o in only):
            projects = [p for p in projects if p.rule.id in only]
        chosen += [p for p in projects if p.rule.id not in skip]
    _clear_progress(args.quiet)

    if not chosen:
        print("Nothing to clean for this selection. Run `msc scan` to see everything.")
        return 0

    print(f"\nPlan ({'dry run' if args.dry_run else 'will delete'}):\n")
    total = 0
    for f in chosen:
        where = discover._display(ctx, f.root) if f.root else f.note
        print(f"  {report.size_str(f):>9}  {f.rule.safety.value:<7}  {f.rule.id:<24} {where}")
        total += f.size
    print(f"\n  Total: {human(total)}" + ("  (+ Time Machine snapshots, size unknown)"
                                         if any(not f.size_known for f in chosen) else ""))

    busy = cleaner.running_apps(sorted({a for f in chosen for a in f.rule.quit_apps}), ctx)
    if busy:
        print(report.c(f"\n  Quit these apps first for best results: {', '.join(busy)}", "33"))

    if args.interactive and not args.dry_run:
        chosen = [f for f in chosen if _confirm(
            f"  clean {f.rule.id} {report.size_str(f)} ({discover._display(ctx, f.root) if f.root else f.note})? [y/N] ")]
    elif not args.dry_run and not args.yes and not _confirm(f"\nDelete {human(total)} now? [y/N] "):
        print("Aborted. Nothing was deleted.")
        return 1

    before = shutil.disk_usage(ctx.home).free
    outcomes = cleaner.execute(chosen, ctx, dry_run=args.dry_run, log=None if args.quiet else print)
    after = shutil.disk_usage(ctx.home).free

    print()
    for o in outcomes:
        status = report.c("ok", "32") if o.ok else report.c("!!", "33")
        print(f"  {status}  {o.finding.rule.id:<24} {human(o.freed):>9}")
        for m in o.messages:
            print(report.c(f"        {m}", "2"))
    freed = sum(o.freed for o in outcomes)
    if args.dry_run:
        print(f"\nDry run: would free about {human(freed)}. Re-run without --dry-run to do it.")
    else:
        print(f"\nFreed about {human(freed)}. Free space: {human(before)} -> {human(after)}"
              + (" (APFS may take a minute to release space from snapshots/purgeable files)"
                 if after - before < freed else ""))
    return 0


def cmd_rules(args, ctx: Context) -> int:
    for r in _rules(ctx, args):
        print(f"{r.id:<24} {r.safety.value:<8} {r.action.value:<16} {r.name}")
    return 0


def cmd_explain(args, ctx: Context) -> int:
    for r in _rules(ctx, args):
        if r.id == args.rule:
            print(f"{r.name}  [{r.id}]\n  category: {r.category}\n  safety:   {r.safety.value}\n"
                  f"  action:   {r.action.value}")
            for p in r.paths:
                print(f"  path:     {p}")
            for cmd in r.commands:
                print(f"  command:  {' '.join(cmd)}")
            if r.min_age_days:
                print(f"  keeps items used in the last {r.min_age_days} days")
            print(f"\n  What it is: {r.why}\n  After cleaning: {r.impact}")
            return 0
    print(f"unknown rule: {args.rule} (see `msc rules`)", file=sys.stderr)
    return 2


def cmd_trash(args, ctx: Context) -> int:
    try:
        for dest in cleaner.move_to_trash(args.paths, ctx):
            print(f"moved to {dest}")
    except Exception as e:  # UnsafePath, OSError
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("Empty the Trash to actually free the space (or `msc clean --only trash`).")
    return 0


def cmd_schedule(args, ctx: Context) -> int:
    if args.action == "install":
        path = schedule.install(ctx, weekday=args.weekday, hour=args.hour)
        print(f"Installed {path}\nSafe-tier cleanup will run weekly. Log: ~/.local/state/macsmartcleaner/scheduled.log")
    elif args.action == "remove":
        print("Removed." if schedule.remove(ctx) else "No schedule installed.")
    else:
        p = schedule.plist_path(ctx)
        print(f"installed: {p}" if os.path.exists(p) else "not installed")
    return 0


def cmd_doctor(args, ctx: Context) -> int:
    ok = True
    print(f"python   {sys.version.split()[0]} ({sys.executable})")
    fda_probe = ctx.path("~/Library/Safari")
    try:
        os.listdir(fda_probe)
        print("access   Full Disk Access: yes")
    except PermissionError:
        ok = False
        print("access   Full Disk Access: NO - System Settings > Privacy & Security > Full Disk Access > add your "
              "Terminal app, then restart it. Without it many folders can't be measured.")
    except FileNotFoundError:
        print("access   Full Disk Access: unknown")
    print(f"root     {'yes' if ctx.is_root else 'no (run with sudo to include /Library and /private/var)'}")
    for tool in ("tmutil", "xcrun", "brew", "docker", "npm", "pnpm", "conda", "go"):
        print(f"tool     {tool:<7} {'found' if ctx.which(tool) else '-'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="msc", description=__doc__)
    p.add_argument("--version", action="version", version=f"macsmartcleaner {__version__}")
    p.add_argument("--rules-file", help="extra JSON rules (default ~/.config/macsmartcleaner/rules.json)")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="find what is using space (read-only)")
    s.add_argument("--projects", nargs="*", metavar="DIR", help="folders containing your code projects")
    s.add_argument("--no-projects", action="store_true", help="skip the project artifact search")
    s.add_argument("--no-discover", action="store_true", help="skip the (slower) unknown big-folder search")
    s.add_argument("--min-size", default="50MB", help="hide findings smaller than this (default 50MB)")
    s.add_argument("--hog-min", default="1GB", help="size threshold for unknown big folders (default 1GB)")
    s.add_argument("--json", metavar="FILE")
    s.add_argument("--html", metavar="FILE")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("clean", help="delete junk (asks before deleting)")
    c.add_argument("--tier", choices=["safe", "caution"], default="safe")
    c.add_argument("--only", action="append", metavar="IDS", help="comma-separated rule ids (needed for review items)")
    c.add_argument("--skip", action="append", metavar="IDS", help="comma-separated rule ids to leave alone")
    c.add_argument("--projects", nargs="*", metavar="DIR",
                   help="also remove project build artifacts (optionally only under these folders)")
    c.add_argument("--older-than", type=int, default=30, metavar="DAYS",
                   help="only projects untouched for this many days (default 30)")
    c.add_argument("--min-age", type=int, metavar="DAYS", help="keep cache items used in the last N days")
    c.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, delete nothing")
    c.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    c.add_argument("-i", "--interactive", action="store_true", help="confirm each item")
    c.set_defaults(func=cmd_clean)

    sub.add_parser("rules", help="list all rules").set_defaults(func=cmd_rules)
    e = sub.add_parser("explain", help="explain one rule")
    e.add_argument("rule")
    e.set_defaults(func=cmd_explain)

    t = sub.add_parser("trash", help="safely move folders you reviewed into the Trash")
    t.add_argument("paths", nargs="+")
    t.set_defaults(func=cmd_trash)

    sc = sub.add_parser("schedule", help="weekly automatic safe cleanup (launchd)")
    sc.add_argument("action", choices=["install", "remove", "status"])
    sc.add_argument("--weekday", type=int, default=0, help="0=Sunday ... 6=Saturday")
    sc.add_argument("--hour", type=int, default=11)
    sc.set_defaults(func=cmd_schedule)

    sub.add_parser("doctor", help="check permissions and tools").set_defaults(func=cmd_doctor)
    return p


def main(argv: Optional[Sequence[str]] = None, ctx: Optional[Context] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["scan"] + list(argv or sys.argv[1:]))
    if sys.platform != "darwin" and ctx is None:
        print("warning: macsmartcleaner is built for macOS; results elsewhere are partial.", file=sys.stderr)
    try:
        return args.func(args, ctx or Context())
    except KeyboardInterrupt:
        print("\ninterrupted - nothing further was deleted.", file=sys.stderr)
        return 130
