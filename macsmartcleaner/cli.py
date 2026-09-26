"""msc - find and clean what's inflating macOS "System Data"."""
from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import sys
from typing import List, Optional, Sequence

from . import __version__, cleaner, discover, report, schedule, sizes, ui
from .context import Context
from .rules import Action, Rule, default_user_rules_path, load_rules
from .scanner import Finding, scan
from .sizes import human, parse_size

def full_scan(ctx: Context, args, projects: bool = True, hogs: bool = True, hog_min: str = "1GB"):
    """Run every scan stage behind one live progress display."""
    # weights = rough share of total scan time, used for the overall percentage
    stages = [("probe", 3), ("rules", 40)]
    if projects:
        stages.append(("projects", 17))
    if hogs:
        stages += [("hogs", 35), ("large", 5)]
    if not args.quiet:
        ui.banner("scanning your Mac - nothing is deleted during a scan")
    found_projects: List[Finding] = []
    found_hogs: list = []
    with ui.make_reporter(stages, quiet=args.quiet, counter_label="scanned") as rep, sizes.cache_session():
        findings = scan(_rules(ctx, args), ctx, reporter=rep)
        if projects:
            found_projects = discover.find_project_artifacts(ctx, getattr(args, "projects", None) or None,
                                                              reporter=rep)
        if hogs:
            found_hogs = discover.find_space_hogs(ctx, findings + found_projects, min_size=parse_size(hog_min),
                                                  reporter=rep)
            found_hogs += discover.find_large_files(ctx, findings + found_projects, reporter=rep)
    return findings, found_projects, found_hogs


def interactive_ok() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


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
    findings, projects, hogs = full_scan(ctx, args, projects=not args.no_projects, hogs=not args.no_discover,
                                         hog_min=args.hog_min)
    data = report.to_dict(ctx, findings, hogs, projects)
    if not args.report and interactive_ok():
        from . import tui
        for path, writer in ((args.json, report.write_json), (args.html, report.write_html)):
            if path:
                writer(path, data)
        return tui.run(ctx, findings, projects, hogs, rescan=lambda: full_scan(
            ctx, args, projects=not args.no_projects, hogs=not args.no_discover, hog_min=args.hog_min))
    report.print_report(ctx, findings, hogs, projects, min_size=parse_size(args.min_size))
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
    only, skip = _split(args.only), _split(args.skip)
    want_projects = args.projects is not None or any(o.startswith("project") for o in only)
    findings, projects, _ = full_scan(ctx, args, projects=want_projects, hogs=False)
    chosen = cleaner.select(findings, tier=args.tier, only=[o for o in only if not o.startswith("project")],
                            skip=skip)
    if want_projects:
        projects = [p for p in projects if ctx.days_since(p.newest_mtime) >= args.older_than]
        if only and not any(o in ("project", "projects", "project:*") for o in only):
            projects = [p for p in projects if p.rule.id in only]
        chosen += [p for p in projects if p.rule.id not in skip]
    if args.root:  # exact items (used by the admin re-run so it cleans only what you picked)
        wanted = {os.path.normpath(r) for r in args.root}
        chosen = [f for f in chosen if f.root and os.path.normpath(f.root) in wanted]

    if not chosen:
        print("Nothing to clean for this selection. Run `msc scan` to see everything.")
        return 0

    admin = cleaner.root_findings(chosen, ctx)
    print(f"\nPlan ({'dry run' if args.dry_run else 'will delete'}):\n")
    total = trash_total = 0
    for f in chosen:
        where = discover._display(ctx, f.root) if f.root else f.note
        tags = (["admin"] if f in admin or (f.rule.command_needs_root and not ctx.is_root) else []) + \
               (["to Trash"] if f.rule.trash else [])
        print(f"  {report.size_str(f):>9}  {f.rule.safety.value:<7}  {f.rule.id:<24} {where}"
              + (report.c(f"  [{', '.join(tags)}]", "2") if tags else ""))
        if f.rule.trash:
            trash_total += f.size
        else:
            total += f.size
    print(f"\n  Total: {human(total)}" + (f" + {human(trash_total)} moved to the Trash" if trash_total else "")
          + ("  (+ Time Machine snapshots, size unknown)" if any(not f.size_known for f in chosen) else ""))
    if admin and not args.dry_run:
        print(report.c(f"  {len(admin)} system item(s) need your Mac password - you'll be asked once.", "2"))

    busy = cleaner.running_apps(sorted({a for f in chosen for a in f.rule.quit_apps}), ctx)
    if busy:
        print(report.c(f"\n  Quit these apps first for best results: {', '.join(busy)}", "33"))

    if args.interactive and not args.dry_run:
        chosen = [f for f in chosen if _confirm(
            f"  clean {f.rule.id} {report.size_str(f)} ({discover._display(ctx, f.root) if f.root else f.note})? [y/N] ")]
    elif not args.dry_run and not args.yes and not _confirm(f"\nClean {human(total + trash_total)} now? [y/N] "):
        print("Aborted. Nothing was deleted.")
        return 1

    if not args.dry_run and not ctx.is_root and any(f.rule.command_needs_root for f in chosen):
        if not sys.stdin.isatty() or os.system("sudo -v") != 0:  # ask for the password before the animation
            print("Skipping items that need your password (run interactively to include them).")
            chosen = [f for f in chosen if not f.rule.command_needs_root]

    before = shutil.disk_usage(ctx.home).free
    print()
    user_part = [f for f in chosen if f not in admin] if not args.dry_run else chosen
    with ui.make_reporter([("clean", 1)], quiet=args.quiet, counter_label="freed") as rep:
        outcomes = cleaner.execute(user_part, ctx, dry_run=args.dry_run,
                                   log=None if args.quiet or ui.color_enabled() else print, reporter=rep)
    ran_admin = False
    if admin and not args.dry_run:
        print(report.c("\n  Cleaning system items (admin)...", "1"))
        ran_admin = cleaner.clean_as_admin(admin, ctx, quiet=True)
        if not ran_admin:
            print(report.c("  System items skipped (no password given or not in a terminal).", "33"))
    after = shutil.disk_usage(ctx.home).free

    print()
    for o in outcomes:
        status = report.c("ok", "32") if o.ok else report.c("!!", "33")
        amount = human(o.freed) + (f" (+{human(o.trashed)} to Trash)" if o.trashed else "")
        print(f"  {status}  {o.finding.rule.id:<24} {amount:>9}")
        for m in o.notes:
            print(report.c(f"        {m}", "2"))
        for m in o.messages:
            print(report.c(f"        {m}", "33"))
    freed = sum(o.freed for o in outcomes)
    trashed = sum(o.trashed for o in outcomes)
    if args.dry_run:
        print(f"\nDry run: would free about {human(freed)}"
              + (f" and move {human(trashed)} to the Trash" if trashed else "") + ". Re-run without --dry-run to do it.")
    else:
        print(f"\nFreed about {human(freed)}" + (" (plus the system items above)" if ran_admin else "")
              + f". Free space: {human(before)} -> {human(after)}"
              + (f"\nMoved {human(trashed)} to the Trash - empty it to free that space too." if trashed else "")
              + ("\n(APFS may take a minute to release space from snapshots/purgeable files)"
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

def cmd_menu(args, ctx: Context) -> int:
    from . import app, smart

    scan_args = build_parser().parse_args(["scan"])
    scan_args.quiet = args.quiet
    scan_args.rules_file = args.rules_file
    doctor_args = build_parser().parse_args(["spotlight"])
    doctor_args.quiet = True

    def doctor() -> int:
        diag_args = build_parser().parse_args(["diagnose"])
        diag_args.quiet = False
        cmd_diagnose(diag_args, ctx)
        return cmd_spotlight(doctor_args, ctx)

    return app.run(ctx, deep_scan=lambda: cmd_scan(scan_args, ctx),
                   smart_clean=lambda: smart.run(ctx, _rules(ctx, args)), doctor=doctor)


def cmd_smart(args, ctx: Context) -> int:
    from . import smart
    return smart.run(ctx, _rules(ctx, args), assume_yes=args.yes, quiet=args.quiet, dry_run=args.dry_run,
                     empty_trash=args.empty_trash)


def cmd_status(args, ctx: Context) -> int:
    from . import sysinfo
    mon = sysinfo.Monitor(ctx.home)
    if args.once or not interactive_ok():
        mon.sample()
        import time as _t
        _t.sleep(1)
        mon.sample()
        s, m = mon.snapshot(), mon.machine
        mem = s.mem
        print(f"{m.model or ''} {m.chip} · {m.os_name} · up {sysinfo.fmt_uptime(m.boot_time)}".strip())
        print(f"CPU      {(s.cpu_total or 0) * 100:5.1f}%   load {' '.join(f'{v:.2f}' for v in s.load)}")
        if mem.get("total"):
            print(f"Memory   {mem['used'] / 1e9:5.1f} of {mem['total'] / 1e9:.0f} GB   pressure {s.pressure_level}"
                  f"   swap {s.swap_used / 1e9:.1f} GB")
        if s.gpu.get("util") is not None:
            print(f"GPU      {s.gpu['util'] * 100:5.1f}%")  # type: ignore[operator]
        print(f"Disk     {human(s.disk_used)} used, {human(s.disk_free)} free")
        print(f"Network  down {sysinfo.fmt_rate(s.net_rx_bps).strip()}  up {sysinfo.fmt_rate(s.net_tx_bps).strip()}")
        if s.battery:
            print(f"Battery  {s.battery['percent']}% {s.battery['state']}")
        print(f"Thermal  {s.thermal}")
        return 0
    import curses
    from . import app
    mon.start()
    try:
        curses.wrapper(app.StatusScreen(mon).loop)
    finally:
        mon.stop()
    return 0


def cmd_startup(args, ctx: Context) -> int:
    from . import startup
    if args.list or not interactive_ok():
        for it in startup.scan(ctx):
            state = "broken" if it.broken else "enabled" if it.enabled else "disabled"
            print(f"{state:<9} {it.kind_label:<14} {it.name:<30} {it.label}")
        return 0
    import curses
    from . import app
    screen = app.StartupScreen(ctx)
    while curses.wrapper(screen.loop) == "sudo":
        app.run_pending_startup(screen, ctx)
    return 0


def cmd_optimize(args, ctx: Context) -> int:
    from . import app, optimize
    tasks = optimize.available(ctx)
    if args.list:
        for t in tasks:
            print(f"{t.id:<15} {'admin' if t.needs_root else '':<6} {'recommended' if t.recommended else '':<12} {t.name}")
        return 0
    if args.run:
        wanted = set(_split(args.run))
        chosen = [t for t in tasks if t.id in wanted or ("recommended" in wanted and t.recommended)]
        if not chosen:
            print("No matching tasks. See `msc optimize --list`.")
            return 2
        app.run_optimize(chosen, ctx)
        return 0
    if not interactive_ok():
        print("Use `msc optimize --list` and `msc optimize --run ID,ID` when not in a terminal.")
        return 2
    import curses
    screen = app.OptimizeScreen(ctx)
    if curses.wrapper(screen.loop) == "run":
        app.run_optimize(screen.chosen(), ctx)
    return 0


def cmd_lens(args, ctx: Context) -> int:
    import curses
    from . import apptools
    start = os.path.abspath(os.path.expanduser(args.path)) if args.path else None
    if args.list or not interactive_ok():
        path = start or ctx.home
        with sizes.cache_session():
            names = sorted(os.listdir(path))
            full = [os.path.join(path, n) for n in names]
            got = sizes.measure_many(full)
        for p in sorted(full, key=lambda p: -got[p].bytes)[: args.top]:
            print(f"{human(got[p].bytes):>9}  {os.path.basename(p)}{'/' if os.path.isdir(p) else ''}")
        return 0
    curses.wrapper(apptools.LensScreen(ctx, start).loop)
    return 0


def cmd_uninstall(args, ctx: Context) -> int:
    from . import apptools, uninstall
    apps = uninstall.list_apps(ctx)
    if args.list or (not args.apps and not interactive_ok()):
        with sizes.cache_session():
            uninstall.measure_apps(apps)
        uninstall.load_last_used(apps, ctx)
        for a in sorted(apps, key=lambda a: -a.size):
            print(f"{human(a.size):>9}  {uninstall.human_age(a.last_used):<10} {a.name:<32} {a.bundle_id}")
        return 0
    if args.apps:
        wanted = {n.lower() for n in args.apps}
        chosen = [a for a in apps if a.name.lower() in wanted or a.bundle_id.lower() in wanted
                  or os.path.splitext(os.path.basename(a.path))[0].lower() in wanted]
        if not chosen:
            print("No matching app. See `msc uninstall --list`.")
            return 2
        with sizes.cache_session():
            uninstall.measure_apps(chosen)
            for a in chosen:
                uninstall.collect(a, ctx)
        for a in chosen:
            print(f"\n{a.name} ({a.bundle_id}) - {human(a.total)} total")
            print(f"  {human(a.size):>9}  {a.path}")
            for p, size, admin in a.files:
                print(f"  {human(size):>9}  {p}" + ("  [admin]" if admin else ""))
        if args.dry_run:
            return 0
        if not args.yes and not _confirm(f"\nMove {len(chosen)} app(s) and their files to the Trash? [y/N] "):
            print("Nothing was removed.")
            return 1
        apptools.run_uninstall(chosen, ctx)
        return 0
    import curses
    un = apptools.UninstallScreen(ctx)
    while curses.wrapper(un.loop) == "uninstall":
        chosen = un.chosen()
        apptools.run_uninstall(chosen, ctx)
        gone = {a.path for a in chosen if not os.path.exists(a.path)}
        un.apps = [a for a in un.apps if a.path not in gone]
        un.selected -= gone
        try:
            input("\n  Press Enter to go back to the app list ")
        except (EOFError, KeyboardInterrupt):
            break
    return 0


def _c(text: str, code: str) -> str:
    return report.c(text, code)


def cmd_spotlight(args, ctx: Context) -> int:
    from . import spotlight
    if args.guard_check:
        _ok, msg = spotlight.guard_check(ctx, parse_size(args.max))
        print(msg)
        return 0
    actions = []
    if args.exclude or args.include:
        actions.append(spotlight.set_exclusions(ctx, add=args.exclude or [], remove=args.include or []))
    if args.off:
        actions.append(spotlight.set_indexing(ctx, on=False))
    if args.on:
        actions.append(spotlight.set_indexing(ctx, on=True))
    if args.guard:
        actions.append(spotlight.install_guard(ctx, parse_size(args.guard)))
    if args.no_guard:
        actions.append(spotlight.remove_guard(ctx))

    if not args.quiet:
        ui.banner("spotlight doctor - why is the search index so big?")
    excluded = spotlight.exclusions(ctx)
    with ui.Spinner("Measuring the Spotlight index and the usual suspects", quiet=args.quiet) as spin, \
            sizes.cache_session():
        size = spotlight.index_size(ctx)
        parts = spotlight.store_breakdown(ctx) if size else []
        user_idx = spotlight.user_index_size(ctx)
        suspects = spotlight.find_suspects(ctx, excluded or [])
        spin.done(True, "")

    print(f"\n  Indexing:            {spotlight.status(ctx)}")
    if size is None:
        print(f"  System index:        {_c('? - run with sudo to measure', '33')}")
    else:
        flag = "31;1" if size > 50e9 else "33" if size > 10e9 else "32"
        verdict = ("runaway - Spotlight is stuck re-indexing something" if size > 50e9 else
                   "too big" if size > 10e9 else "normal")
        print(f"  System index:        {_c(human(size), flag)}  ({verdict})")
        for name, b in parts[:6]:
            if b > 100e6:
                print(f"      {human(b):>9}  {name}")
    print(f"  Per-user index:      {human(user_idx)}  (~/Library/Metadata/CoreSpotlight)")
    if excluded is None:
        print(f"  Excluded folders:    {_c('? - run with sudo to read', '33')}")
    else:
        print(f"  Excluded folders:    {len(excluded)}" + ("" if not excluded else ""))
        for e in excluded[:10]:
            print(f"      {e}")

    todo = [s for s in suspects if not s.excluded]
    if suspects:
        print("\n  " + _c("Folders that typically make Spotlight loop:", "1"))
        for s in suspects:
            mark = _c("excluded", "32") if s.excluded else _c("INDEXED ", "33")
            print(f"    {mark}  {human(s.bytes):>9}  {s.files:>10,} files  {discover._display(ctx, s.path)}")
            print(_c(f"                                              {s.reason}", "2"))

    if args.watch:
        print()
        with ui.Spinner(f"Watching what Spotlight reads for {args.watch} s (keep using your Mac normally)",
                        quiet=args.quiet) as spin:
            ok, top, msg = spotlight.watch(ctx, args.watch)
            spin.done(ok, msg)
        if ok:
            if top:
                print("\n  " + _c("Where Spotlight spent its time (file accesses):", "1"))
                for folder, n in top:
                    print(f"    {n:>7,}  {folder}")
                print(_c("  The folder at the top is your culprit if it's something you never search in.", "2"))
            else:
                print("  Spotlight was idle - run again while the index is growing.")

    for ok, msg in actions:
        print(("  " + _c("✔", "32") if ok else "  " + _c("✖", "31")) + " " + msg)

    if args.auto_exclude:
        if not todo:
            print("\n  Nothing to exclude - all suspects are already excluded.")
        else:
            ok, msg = spotlight.set_exclusions(ctx, add=[s.path for s in todo])
            print(("  " + _c("✔", "32") if ok else "  " + _c("✖", "31")) + " " + msg)
            if ok and size and size > 10e9:
                ok2, msg2 = spotlight.rebuild(ctx)
                print(("  " + _c("✔", "32") if ok2 else "  " + _c("✖", "31")) + " " + msg2)
    if args.rebuild:
        ok, msg = spotlight.rebuild(ctx)
        print(("  " + _c("✔", "32") if ok else "  " + _c("✖", "31")) + " " + msg)

    if not (args.auto_exclude or args.exclude or args.rebuild or args.watch or args.guard):
        print("\n  " + _c("What to do", "1;4"))
        steps = ["`sudo msc spotlight --watch 60` while the index is growing shows which folders it reads"]
        if todo:
            steps.append(f"`sudo msc spotlight --auto-exclude` stops indexing the {len(todo)} folder(s) marked INDEXED "
                         "(they still open normally, they just don't appear in Spotlight search) and rebuilds once")
        steps.append("`sudo msc spotlight --guard 20GB` rebuilds automatically if the index ever passes 20 GB again")
        for i, step in enumerate(steps, 1):
            print(f"    {i}. {step}")
        print("    Last resort: `sudo msc spotlight --off` turns Spotlight indexing off (search and Mail search stop working)")
    return 0


def cmd_diagnose(args, ctx: Context) -> int:
    from . import diagnose
    if not args.quiet:
        ui.banner("system data breakdown")
    progress = [0, 0]

    def on_dir(_d, f, b):
        progress[0] += f
        progress[1] += b

    with ui.Spinner("Measuring everything that ends up in 'System Data'", quiet=args.quiet) as spin, \
            sizes.cache_session():
        rows = diagnose.measure_pieces(ctx, on_dir)
        big = diagnose.biggest_children([ctx.path(p) for p in ("/Library/Application Support", "/private/var/db",
                                                               "~/Library", "/Library")], on_dir=on_dir)
        spin.done(True, f"{progress[0]:,} files, {human(progress[1])}")
    cap, free, vols = diagnose.apfs_volumes(ctx)
    snaps = diagnose.snapshots(ctx)

    if vols:
        print("\n  " + _c("Your disk (APFS container)", "1;4") + f"   {human(cap)} total, {human(free)} free")
        for name, roles, used in vols:
            hint = {"Data": "your files + most System Data", "System": "macOS itself (read-only)",
                    "VM": "swap", "Update": "staged macOS update - large = stuck update",
                    "Preboot": "boot files", "Recovery": "recovery system"}.get(roles.split(",")[0], "")
            flag = "33" if roles.startswith("Update") and used > 5e9 else "0"
            print(f"    {_c(f'{human(used):>9}', flag)}  {name:<28} {_c(hint, '2')}")
    tm = [s for s in snaps if "com.apple.TimeMachine" in s]
    other = [s for s in snaps if s not in tm]
    print(f"\n  Snapshots on your data volume: {len(snaps)}  "
          f"({len(tm)} Time Machine, {len(other)} other)" if snaps is not None else "")
    for s in other[:8]:
        print(_c(f"    {s}", "2"))
    if snaps:
        print(_c("    Snapshot sizes are hidden by APFS; they hold deleted/changed data. "
                 "Time Machine ones: `msc clean --only tm-snapshots`.", "2"))
        if other:
            print(_c("    Others belong to backup apps (e.g. Carbon Copy Cloner) - remove them in that app.", "2"))

    print("\n  " + _c("Pieces of System Data", "1;4"))
    known = 0
    for r in sorted(rows, key=lambda r: -(r.usage.bytes if r.usage else -1)):
        if r.usage is None or not r.usage.exists:
            continue
        unreadable = r.usage.errors and not ctx.is_root
        size = human(r.usage.bytes) + ("+" if unreadable else "")
        known += r.usage.bytes
        print(f"    {size:>10}  {r.label:<38} {_c(r.advice, '2')}")
    print(_c(f"    {human(known):>10}  total measured" + ("  (+ = partly hidden: run with sudo)" if not ctx.is_root else ""),
             "1"))

    for parent, kids in big.items():
        if kids and kids[0][1] > 1e9:
            print("\n  " + _c(f"Biggest in {discover._display(ctx, parent)}", "1"))
            for name, b in kids:
                if b > 200e6:
                    print(f"    {human(b):>10}  {name}")
    print("\n  Next: `sudo msc lens /` to browse the whole disk by size, `sudo msc spotlight` for the index.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="msc", description=__doc__,
                                epilog="Run `msc` with no arguments for the interactive menu.")
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
    s.add_argument("--report", action="store_true", help="print a text report instead of the interactive browser")
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
    c.add_argument("--root", action="append", metavar="PATH", help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_clean)

    sub.add_parser("menu", help="interactive home screen (default)").set_defaults(func=cmd_menu)

    sm = sub.add_parser("smart", help="Smart Clean: clear caches & junk, skipping apps you have open")
    sm.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sm.add_argument("-n", "--dry-run", action="store_true", help="show what would be cleaned")
    sm.add_argument("--empty-trash", action="store_true", help="also empty the Trash (asked otherwise)")
    sm.set_defaults(func=cmd_smart)

    st = sub.add_parser("status", help="live system status: CPU, GPU, memory, disk, network, battery")
    st.add_argument("--once", action="store_true", help="print one snapshot instead of the live view")
    st.set_defaults(func=cmd_status)

    le = sub.add_parser("lens", help="Space Lens: browse any folder by size (default: your home folder)")
    le.add_argument("path", nargs="?", help="folder to start in; `/` shows the whole disk")
    le.add_argument("--list", action="store_true", help="print the biggest items instead of the browser")
    le.add_argument("--top", type=int, default=30, help="how many items --list prints")
    le.set_defaults(func=cmd_lens)

    un = sub.add_parser("uninstall", help="remove apps together with all their leftovers")
    un.add_argument("apps", nargs="*", metavar="APP", help="app name or bundle id (omit for the interactive list)")
    un.add_argument("--list", action="store_true", help="list apps with size and last use")
    un.add_argument("-n", "--dry-run", action="store_true", help="show what would be removed")
    un.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    un.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("spotlight", help="Spotlight Doctor: why the search index is huge, and fix it")
    sp.add_argument("--watch", type=int, metavar="SECONDS", help="record which folders Spotlight reads (sudo)")
    sp.add_argument("--exclude", action="append", metavar="FOLDER", help="stop indexing a folder (sudo)")
    sp.add_argument("--include", action="append", metavar="FOLDER", help="index a folder again (sudo)")
    sp.add_argument("--auto-exclude", action="store_true", help="exclude all loop suspects, then rebuild (sudo)")
    sp.add_argument("--rebuild", action="store_true", help="erase and rebuild the index (sudo)")
    sp.add_argument("--guard", metavar="SIZE", help="hourly check: rebuild if the index passes SIZE, e.g. 20GB")
    sp.add_argument("--no-guard", action="store_true", help="remove the guard")
    sp.add_argument("--off", action="store_true", help="turn Spotlight indexing off (last resort)")
    sp.add_argument("--on", action="store_true", help="turn Spotlight indexing back on")
    sp.add_argument("--guard-check", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--max", default="20GB", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_spotlight)

    dg = sub.add_parser("diagnose", help="System Data breakdown: volumes, snapshots, indexes, swap, logs")
    dg.set_defaults(func=cmd_diagnose)

    su = sub.add_parser("startup", help="see and disable apps that start automatically")
    su.add_argument("--list", action="store_true", help="print the list instead of the interactive view")
    su.set_defaults(func=cmd_startup)

    op = sub.add_parser("optimize", help="maintenance tasks: DNS, memory, Finder/Dock, Spotlight...")
    op.add_argument("--list", action="store_true", help="list available tasks")
    op.add_argument("--run", action="append", metavar="IDS", help="run tasks by id ('recommended' for the defaults)")
    op.set_defaults(func=cmd_optimize)

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
        default = "menu" if interactive_ok() else "scan"
        args = parser.parse_args([default] + list(argv if argv is not None else sys.argv[1:]))
    if sys.platform != "darwin" and ctx is None:
        print("warning: macsmartcleaner is built for macOS; results elsewhere are partial.", file=sys.stderr)
    try:
        return args.func(args, ctx or Context())
    except KeyboardInterrupt:
        print("\ninterrupted - nothing further was deleted.", file=sys.stderr)
        return 130
