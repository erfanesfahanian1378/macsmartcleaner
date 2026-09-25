"""Tests for the system-status parsers, startup items, Smart Clean and the optimizer."""
import io
import os
import plistlib
import unittest
from contextlib import redirect_stdout

from macsmartcleaner import optimize, smart, startup, sysinfo
from macsmartcleaner.rules import BUILTIN_RULES
from macsmartcleaner.scanner import scan
from tests.test_macsmartcleaner import FakeMac, fake_runner, write

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               12345.
Pages active:                            300000.
Pages inactive:                          290000.
Pages speculative:                         5000.
Pages throttled:                              0.
Pages wired down:                        150000.
Pages purgeable:                          10000.
"Translation faults":                 123456789.
Pages copy-on-write:                    1234567.
Pages zero filled:                    987654321.
Pages reactivated:                       123456.
Pages purged:                             54321.
File-backed pages:                       250000.
Anonymous pages:                         340000.
Pages stored in compressor:              200000.
Pages occupied by compressor:             60000.
"""

IOREG = '''+-o AGXAcceleratorG14X  <class AGXAcceleratorG14X, id 0x1000003f1>
    {
      "gpu-core-count" = 19
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=1234,"Tiler Utilization %"=7,"Renderer Utilization %"=11,"Device Utilization %"=12,"In use system memory"=987654321}
    }
'''

NETSTAT = """Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                         100     0      50000      100     0      50000     0
lo0        16384 127           localhost          100     -      50000      100     -      50000     -
en0        1500  <Link#11>   aa:bb:cc:dd:ee:ff   5000     0    7000000     3000     0    1000000     0
en0        1500  192.168.1     192.168.1.20      5000     -    7000000     3000     -    1000000     -
utun0      1380  <Link#16>                         10     0       1000       10     0       1000     0
en1        1500  <Link#12>   11:22:33:44:55:66    200     0     300000      100     0      20000     0
"""


class TestParsers(unittest.TestCase):
    def test_vm_stat_and_breakdown(self):
        vm = sysinfo.parse_vm_stat(VM_STAT)
        self.assertEqual(vm["Pages wired down"], 150000 * 16384)
        mem = sysinfo.memory_breakdown(vm, 16 * 1024 ** 3)
        self.assertEqual(mem["app"], (340000 - 10000) * 16384)
        self.assertEqual(mem["used"], mem["app"] + mem["wired"] + mem["compressed"])
        self.assertLess(mem["used"], mem["total"])

    def test_swap(self):
        total, used = sysinfo.parse_swapusage("total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)")
        self.assertEqual(total, 2048 * 1024 ** 2)
        self.assertEqual(used, int(1024.5 * 1024 ** 2))

    def test_gpu(self):
        g = sysinfo.parse_ioreg_gpu(IOREG)
        self.assertAlmostEqual(g["util"], 0.12)
        self.assertEqual(g["cores"], 19)
        self.assertEqual(g["memory"], 987654321)
        self.assertIsNone(sysinfo.parse_ioreg_gpu("")["util"])
        amd = '"PerformanceStatistics" = {"GPU Activity(%)"=37,"hardwareWaitTime"=0}'
        self.assertAlmostEqual(sysinfo.parse_ioreg_gpu(amd)["util"], 0.37)

    def test_battery_and_thermal(self):
        b = sysinfo.parse_pmset_batt("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t87%; charging; "
                                     "1:02 remaining present: true\n")
        self.assertEqual((b["percent"], b["state"], b["remaining"], b["source"]), (87, "charging", "1:02", "AC Power"))
        self.assertIsNone(sysinfo.parse_pmset_batt("Now drawing from 'AC Power'\n"))  # desktop Mac
        self.assertEqual(sysinfo.parse_pmset_therm("Note: No thermal warning level has been recorded\n"), "nominal")
        self.assertEqual(sysinfo.parse_pmset_therm("CPU_Speed_Limit \t= 70\n"), "throttled to 70%")

    def test_ps_netstat_iostat(self):
        procs = sysinfo.parse_ps("  PID  %CPU    RSS COMM\n  123  45.5 204800 Google Chrome Helper\n  1 0.0 10 launchd\n")
        self.assertEqual(procs[0]["name"], "Google Chrome Helper")
        self.assertEqual(procs[0]["rss"], 204800 * 1024)
        self.assertEqual(sysinfo.parse_netstat_ib(NETSTAT), (7300000, 1020000))
        io_ = "              disk0               disk4 \n    KB/t  xfrs   MB     KB/t  xfrs   MB \n   23.10 1000 2000.5   10.0 5 0.5\n"
        self.assertAlmostEqual(sysinfo.parse_iostat_total_mb(io_), 2001.0)

    def test_monitor_samples(self):
        mon = sysinfo.Monitor()
        mon.sample()
        mon.sample()
        snap = mon.snapshot()
        self.assertTrue(snap.disk_total > 0)
        self.assertIsInstance(sysinfo.fmt_rate(1500.0), str)


class TestStartup(FakeMac):
    def agent(self, label, program, folder="Library/LaunchAgents"):
        path = self.h(folder, label + ".plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump({"Label": label, "ProgramArguments": [program], "RunAtLoad": True}, fh)
        return path

    def test_scan_flags_broken_and_disabled(self):
        existing = self.h("bin/tool")
        write(existing, 10)
        self.agent("com.acme.helper", existing)
        self.agent("com.gone.agent", "/Applications/Gone.app/Contents/MacOS/gone")
        self.ctx.which = lambda n: "/bin/" + n
        self.ctx.runner = fake_runner({
            "launchctl print-disabled gui/" + str(os.getuid()): '\tdisabled services = {\n\t\t"com.acme.helper" => disabled\n\t}\n',
            "launchctl list": "PID\tStatus\tLabel\n-\t0\tcom.acme.helper\n",
        })
        items = {i.label: i for i in startup.scan(self.ctx)}
        self.assertTrue(items["com.gone.agent"].broken)
        self.assertIn("Leftover", items["com.gone.agent"].advice)
        self.assertFalse(items["com.acme.helper"].enabled)
        self.assertEqual(items["com.acme.helper"].name, "Acme")

    def test_actions(self):
        path = self.agent("com.acme.helper", "/usr/bin/true")
        it = startup.scan_launchd(self.ctx)[0]
        cmds = startup.plan(it, "disable", self.ctx)
        self.assertEqual(cmds[1], ["launchctl", "disable", f"gui/{os.getuid()}/com.acme.helper"])
        self.ctx.which = lambda n: "/bin/" + n
        ok, _msg = startup.apply(it, "remove", self.ctx)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(self.h(".Trash/com.acme.helper.plist")))

    def test_daemon_needs_sudo(self):
        path = self.agent("com.vpn.daemon", "/usr/bin/true", folder="../../Library/LaunchDaemons")
        it = [i for i in startup.scan_launchd(self.ctx) if i.kind == "daemon"][0]
        self.assertTrue(it.needs_root)
        self.assertEqual(startup.plan(it, "disable", self.ctx)[0][0], "sudo")
        self.assertTrue(os.path.exists(path))


class TestSmart(FakeMac):
    def test_skips_caches_of_running_apps(self):
        write(self.h("Library/Caches/com.spotify.client/data"), 50_000)
        write(self.h("Library/Caches/com.tinyapp.x/data"), 50_000)
        write(self.h("Library/Application Support/Slack/Cache/blob"), 50_000)
        write(self.h("Library/Developer/Xcode/DerivedData/App/x.o"), 50_000)
        apps = smart.RunningApps(names={"spotify", "slack", "xcode"}, bundle_ids={"com.spotify.client"})
        findings = scan(smart.smart_rules(BUILTIN_RULES), self.ctx)
        plan = smart.build_plan(findings, apps, self.ctx)
        cleaned = {os.path.basename(t.path) for f in plan.findings for t in f.targets}
        self.assertEqual(cleaned, {"com.tinyapp.x"})
        skipped = " ".join(s[0] for s in plan.skipped_open)
        self.assertIn("spotify", skipped)
        self.assertIn("Slack", skipped)
        self.assertIn("Xcode", skipped)

    def test_running_apps_parsing(self):
        self.ctx.which = lambda n: "/bin/" + n
        self.ctx.runner = fake_runner({"ps -Axo comm=": (
            "/Applications/Slack.app/Contents/MacOS/Slack\n"
            "/Applications/Slack.app/Contents/Frameworks/Slack Helper.app/Contents/MacOS/Slack Helper\n"
            "/usr/libexec/logd\n")})
        apps = smart.running_apps(self.ctx)
        self.assertEqual(apps.display, ["Slack"])
        self.assertEqual(apps.matches("Slack"), "Slack")  # display name, not an id
        self.assertIsNone(apps.matches("com.other.app"))

    def test_run_yes(self):
        write(self.h("Library/Caches/com.tinyapp.x/data"), 50_000)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(smart.run(self.ctx, BUILTIN_RULES, assume_yes=True, quiet=True), 0)
        self.assertIn("Freed", out.getvalue())
        self.assertFalse(os.path.exists(self.h("Library/Caches/com.tinyapp.x")))


class TestOptimize(FakeMac):
    def test_available_and_run(self):
        self.ctx.which = lambda n: "/bin/" + n if n in ("qlmanage", "killall") else None
        ids = [t.id for t in optimize.available(self.ctx)]
        self.assertEqual(ids, ["quicklook", "ui"])
        with redirect_stdout(io.StringIO()):
            results = optimize.run_tasks([t for t in optimize.TASKS if t.id == "quicklook"], self.ctx, quiet=True)
        self.assertTrue(results[0].ok)
        self.assertIn(["qlmanage", "-r", "cache"], self.ctx.runner.calls)


if __name__ == "__main__":
    unittest.main()


class TestCoverage(FakeMac):
    def app(self, name, bundle_id):
        path = os.path.join(self.root, "Applications", name + ".app", "Contents", "Info.plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleName": name}, fh)

    def test_app_leftovers(self):
        for i, (n, b) in enumerate([("Slack", "com.tinyspeck.slackmacgap"), ("Docker", "com.docker.docker"),
                                    ("A", "com.a.a"), ("B", "com.b.b"), ("C", "com.c.c")]):
            self.app(n, b)
        write(self.h("Library/Containers/com.gone.editor/Data/x.db"), 5000)          # leftover
        write(self.h("Library/Containers/com.docker.helper/Data/x"), 5000)           # same vendor: keep
        write(self.h("Library/Group Containers/ABCDE12345.com.gone.editor/x"), 5000)  # leftover (team prefix)
        write(self.h("Library/Containers/com.apple.Notes/Data/x"), 5000)             # apple: keep
        write(self.h("Library/Saved Application State/com.gone.editor.savedState/w"), 100)
        write(self.h("Library/Application Support/Slack/data"), 100)                  # not an id: skip
        by = self.findings_by_rule(scan(BUILTIN_RULES, self.ctx))
        roots = sorted(os.path.relpath(f.root, self.h("Library")) for f in by["app-leftovers"])
        self.assertEqual(roots, ["Containers/com.gone.editor", "Group Containers/ABCDE12345.com.gone.editor",
                                 "Saved Application State/com.gone.editor.savedState"])
        self.assertTrue(by["app-leftovers"][0].rule.trash)

    def test_trash_rules_move_to_trash_and_min_age(self):
        write(self.h("Downloads/old.dmg"), 5000, age_days=30)
        write(self.h("Downloads/new.dmg"), 5000)
        findings = scan([r for r in BUILTIN_RULES if r.id == "old-installers"], self.ctx)
        self.assertEqual([os.path.basename(f.root) for f in findings], ["old.dmg"])
        from macsmartcleaner import cleaner
        cleaner.execute(findings, self.ctx, dry_run=False)
        self.assertTrue(os.path.exists(self.h(".Trash/old.dmg")))
        self.assertTrue(os.path.exists(self.h("Downloads/new.dmg")))

    def test_root_only_folder_shows_unknown_size(self):
        spot = os.path.join(self.root, "System/Volumes/Data/.Spotlight-V100")
        write(os.path.join(spot, "Store-V2", "index"), 5000)
        os.chmod(spot, 0o000)
        try:
            if os.access(spot, os.R_OK):  # running as root: permissions don't apply
                self.skipTest("root can read everything")
            by = self.findings_by_rule(scan([r for r in BUILTIN_RULES if r.id == "spotlight-index"], self.ctx))
            f = by["spotlight-index"][0]
            self.assertFalse(f.size_known)
            self.assertIn("sudo", f.note)
        finally:
            os.chmod(spot, 0o755)

    def test_large_files(self):
        from macsmartcleaner import discover
        write(self.h("Movies/big.mov"), 3_000_000, age_days=400)
        write(self.h("Documents/medium.zip"), 2_000_000)
        write(self.h("Library/Caches/huge.bin"), 3_000_000)        # Library: never here
        write(self.h("Pictures/x.photoslibrary/originals/a.heic"), 3_000_000)  # inside a package
        found = {os.path.basename(h.path): h for h in discover.find_large_files(self.ctx, min_size=1_000_000)}
        self.assertEqual(set(found), {"big.mov", "medium.zip"})
        self.assertEqual(found["big.mov"].verdict, "old")
        self.assertEqual(found["medium.zip"].verdict, "large")

    def test_leftover_inside_counts_once(self):
        for n, b in [("A", "com.a.a"), ("B", "com.b.b"), ("C", "com.c.c"), ("D", "com.d.d"), ("E", "com.e.e")]:
            self.app(n, b)
        write(self.h("Library/Containers/com.gone.app/Data/Library/Caches/c.bin"), 400_000)
        write(self.h("Library/Containers/com.gone.app/Data/x.db"), 100_000)
        by = self.findings_by_rule(scan(BUILTIN_RULES, self.ctx))
        inner = by["sandbox-caches"][0].size
        outer = by["app-leftovers"][0].size
        total = sum(f.size for f in by["sandbox-caches"] + by["app-leftovers"])
        self.assertLess(outer, 400_000)  # the cache part is not double counted
        self.assertLess(abs(total - (inner + outer)), 1)
