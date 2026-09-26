"""Auto-Protect."""
import os
import plistlib
import subprocess
import unittest
import unittest.mock
from collections import namedtuple

from macsmartcleaner import guard
from tests.test_macsmartcleaner import FakeMac, write

DU = namedtuple("DU", "total used free")


class TestGuard(FakeMac):
    def setUp(self):
        super().setUp()
        self.ctx.is_root = True
        self.ctx.which = lambda n: "/usr/bin/" + n
        os.makedirs(os.path.join(self.root, "Library/LaunchDaemons"))
        os.makedirs(os.path.join(self.root, "Library/Logs"))

    def test_install_status_remove(self):
        ok, msg = guard.install(self.ctx, 20_000_000_000, 50_000_000_000, user="me")
        self.assertTrue(ok, msg)
        with open(self.ctx.path(guard.PLIST), "rb") as fh:
            p = plistlib.load(fh)
        self.assertEqual(p["StartInterval"], 3600)
        self.assertIn("--user", p["ProgramArguments"])
        st = guard.status(self.ctx)
        self.assertTrue(st["installed"])
        self.assertEqual((st["index_max"], st["min_free"]), (20_000_000_000, 50_000_000_000))
        self.assertTrue(any(c[:2] == ["launchctl", "bootstrap"] for c in self.runner.calls))
        self.assertTrue(guard.remove(self.ctx)[0])
        self.assertFalse(guard.status(self.ctx)["installed"])

    def test_install_needs_root(self):
        self.ctx.is_root = False
        self.assertFalse(guard.install(self.ctx, 1, 1, user="me")[0])

    def test_check_rebuilds_big_index_and_frees_space_when_low(self):
        write(os.path.join(self.root, "System/Volumes/Data/.Spotlight-V100/Store-V2/x"), 3_000_000)
        write(self.h("Library/Caches/com.tiny.app/c"), 50_000)
        snaps = {"n": ["com.apple.TimeMachine.2026-09-26-100000.local"]}

        def run(cmd, timeout):
            c = " ".join(cmd)
            if "listlocalsnapshots" in c:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(snaps["n"]) + "\n", "")
            if "deletelocalsnapshots /" in c:
                snaps["n"] = []
            self.runner.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.ctx.runner = run
        with unittest.mock.patch("shutil.disk_usage", return_value=DU(1000, 990, 1_000_000)):
            notes = guard.check(self.ctx, index_max=1_000_000, min_free=10_000_000)
        text = "\n".join(notes)
        self.assertIn(["mdutil", "-E", "/System/Volumes/Data"], self.runner.calls)
        self.assertIn("deleted 1 local snapshot", text)
        self.assertFalse(os.path.exists(self.h("Library/Caches/com.tiny.app")))
        with open(self.ctx.path(guard.LOG)) as fh:
            self.assertIn("Spotlight index", fh.read())

    def test_check_does_nothing_when_healthy(self):
        write(os.path.join(self.root, "System/Volumes/Data/.Spotlight-V100/Store-V2/x"), 1000)
        write(self.h("Library/Caches/com.tiny.app/c"), 50_000)
        with unittest.mock.patch("shutil.disk_usage", return_value=DU(1000, 10, 900_000_000_000)):
            notes = guard.check(self.ctx, index_max=20_000_000_000, min_free=50_000_000_000)
        self.assertTrue(all("ok" in n for n in notes), notes)
        self.assertTrue(os.path.exists(self.h("Library/Caches/com.tiny.app")))


if __name__ == "__main__":
    unittest.main()
