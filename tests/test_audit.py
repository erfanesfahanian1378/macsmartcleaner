"""Regression tests for the CleanMyMac-parity audit: uninstaller, Space Lens, safety trash mode,
cleaner accounting, Time Machine handler, optimizer handlers."""
import io
import os
import plistlib
import subprocess
import unittest
from contextlib import redirect_stdout

from macsmartcleaner import cleaner, optimize, report, safety, uninstall
from macsmartcleaner.cli import main
from macsmartcleaner.rules import BUILTIN_RULES
from macsmartcleaner.scanner import Finding, scan
from tests.test_macsmartcleaner import FakeMac, fake_runner, write


def rule(rid):
    return next(r for r in BUILTIN_RULES if r.id == rid)


class TestUninstaller(FakeMac):
    def app(self, name, bid, folder="Applications"):
        path = os.path.join(self.root, folder, name + ".app")
        os.makedirs(os.path.join(path, "Contents", "MacOS"), exist_ok=True)
        with open(os.path.join(path, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": bid, "CFBundleName": name, "CFBundleShortVersionString": "1.2"}, fh)
        write(os.path.join(path, "Contents", "MacOS", name), 50_000)
        return path

    def test_lists_third_party_apps_only(self):
        self.app("Figma", "com.figma.Desktop")
        self.app("Safari", "com.apple.Safari")
        self.app("Tool", "io.acme.tool", folder="Applications/Acme")
        names = sorted(a.name for a in uninstall.list_apps(self.ctx))
        self.assertEqual(names, ["Figma", "Tool"])

    def test_finds_related_files_and_uninstalls_to_trash(self):
        path = self.app("Figma", "com.figma.Desktop")
        related = [
            "Library/Application Support/Figma/x", "Library/Caches/com.figma.Desktop/c",
            "Library/Containers/com.figma.Desktop.ShareExt/Data/x", "Library/Preferences/com.figma.Desktop.plist",
            "Library/Saved Application State/com.figma.Desktop.savedState/w", "Library/Logs/Figma/l.log",
            "Library/Group Containers/ABCDE12345.com.figma.Desktop/g",
        ]
        for r in related:
            write(self.h(r), 1000)
        write(self.h("Library/Preferences/com.figma.other.plist"), 10)  # different app of the same vendor: keep
        write(self.h("Library/Application Support/Figmatic/x"), 10)     # similar name: keep
        app = [a for a in uninstall.list_apps(self.ctx) if a.name == "Figma"][0]
        uninstall.collect(app, self.ctx)
        found = sorted(os.path.relpath(p, self.home) for p, _s, _a in app.files)
        self.assertEqual(found, sorted(r.split("/")[0] + "/" + r.split("/")[1] + "/" + r.split("/")[2]
                                       for r in related))
        self.ctx.which = lambda n: None  # no pgrep/osascript/launchctl here
        moved, problems = uninstall.uninstall(app, self.ctx)
        self.assertEqual(problems, [])
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(self.h(".Trash/Figma.app")))
        self.assertTrue(os.path.exists(self.h(".Trash/com.figma.Desktop.plist")))
        self.assertTrue(os.path.exists(self.h("Library/Preferences/com.figma.other.plist")))
        self.assertTrue(os.path.exists(self.h("Library/Application Support/Figmatic/x")))

    def test_copies_sharing_a_bundle_id_keep_their_settings(self):
        self.app("Python Launcher", "org.python.PythonLauncher", folder="Applications/Python 3.12")
        self.app("Python Launcher", "org.python.PythonLauncher", folder="Applications/Python 3.13")
        write(self.h("Library/Preferences/org.python.PythonLauncher.plist"), 100)
        found = sorted(a.name for a in uninstall.list_apps(self.ctx))
        self.assertEqual(found, ["Python Launcher (Python 3.12)", "Python Launcher (Python 3.13)"])
        a = uninstall.list_apps(self.ctx)[0]
        self.assertTrue(a.shared)
        uninstall.collect(a, self.ctx)
        self.assertEqual(a.files, [])

    def test_mdls_dates_are_utc(self):
        d = uninstall._parse_mdls_dates("2025-01-01 00:00:00 +0000\0(null)", 2)
        self.assertEqual(d[0], 1735689600.0)
        self.assertIsNone(d[1])


class TestSafetyTrashMode(FakeMac):
    def test_trash_mode_allows_apps_and_own_prefs_only(self):
        app = os.path.join(self.root, "Applications", "Figma.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.figma.Desktop"}, fh)
        apple = os.path.join(self.root, "Applications", "Safari.app")
        os.makedirs(os.path.join(apple, "Contents"))
        with open(os.path.join(apple, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.apple.Safari"}, fh)
        safety.check(app, self.ctx, trash=True)
        safety.check(self.h("Library/Preferences/com.figma.Desktop.plist"), self.ctx, trash=True)
        for p in [apple, self.h("Library/Preferences/com.apple.finder.plist"), self.h("Library/Preferences"),
                  self.h("Library/Keychains/login.keychain-db")]:
            with self.assertRaises(safety.UnsafePath, msg=p):
                safety.check(p, self.ctx, trash=True)
        with self.assertRaises(safety.UnsafePath):  # not in normal (permanent delete) mode
            safety.check(app, self.ctx)


class TestCleanerAudit(FakeMac):
    def test_logs_keep_their_folders(self):
        write(self.h("Library/Logs/DiagnosticReports/crash.ips"), 5000)
        write(self.h("Library/Logs/SomeApp/app.log"), 5000)
        findings = scan([rule("user-logs")], self.ctx)
        cleaner.execute(findings, self.ctx, dry_run=False)
        self.assertTrue(os.path.isdir(self.h("Library/Logs/DiagnosticReports")))
        self.assertFalse(os.path.exists(self.h("Library/Logs/DiagnosticReports/crash.ips")))
        self.assertFalse(os.path.exists(self.h("Library/Logs/SomeApp/app.log")))

    def test_trash_is_not_counted_as_freed_and_missing_paths_are_quiet(self):
        write(self.h("Downloads/old.dmg"), 5000, age_days=30)
        findings = scan([rule("old-installers")], self.ctx)
        dry = cleaner.execute(findings, self.ctx, dry_run=True)
        self.assertEqual(dry[0].freed, 0)
        self.assertGreater(dry[0].trashed, 0)
        out = cleaner.execute(findings, self.ctx, dry_run=False)
        self.assertEqual((out[0].freed, out[0].messages), (0, []))
        self.assertGreater(out[0].trashed, 0)
        again = cleaner.execute(findings, self.ctx, dry_run=False)  # already gone
        self.assertEqual(again[0].messages, [])

    def test_trash_name_collision_keeps_extension(self):
        write(self.h(".Trash/a.dmg"), 10)
        write(self.h("Documents/a.dmg"), 10)
        dest = cleaner.move_to_trash([self.h("Documents/a.dmg")], self.ctx)[0]
        self.assertTrue(dest.endswith(".dmg") and dest != self.h(".Trash/a.dmg"), dest)

    def test_empty_trash_runs_before_moving_things_to_trash(self):
        order = [f.rule.id for f in cleaner._order([Finding(rule("old-installers"), None, 0),
                                                     Finding(rule("trash"), None, 0)])]
        self.assertEqual(order, ["trash", "old-installers"])

    def test_admin_items_are_marked_not_failed_silently(self):
        f = Finding(rule("rotated-logs"), self.h("x.gz"), 10)
        out = cleaner.execute([f], self.ctx, dry_run=False)
        self.assertTrue(out[0].skipped_root)
        self.assertEqual(cleaner.root_findings([f], self.ctx), [f])


class TestTimeMachine(FakeMac):
    def test_handler_falls_back_to_dates(self):
        state = {"snaps": ["com.apple.TimeMachine.2026-09-20-101010.local"]}

        def run(cmd, timeout):
            c = " ".join(cmd)
            if "listlocalsnapshots" in c:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(state["snaps"]) + "\n", "")
            if "listlocalsnapshotdates" in c:
                return subprocess.CompletedProcess(cmd, 0, "Snapshot dates for all disks:\n2026-09-20-101010\n", "")
            if c.endswith("deletelocalsnapshots /"):
                return subprocess.CompletedProcess(cmd, 1, "", "Usage: tmutil deletelocalsnapshots <date>")
            if "deletelocalsnapshots 2026-09-20-101010" in c:
                state["snaps"] = []
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.ctx.runner = run
        self.ctx.which = lambda n: "/usr/bin/" + n
        ok, msg = rule("tm-snapshots").handler(self.ctx, False)
        self.assertTrue(ok, msg)
        self.assertIn("deleted 1", msg)

    def test_report_note_comes_from_time_machine_only(self):
        spot = Finding(rule("spotlight-index"), "/x", 0, size_known=False, note="run with sudo to measure")
        buf = io.StringIO()
        report.print_report(self.ctx, [spot], [], [], min_size=0, out=buf)
        self.assertNotIn("Time Machine:", buf.getvalue())


class TestOptimizeAudit(FakeMac):
    def test_mail_refuses_while_open_and_vacuums(self):
        task = next(t for t in optimize.TASKS if t.id == "mail")
        db = self.h("Library/Mail/V10/MailData/Envelope Index")
        write(db, 10000)
        self.ctx.which = lambda n: "/usr/bin/" + n
        self.ctx.runner = fake_runner({})
        self.ctx.runner = lambda cmd, t: subprocess.CompletedProcess(cmd, 0 if cmd[0] == "pgrep" else 0, "", "")
        ok, msg = task.handler(self.ctx)
        self.assertFalse(ok)
        self.assertIn("Quit Mail", msg)
        calls = []
        self.ctx.runner = lambda cmd, t: (calls.append(cmd), subprocess.CompletedProcess(
            cmd, 1 if cmd[0] == "pgrep" else 0, "", ""))[1]
        ok, msg = task.handler(self.ctx)
        self.assertTrue(ok, msg)
        self.assertIn(["sqlite3", db, "VACUUM;"], calls)
        self.assertIn("mail", [t.id for t in optimize.available(self.ctx)])


class TestLensCLI(FakeMac):
    def test_list_biggest_first(self):
        write(self.h("Movies/a.mov"), 300_000)
        write(self.h("Documents/b.pdf"), 100_000)
        out = io.StringIO()
        with redirect_stdout(out):
            main(["lens", self.home, "--list"], ctx=self.ctx)
        lines = [ln.split()[-1] for ln in out.getvalue().splitlines()]
        self.assertEqual(lines[:2], ["Movies/", "Documents/"])


if __name__ == "__main__":
    unittest.main()


class TestQuitProtection(unittest.TestCase):
    def test_core_processes_are_refused_before_any_prompt(self):
        from macsmartcleaner.app import StatusScreen
        screen = StatusScreen(monitor=None)
        for name, pid in (("WindowServer", 400), ("kernel_task", 0), ("loginwindow", 123), ("anything", 1),
                          ("python", os.getpid())):
            screen.quit_process({"name": name, "pid": pid}, force=True)
            self.assertIn("part of macOS", screen.flash, name)


class TestLeftoverAppleIds(unittest.TestCase):
    """Real false positives seen on a macOS 26 machine."""

    def test_apple_group_containers_are_recognised(self):
        from macsmartcleaner.apps import bundle_id_of, is_apple
        for name in ("243LU875E5.groups.com.apple.podcasts", "group.is.workflow.my.app",
                     "group.is.workflow.shortcuts", "systemgroup.com.apple.configurationprofiles",
                     "group.com.apple.notes", "com.apple.Safari.savedState"):
            bid = bundle_id_of(name)
            self.assertTrue(bid is None or is_apple(bid), name)
        self.assertFalse(is_apple(bundle_id_of("ABCDE12345.com.figma.Desktop")))

    def test_spotlight_second_opinion_clears_candidates(self):
        import unittest.mock
        from macsmartcleaner import rules as rules_mod
        from macsmartcleaner.context import Context
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ctx = Context(home=d, root=d, is_root=False, sudo_user=None)
            for i in range(6):
                app = os.path.join(d, "Applications", f"A{i}.app", "Contents")
                os.makedirs(app)
                with open(os.path.join(app, "Info.plist"), "wb") as fh:
                    plistlib.dump({"CFBundleIdentifier": f"com.v{i}.app"}, fh)
            os.makedirs(os.path.join(d, "Library", "Containers", "com.helper.tool"))
            os.makedirs(os.path.join(d, "Library", "Containers", "com.gone.app"))
            with unittest.mock.patch("macsmartcleaner.apps.installed_somewhere",
                                     side_effect=lambda bid, c: bid == "com.helper.tool"):
                roots = rules_mod._probe_leftovers(ctx).roots
            self.assertEqual([os.path.basename(r) for r in roots], ["com.gone.app"])


class TestAdminStep(FakeMac):
    def test_admin_rerun_command_targets_exactly_the_chosen_items(self):
        import unittest.mock
        items = [Finding(rule("rotated-logs"), "/private/var/log/a.gz", 10),
                 Finding(rule("system-logs"), "/Library/Logs", 20)]
        calls = []
        with unittest.mock.patch("subprocess.call", side_effect=lambda cmd, **kw: calls.append(cmd) or 0), \
                unittest.mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertTrue(cleaner.clean_as_admin(items, self.ctx))
        self.assertEqual(calls[0], ["sudo", "-v"])
        cmd = calls[1]
        self.assertEqual(cmd[:3], ["sudo", "-n", "env"])
        self.assertIn("clean", cmd)
        self.assertEqual(cmd[cmd.index("--only") + 1], "rotated-logs,system-logs")
        roots = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--root"]
        self.assertEqual(roots, ["/private/var/log/a.gz", "/Library/Logs"])

    def test_no_password_means_nothing_runs(self):
        import unittest.mock
        with unittest.mock.patch("subprocess.call", return_value=1) as call, \
                unittest.mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertFalse(cleaner.clean_as_admin([Finding(rule("core-dumps"), "/cores/core.1", 5)], self.ctx))
        self.assertEqual(call.call_count, 1)  # only `sudo -v`


class TestAerials(FakeMac):
    def test_videos_removed_catalogue_and_folders_kept(self):
        base = os.path.join(self.root, "Library/Application Support/com.apple.idleassetsd/Customer")
        write(os.path.join(base, "4KSDR240FPS", "A1B2.mov"), 300_000)
        write(os.path.join(base, "entries.json"), 500)
        self.ctx.is_root = True  # the rule needs admin rights
        findings = scan([rule("aerial-videos")], self.ctx)
        self.assertEqual([os.path.basename(t.path) for f in findings for t in f.targets], ["4KSDR240FPS"])
        cleaner.execute(findings, self.ctx, dry_run=False)
        self.assertFalse(os.path.exists(os.path.join(base, "4KSDR240FPS", "A1B2.mov")))
        self.assertTrue(os.path.isdir(os.path.join(base, "4KSDR240FPS")))
        self.assertTrue(os.path.exists(os.path.join(base, "entries.json")))
