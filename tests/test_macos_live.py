"""Live checks against the real macOS system tools. Skipped on other platforms; run in CI on macOS.

These verify the parts a fake home can't: that Apple's commands still print what the parsers
expect, that the ctypes CPU reader works, and that the parallel walker matches the simple one
on real APFS (firmlinks, hard links, sealed system volume).
"""
import os
import sys
import unittest

from macsmartcleaner import apps, rules, safety, sizes, startup, sysinfo, uninstall
from macsmartcleaner.context import Context

MAC = sys.platform == "darwin"


@unittest.skipUnless(MAC, "macOS only")
class TestLiveSystem(unittest.TestCase):
    ctx = Context() if MAC else None

    def test_per_core_cpu_via_mach(self):
        mach = sysinfo._MachCPU()
        self.assertTrue(mach.ok, "host_processor_info via ctypes failed")
        ticks = mach.ticks()
        self.assertEqual(len(ticks), os.cpu_count())
        self.assertTrue(all(total >= busy >= 0 for busy, total in ticks))

    def test_memory_sources(self):
        m = sysinfo.machine_info()
        self.assertGreater(m.memory, 1e9)
        self.assertTrue(m.os_name.startswith("macOS "), m.os_name)
        self.assertTrue(m.chip, "machdep.cpu.brand_string empty")
        vm = sysinfo.parse_vm_stat(sysinfo._cmd(["vm_stat"]))
        for key in ("Pages free", "Pages wired down", "Pages occupied by compressor", "Anonymous pages",
                    "File-backed pages", "Pages purgeable"):
            self.assertIn(key, vm)
        mem = sysinfo.memory_breakdown(vm, m.memory)
        self.assertTrue(0.05 * m.memory < mem["used"] <= m.memory, mem)
        self.assertTrue(sysinfo._sysctl("kern.memorystatus_level").isdigit())
        total, used = sysinfo.parse_swapusage(sysinfo._sysctl("vm.swapusage"))
        self.assertGreaterEqual(total, used)

    def test_monitor_end_to_end(self):
        mon = sysinfo.Monitor()
        mon.sample()
        import time
        time.sleep(1.1)
        mon.sample()
        s = mon.snapshot()
        self.assertIsNotNone(s.cpu_total)
        self.assertEqual(len(s.cpu_cores), os.cpu_count())
        self.assertIn(s.pressure_level, ("normal", "warning", "critical"))
        self.assertGreater(s.disk_total, 0)
        self.assertIsNotNone(s.net_rx_bps)
        self.assertTrue(s.procs)
        print(f"\n  live: cpu={s.cpu_total:.2f} cores={len(s.cpu_cores)} mem={s.mem['used'] / 1e9:.1f}GB "
              f"pressure={s.pressure_level} gpu={s.gpu} thermal={s.thermal} battery={s.battery} "
              f"io={s.disk_read_mbs} p/e={mon.machine.p_cores}/{mon.machine.e_cores}")

    def test_network_and_io_parsers_on_real_output(self):
        rx, tx = sysinfo.parse_netstat_ib(sysinfo._cmd(["netstat", "-ib"]))
        self.assertGreater(rx + tx, 0)
        self.assertIsNotNone(sysinfo.parse_iostat_total_mb(sysinfo._cmd(["iostat", "-Id"])))
        self.assertIsInstance(sysinfo.parse_pmset_therm(sysinfo._cmd(["pmset", "-g", "therm"])), str)

    def test_parallel_walker_matches_sequential_on_apfs(self):
        roots = ["/System/Library/Frameworks/AppKit.framework", "/System/Library/CoreServices",
                 os.path.expanduser("~/Library/Caches"), "/usr/share"]
        roots = [r for r in roots if os.path.isdir(r)]
        want = {r: sizes.measure(r) for r in roots}
        got = sizes.measure_many(roots, workers=4)
        for r in roots:
            self.assertEqual((got[r].bytes, got[r].files), (want[r].bytes, want[r].files), r)

    def test_launchctl_parsers(self):
        res = self.ctx.run(["launchctl", "print-disabled", f"gui/{self.ctx.uid}"], timeout=20)
        self.assertIsNotNone(res)
        self.assertTrue(startup._parse_disabled(res.stdout), "print-disabled format changed")
        res = self.ctx.run(["launchctl", "list"], timeout=20)
        self.assertTrue(startup._parse_launchctl_list(res.stdout), "launchctl list format changed")
        items = startup.scan_launchd(self.ctx)
        print(f"\n  startup items: {[(i.kind, i.label, i.enabled, i.running, i.broken) for i in items][:15]}")

    def test_installed_apps_and_leftovers(self):
        ids, names = apps.installed_apps(self.ctx)
        self.assertIn("com.apple.finder", ids)
        probe = rules._probe_leftovers(self.ctx)
        print(f"\n  leftovers flagged on this Mac: {probe.roots[:40]}")
        for p in probe.roots:
            self.assertFalse(os.path.basename(p).lower().startswith("com.apple."), p)

    def test_uninstaller_listing(self):
        found = uninstall.list_apps(self.ctx)
        self.assertTrue(all(not a.bundle_id.lower().startswith("com.apple.") for a in found))
        uninstall.load_last_used(found[:10], self.ctx)
        for a in found[:10]:
            if a.last_used:
                self.assertLess(a.last_used, __import__("time").time() + 86400)
        sample = found[:5]
        for a in sample:
            uninstall.collect(a, self.ctx)
        print(f"\n  apps: {[(a.name, a.bundle_id, len(a.files)) for a in sample]}")

    def test_time_machine_probe_runs(self):
        res = rules._probe_tm_snapshots(self.ctx)
        self.assertTrue(res.virtual)

    def test_safety_on_real_paths(self):
        for p in ["/System/Library", "/Applications/Safari.app", os.path.expanduser("~/Library"),
                  os.path.expanduser("~/Documents"), "/private/var/folders"]:
            with self.assertRaises(safety.UnsafePath, msg=p):
                safety.check(p, self.ctx)
        safety.check(os.path.expanduser("~/Library/Caches/some.cache"), self.ctx)
        cache_dir = sysinfo._cmd(["getconf", "DARWIN_USER_CACHE_DIR"]).strip().rstrip("/")
        safety.check(os.path.join(cache_dir, "com.example.cache"), self.ctx)
        with self.assertRaises(safety.UnsafePath):
            safety.check("/Applications/Safari.app", self.ctx, trash=True)

    def test_gpu_query_does_not_fail(self):
        g = sysinfo.parse_ioreg_gpu(sysinfo._cmd(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"]))
        self.assertIn("util", g)


if __name__ == "__main__":
    unittest.main()
