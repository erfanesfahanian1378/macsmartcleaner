"""Spotlight Doctor and System Data breakdown."""
import os
import plistlib
import unittest

from macsmartcleaner import diagnose, spotlight
from tests.test_macsmartcleaner import FakeMac, write

FS_USAGE = """\
12:00:01.000001  open              F=5        (R_____)  /Users/me/Library/CloudStorage/OneDrive-Uni/Docs/a.docx                0.000120   mdworker_shared.901
12:00:01.000002  getattrlist                            /Users/me/Library/CloudStorage/OneDrive-Uni/Docs/b.pdf                  0.000020   mdworker_shared.901
12:00:01.000003  open              F=6        (R_____)  /System/Volumes/Data/Users/me/Library/CloudStorage/OneDrive-Uni/x.xlsx  0.000100   mds_stores.88
12:00:01.000004  write             F=7    B=0x1000      /System/Volumes/Data/.Spotlight-V100/Store-V2/ABC/journalAttr.4        0.000050   mds_stores.88
12:00:01.000005  open              F=8        (R_____)  /Users/me/Projects/web/node_modules/react/index.js                 0.000030   mdworker_shared.902
"""


class TestSpotlight(FakeMac):
    def store(self):
        return os.path.join(self.root, "System/Volumes/Data/.Spotlight-V100")

    def test_parse_and_group_fs_usage(self):
        paths = spotlight.parse_fs_usage(FS_USAGE)
        self.assertEqual(len(paths), 5)
        top = spotlight.group_paths(paths, "/Users/me")
        self.assertEqual(top[0], ("~/Library/CloudStorage/OneDrive-Uni", 3))  # its own index writes don't count
        self.assertEqual(dict(top)["~/Projects/web/node_modules"], 1)

    def test_index_size_and_exclusions_roundtrip(self):
        write(os.path.join(self.store(), "Store-V2", "ABC", "journalAttr.4"), 400_000)
        conf = os.path.join(self.store(), "VolumeConfiguration.plist")
        with open(conf, "wb") as fh:
            plistlib.dump({"Exclusions": ["/Volumes/Backup"], "Other": 1}, fh)
        self.assertGreaterEqual(spotlight.index_size(self.ctx), 400_000)
        self.assertEqual(spotlight.store_breakdown(self.ctx)[0][0], os.path.join("Store-V2", "ABC"))
        self.ctx.is_root = True
        self.ctx.which = lambda n: "/bin/" + n
        cloud = self.h("Library/CloudStorage")
        os.makedirs(cloud)
        ok, msg = spotlight.set_exclusions(self.ctx, add=[cloud])
        self.assertTrue(ok, msg)
        self.assertIn(os.path.realpath(cloud), spotlight.exclusions(self.ctx))
        self.assertTrue(os.path.exists(conf + ".msc-backup"))
        with open(conf, "rb") as fh:
            self.assertEqual(plistlib.load(fh)["Other"], 1)  # the rest of the config is untouched
        self.assertIn(["launchctl", "kickstart", "-k", "system/com.apple.metadata.mds"], self.runner.calls)
        spotlight.set_exclusions(self.ctx, remove=[cloud])
        self.assertEqual(spotlight.exclusions(self.ctx), ["/Volumes/Backup"])

    def test_changes_need_root(self):
        ok, msg = spotlight.set_exclusions(self.ctx, add=["/x"])
        self.assertFalse(ok)
        self.assertIn("sudo", msg)

    def test_suspects_flag_cloud_storage_always(self):
        write(self.h("Library/CloudStorage/OneDrive-Uni/doc.txt"), 100)
        s = spotlight.find_suspects(self.ctx, [])
        self.assertEqual([os.path.basename(x.path) for x in s], ["CloudStorage"])
        self.assertFalse(s[0].excluded)
        self.assertTrue(spotlight.find_suspects(self.ctx, [self.h("Library/CloudStorage")])[0].excluded)

    def test_guard_rebuilds_only_when_too_big(self):
        write(os.path.join(self.store(), "Store-V2", "big"), 3_000_000)
        self.ctx.is_root = True
        self.ctx.which = lambda n: "/bin/" + n
        os.makedirs(os.path.join(self.root, "Library/Logs"), exist_ok=True)
        spotlight.guard_check(self.ctx, 10_000_000)
        self.assertNotIn(["mdutil", "-E", "/System/Volumes/Data"], self.runner.calls)
        _ok, msg = spotlight.guard_check(self.ctx, 1_000_000)
        self.assertIn(["mdutil", "-E", "/System/Volumes/Data"], self.runner.calls)
        self.assertIn("index erased", msg)


APFS_LIST = plistlib.dumps({"Containers": [{
    "CapacityCeiling": 994_662_584_320, "CapacityFree": 393_260_000_000,
    "Volumes": [
        {"Name": "Macintosh HD", "Roles": ["System"], "CapacityInUse": 11_000_000_000, "DeviceIdentifier": "disk3s1"},
        {"Name": "Macintosh HD - Data", "Roles": ["Data"], "CapacityInUse": 560_000_000_000, "DeviceIdentifier": "disk3s5"},
        {"Name": "Update", "Roles": ["Update"], "CapacityInUse": 14_000_000_000},
        {"Name": "VM", "Roles": ["VM"], "CapacityInUse": 6_000_000_000},
    ]}]})


class TestDiagnose(FakeMac):
    def test_apfs_and_snapshots_parsing(self):
        cap, free, vols = diagnose.parse_apfs_list(APFS_LIST)
        self.assertEqual(cap, 994_662_584_320)
        self.assertEqual(vols[0][0], "Macintosh HD - Data")
        self.assertIn(("Update", "Update", 14_000_000_000), vols)
        snaps = diagnose.parse_snapshots(plistlib.dumps({"Snapshots": [
            {"SnapshotName": "com.apple.TimeMachine.2026-09-25-120000.local"},
            {"SnapshotName": "com.bombich.ccc.123"}]}))
        self.assertEqual(len(snaps), 2)
        self.assertEqual(diagnose.parse_apfs_list(b"garbage"), (0, 0, []))

    def test_measure_pieces(self):
        write(os.path.join(self.root, "private/var/vm/swapfile0"), 200_000)
        rows = {r.label: r for r in diagnose.measure_pieces(self.ctx)}
        self.assertGreaterEqual(rows["Swap & sleep image"].usage.bytes, 200_000)


if __name__ == "__main__":
    unittest.main()
