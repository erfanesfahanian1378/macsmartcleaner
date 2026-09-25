import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from macsmartcleaner import cleaner, discover, safety
from macsmartcleaner.cli import main
from macsmartcleaner.context import Context
from macsmartcleaner.rules import BUILTIN_RULES, load_rules
from macsmartcleaner.scanner import scan
from macsmartcleaner.sizes import human, measure, parse_size


def write(path, size=4096, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


def fake_runner(responses):
    calls = []

    def run(cmd, timeout):
        calls.append(list(cmd))
        key = " ".join(cmd[:3])
        out = responses.get(key, "")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    run.calls = calls
    return run


class FakeMac(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.root, "Users", "me")
        os.makedirs(self.home)
        self.runner = fake_runner({})
        self.ctx = Context(home=self.home, root=self.root, is_root=False, runner=self.runner, sudo_user=None)

    def tearDown(self):
        self.tmp.cleanup()

    def h(self, *parts):
        return os.path.join(self.home, *parts)

    def findings_by_rule(self, findings):
        out = {}
        for f in findings:
            out.setdefault(f.rule.id, []).append(f)
        return out


class TestSizes(unittest.TestCase):
    def test_measure_and_format(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a", "b.bin"), 100_000)
            u = measure(d)
            self.assertGreaterEqual(u.bytes, 100_000)
            self.assertEqual(u.files, 1)
            self.assertFalse(measure(os.path.join(d, "missing")).exists)
        self.assertEqual(human(1_500_000_000), "1.5 GB")
        self.assertEqual(parse_size("2GB"), 2_000_000_000)
        self.assertEqual(parse_size("500m"), 500_000_000)

    def test_hardlinks_counted_once(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "x"), 200_000)
            os.link(os.path.join(d, "x"), os.path.join(d, "y"))
            self.assertLess(measure(d).bytes, 400_000)


class TestRules(unittest.TestCase):
    def test_unique_ids_and_user_rules(self):
        self.assertEqual(len({r.id for r in BUILTIN_RULES}), len(BUILTIN_RULES))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"id": "mine", "safety": "safe", "action": "delete-contents", "paths": ["~/.x"]}], fh)
        try:
            self.assertIn("mine", [r.id for r in load_rules(fh.name)])
        finally:
            os.unlink(fh.name)


class TestScanner(FakeMac):
    def test_specific_rules_carve_out_of_generic_caches(self):
        write(self.h("Library/Caches/com.spotify.client/data"), 50_000)
        write(self.h("Library/Caches/Homebrew/downloads/x.tar.gz"), 80_000)
        write(self.h("Library/Caches/com.apple.bird/keep"), 10_000)  # iCloud: excluded
        write(self.h(".cache/huggingface/hub/models--bert/blob"), 90_000)
        write(self.h(".cache/huggingface/token"), 100)  # must not be swept by generic ~/.cache
        write(self.h(".cache/pip/wheels/a.whl"), 30_000)

        by = self.findings_by_rule(scan(BUILTIN_RULES, self.ctx))
        user_cache_targets = {os.path.basename(t.path) for t in by["user-caches"][0].targets}
        self.assertEqual(user_cache_targets, {"com.spotify.client"})
        self.assertIn("homebrew", by)
        self.assertIn("pip", by)
        self.assertIn("huggingface-models", by)
        self.assertNotIn("xdg-cache", by)  # only huggingface+pip there, both claimed

    def test_min_age_keeps_recent_items(self):
        write(self.h("Library/Caches/old/x"), 10_000, age_days=30)
        write(self.h("Library/Caches/new/x"), 10_000)
        rules = [r for r in BUILTIN_RULES if r.id == "user-caches"]
        import dataclasses
        rules = [dataclasses.replace(rules[0], min_age_days=7)]
        f = scan(rules, self.ctx)[0]
        self.assertEqual([os.path.basename(t.path) for t in f.targets], ["old"])
        self.assertEqual(f.skipped_young, 1)

    def test_time_machine_probe(self):
        self.ctx.runner = fake_runner({"tmutil listlocalsnapshots /": "Snapshots for disk /:\n"
                                       "com.apple.TimeMachine.2026-09-20-101010.local\n"
                                       "com.apple.TimeMachine.2026-09-21-101010.local\n"})
        self.ctx.which = lambda name: "/usr/bin/" + name
        by = self.findings_by_rule(scan(BUILTIN_RULES, self.ctx))
        self.assertIn("tm-snapshots", by)
        self.assertFalse(by["tm-snapshots"][0].size_known)
        self.assertIn("2 local snapshot", by["tm-snapshots"][0].note)


class TestSafety(FakeMac):
    def test_protected_paths_refused(self):
        for p in [self.home, self.h("Library"), self.h("Library/Caches"), self.h("Documents"),
                  self.h("Library/Keychains/login.keychain-db"), self.root,
                  os.path.join(self.root, "System/Library/x")]:
            with self.assertRaises(safety.UnsafePath, msg=p):
                safety.check(p, self.ctx)

    def test_allowed_paths(self):
        safety.check(self.h("Library/Caches/com.x"), self.ctx)
        safety.check(self.h("Documents/proj/node_modules"), self.ctx)
        safety.check(os.path.join(self.root, "Library/Caches/foo"), self.ctx)

    def test_symlinked_parent_cannot_escape(self):
        os.makedirs(os.path.join(self.root, "System/Library"))
        os.makedirs(self.h("Library/Caches"))
        os.symlink(os.path.join(self.root, "System"), self.h("Library/Caches/evil"))
        with self.assertRaises(safety.UnsafePath):
            safety.check(self.h("Library/Caches/evil/Library"), self.ctx)


class TestCleaner(FakeMac):
    def test_dry_run_then_clean(self):
        write(self.h("Library/Caches/com.spotify.client/data"), 50_000)
        write(self.h("Library/Developer/Xcode/DerivedData/App-abc/Build/x.o"), 60_000)
        write(self.h(".cache/huggingface/hub/models--bert/blob"), 90_000)
        findings = scan(BUILTIN_RULES, self.ctx)
        chosen = cleaner.select(findings, tier="safe")
        self.assertEqual({f.rule.id for f in chosen}, {"user-caches", "xcode-deriveddata"})

        cleaner.execute(chosen, self.ctx, dry_run=True)
        self.assertTrue(os.path.exists(self.h("Library/Caches/com.spotify.client/data")))

        outcomes = cleaner.execute(chosen, self.ctx, dry_run=False)
        self.assertTrue(all(o.ok for o in outcomes), [o.messages for o in outcomes])
        self.assertFalse(os.path.exists(self.h("Library/Caches/com.spotify.client")))
        self.assertTrue(os.path.isdir(self.h("Library/Caches")))  # folder itself kept
        self.assertTrue(os.path.exists(self.h(".cache/huggingface/hub/models--bert/blob")))  # review tier
        self.assertTrue(os.path.exists(cleaner.history_path(self.ctx)))

    def test_review_needs_only(self):
        write(self.h(".cache/huggingface/hub/models--bert/blob"), 90_000)
        findings = scan(BUILTIN_RULES, self.ctx)
        self.assertEqual(cleaner.select(findings, tier="caution"), [])
        self.assertEqual(len(cleaner.select(findings, only=["huggingface-models"])), 1)

    def test_read_only_tree_is_removed(self):
        write(self.h("Library/Developer/Xcode/DerivedData/ro/sub/f"), 1000)
        os.chmod(self.h("Library/Developer/Xcode/DerivedData/ro/sub"), 0o555)
        chosen = cleaner.select(scan(BUILTIN_RULES, self.ctx), only=["xcode-deriveddata"])
        cleaner.execute(chosen, self.ctx, dry_run=False)
        self.assertFalse(os.path.exists(self.h("Library/Developer/Xcode/DerivedData/ro")))

    def test_command_rule_runs_tool(self):
        write(self.h("Library/Caches/Homebrew/x.tar.gz"), 10_000)
        self.ctx.which = lambda name: "/usr/bin/" + name
        chosen = cleaner.select(scan(BUILTIN_RULES, self.ctx), only=["homebrew"])
        cleaner.execute(chosen, self.ctx, dry_run=False)
        self.assertIn(["brew", "cleanup", "--prune=all", "-s"], self.ctx.runner.calls)

    def test_trash(self):
        write(self.h("Library/Application Support/OldApp/big.db"), 1000)
        cleaner.move_to_trash([self.h("Library/Application Support/OldApp")], self.ctx)
        self.assertTrue(os.path.exists(self.h(".Trash/OldApp/big.db")))
        with self.assertRaises(safety.UnsafePath):
            cleaner.move_to_trash([self.h("Documents")], self.ctx)


class TestDiscover(FakeMac):
    def test_project_artifacts(self):
        write(self.h("Projects/web/package.json"), 10, age_days=90)
        write(self.h("Projects/web/node_modules/react/index.js"), 2_000_000)
        write(self.h("Projects/game/Assets/a.cs"), 10)
        write(self.h("Projects/game/ProjectSettings/p.asset"), 10)
        write(self.h("Projects/game/Library/ArtifactDB"), 3_000_000)
        write(self.h("Projects/notes/Library/book.pdf"), 3_000_000)  # not Unity: no markers
        write(self.h("Projects/ml/.venv/pyvenv.cfg"), 10)
        write(self.h("Projects/ml/.venv/lib/torch.so"), 2_000_000)
        found = discover.find_project_artifacts(self.ctx, ["~/Projects"])
        names = sorted(os.path.relpath(f.root, self.h("Projects")) for f in found)
        self.assertEqual(names, ["game/Library", "ml/.venv", "web/node_modules"])
        web = [f for f in found if f.root.endswith("node_modules")][0]
        self.assertGreaterEqual(self.ctx.days_since(web.newest_mtime), 89)

    def test_space_hogs_classification(self):
        write(self.h("Library/Application Support/SomeCache/blob"), 2_000_000)
        write(self.h("Library/Application Support/GoneApp/data.db"), 2_000_000, age_days=400)
        write(self.h("Library/Caches/com.x/y"), 2_000_000)  # covered by user-caches
        findings = scan(BUILTIN_RULES, self.ctx)
        hogs = {os.path.basename(h.path): h for h in discover.find_space_hogs(self.ctx, findings, min_size=1_000_000)}
        self.assertEqual(hogs["SomeCache"].verdict, "likely-junk")
        self.assertIn(hogs["GoneApp"].verdict, ("stale", "orphaned"))
        self.assertNotIn("com.x", hogs)


class TestCLI(FakeMac):
    def test_scan_and_dry_run_clean(self):
        write(self.h("Library/Caches/com.spotify.client/data"), 60_000_000)
        out = io.StringIO()
        html = os.path.join(self.root, "r.html")
        with redirect_stdout(out):
            self.assertEqual(main(["scan", "--no-discover", "--projects", "~/none", "--html", html], ctx=self.ctx), 0)
            self.assertEqual(main(["clean", "--dry-run"], ctx=self.ctx), 0)
        text = out.getvalue()
        self.assertIn("user-caches", text)
        self.assertIn("Dry run", text)
        self.assertTrue(os.path.exists(self.h("Library/Caches/com.spotify.client/data")))
        with open(html) as fh:
            self.assertIn("Mac Storage Report", fh.read())


if __name__ == "__main__":
    unittest.main()
