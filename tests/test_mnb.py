# SPDX-License-Identifier: GPL-3.0-only
"""Behavioral mnb contracts; local Git fixtures, no app launch or installed data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("personal_build", ROOT / "scripts/personal_build.py")
MNB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MNB)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Build test")
        self.git("config", "user.email", "build-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "input").write_text("first")
        self.git("add", "input")
        self.git("commit", "-qm", "fixture")
        self.first = self.git("rev-parse", "HEAD")
        self.worktree = self.base / "managed"
        self.state = self.repo / ".git/lighttable-personal-builds"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.PIPE).strip()

    def prepare(self, cutoff=None):
        MNB.prepare_worktree(self.repo, self.worktree, cutoff or self.first, self.state)

    def test_reuses_only_owned_clean_detached_source(self):
        self.prepare()
        (self.repo / "input").write_text("second")
        self.git("commit", "-qam", "second")
        cutoff = self.git("rev-parse", "HEAD")
        self.prepare(cutoff)
        self.assertEqual((self.worktree / "input").read_text(), "second")
        self.assertEqual(MNB.git(self.worktree, "rev-parse", "HEAD"), cutoff)
        self.assertEqual(MNB.git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertIn(MNB.LOCK_REASON, self.git("worktree", "list", "--porcelain"))

    def test_unowned_directory_is_untouched(self):
        self.worktree.mkdir()
        original = self.worktree / "private-work"
        original.write_text("preserve")
        with self.assertRaisesRegex(MNB.BuildError, "Unowned"):
            self.prepare()
        self.assertEqual(original.read_text(), "preserve")

    def test_tracked_and_untracked_work_is_preserved(self):
        self.prepare()
        for name in ("input", "new-work"):
            with self.subTest(name=name):
                target = self.worktree / name
                target.write_text("preserve")
                with self.assertRaisesRegex(MNB.BuildError, "dirty"):
                    self.prepare()
                self.assertEqual(target.read_text(), "preserve")
                if name == "input":
                    target.write_text("first")
                else:
                    target.unlink()

    def test_branch_and_foreign_lock_are_preserved(self):
        self.prepare()
        MNB.git(self.worktree, "checkout", "-qb", "someone-elses-work")
        with self.assertRaisesRegex(MNB.BuildError, "has a branch"):
            self.prepare()
        MNB.git(self.worktree, "checkout", "--detach", self.first)
        lock = Path(MNB.git(self.worktree, "rev-parse", "--absolute-git-dir")) / "locked"
        lock.write_text("another task")
        with self.assertRaisesRegex(MNB.BuildError, "another task"):
            self.prepare()
        self.assertEqual(lock.read_text(), "another task")

    def test_retirement_removes_only_the_owned_clean_worktree(self):
        self.prepare()
        with patch.object(MNB.Path, "home", return_value=self.base):
            MNB.retire_worktree(self.repo, self.worktree)
        self.assertFalse(self.worktree.exists())
        self.assertTrue((self.repo / "input").is_file())
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_retirement_preserves_dirty_worktree_and_its_lock(self):
        self.prepare()
        (self.worktree / "private").write_text("keep")
        with patch.object(MNB.Path, "home", return_value=self.base):
            with self.assertRaisesRegex(MNB.BuildError, "dirty"):
                MNB.retire_worktree(self.repo, self.worktree)
        self.assertEqual((self.worktree / "private").read_text(), "keep")
        self.assertIn(MNB.LOCK_REASON, self.git("worktree", "list", "--porcelain"))

    def test_concurrent_build_lock_refuses_without_waiting(self):
        with MNB.exclusive_lock(self.base / "build.lock"):
            with self.assertRaisesRegex(MNB.BuildError, "Another personal build"):
                with MNB.exclusive_lock(self.base / "build.lock"):
                    self.fail("lock was shared")


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.app = self.base / "app"
        self.contents = self.app / "Contents"
        (self.contents / "Resources/LightTable").mkdir(parents=True)
        self.info = {"LightTableSourceRevision": "a" * 40, "LightTableSourceDirty": False,
                     "LightTableSourceTree": "tree", "CFBundleShortVersionString": "1.2.3",
                     "CFBundleIdentifier": "com.reville.filmlab.nprinstalled",
                     "LSEnvironment": {"LIGHTTABLE_CATALOG_FILE": str(self.base / "catalog")}}
        self.manifest = {"SOURCE_REVISION": "a" * 40, "SOURCE_DIRTY": "false", "SOURCE_TREE_HASH": "tree"}
        self.write_bundle()

    def write_bundle(self):
        (self.contents / "Info.plist").write_bytes(plistlib.dumps(self.info))
        (self.contents / "Resources/LightTable/personal-build.env").write_text(
            "\n".join(f"{key}={value}" for key, value in self.manifest.items()))

    def test_dirty_and_mismatched_manifest_cannot_pass(self):
        with patch.object(MNB, "command") as signer:
            identity = MNB.bundle_identity(self.app, "a" * 40)
            self.assertEqual(identity["signature"], "passed")
            signer.assert_called_once()
            for key, value in [("SOURCE_DIRTY", "true"), ("SOURCE_REVISION", "b" * 40),
                               ("SOURCE_TREE_HASH", "different")]:
                with self.subTest(key=key):
                    previous = self.manifest[key]
                    self.manifest[key] = value
                    self.write_bundle()
                    with self.assertRaises(MNB.BuildError):
                        MNB.bundle_identity(self.app, "a" * 40)
                    self.manifest[key] = previous

    def test_signature_failure_is_a_failure(self):
        with patch.object(MNB, "command", side_effect=MNB.BuildError("bad signature")):
            with self.assertRaisesRegex(MNB.BuildError, "signature"):
                MNB.bundle_identity(self.app, "a" * 40)

    def test_install_restores_old_app_when_final_move_fails(self):
        candidate = self.base / "candidate"
        candidate.mkdir()
        backup = self.base / "backup"
        real_replace = MNB.os.replace
        def fail_candidate(source, target):
            if source == candidate:
                raise OSError("simulated installation failure")
            real_replace(source, target)
        with patch.object(MNB, "bundle_processes", return_value={"native": [], "server": []}), \
             patch.object(MNB.os, "replace", side_effect=fail_candidate):
            with self.assertRaises(OSError):
                MNB.install_staged(candidate, self.app, backup)
        self.assertTrue((self.app / "Contents/Info.plist").is_file())
        self.assertFalse(backup.exists())
        self.assertTrue(candidate.exists())

    def test_health_requires_same_bundle_catalog_server_and_connected_window(self):
        identity = {"revision": "a" * 40, "version": "1.2.3", "catalog": str(self.base / "catalog")}
        payload = {"ok": True, "version": "1.2.3", "catalog": identity["catalog"],
                   "pid": 22, "port": 8321, "sourceRevision": None, "windowConnected": True}
        processes = {"native": [11], "server": [22]}
        self.assertEqual(MNB.verify_health(payload, identity, processes, 200)["server_pid"], 22)
        for changes in ({"catalog": "/wrong"}, {"pid": 99}, {"windowConnected": False},
                        {"version": "old"}, {"sourceRevision": "b" * 40}, {"headless": True}):
            with self.subTest(changes=changes), self.assertRaises(MNB.BuildError):
                MNB.verify_health(payload | changes, identity, processes, 200)
        with self.assertRaises(MNB.BuildError):
            MNB.verify_health(payload, identity, {"native": [], "server": [22]}, 200)
        with self.assertRaises(MNB.BuildError):
            MNB.verify_health(payload, identity, processes, 500)


class OrchestrationTests(unittest.TestCase):
    def test_fetch_once_build_before_quit_and_never_overlay_pinned_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            worktree = base / "managed"
            (worktree / "scripts").mkdir(parents=True)
            updater = worktree / "scripts/update-personal-app.sh"
            updater.write_text("pinned updater")
            app = base / "installed.app"
            app.mkdir()
            calls = []
            def fake_git(root, *args):
                calls.append(("git", args))
                return str(base / "common") if "--git-common-dir" in args else "a" * 40
            def fake_command(argv, **kwargs):
                calls.append(("command", [str(x) for x in argv]))
                return ""
            receipt = {"phases": [], "build_log": str(base / "build.log")}
            identity = {"revision": "a" * 40, "catalog": str(base / "catalog")}
            with patch.object(MNB, "git", side_effect=fake_git), \
                 patch.object(MNB.Path, "home", return_value=base), \
                 patch.object(MNB, "prepare_worktree"), patch.object(MNB, "require_clean"), \
                 patch.object(MNB, "command", side_effect=fake_command), \
                 patch.object(MNB, "bundle_identity", return_value=identity), \
                 patch.object(MNB, "quit_bundle", side_effect=lambda app: calls.append(("quit", app))), \
                 patch.object(MNB, "install_staged"), patch.object(MNB, "wait_for_health", return_value={"http_status": 200}):
                MNB.run_build(base, app, worktree, receipt)
            fetches = [x for x in calls if x[0] == "git" and x[1][0] == "fetch"]
            self.assertEqual(len(fetches), 1)
            build = next(i for i, x in enumerate(calls) if x[0] == "command" and "--build-only" in x[1])
            quit_index = next(i for i, x in enumerate(calls) if x[0] == "quit")
            self.assertLess(build, quit_index)
            self.assertEqual(updater.read_text(), "pinned updater")
            self.assertEqual(receipt["status"], "installed_verified")
            self.assertEqual(receipt["checks"]["visual_review"], "not_run")

    def test_build_failure_preserves_the_running_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            worktree = base / "managed"
            (worktree / "scripts").mkdir(parents=True)
            (worktree / "scripts/update-personal-app.sh").write_text("pinned updater")
            receipt = {"phases": [], "build_log": str(base / "build.log")}
            with patch.object(MNB, "common_dir", return_value=base / "common"), \
                 patch.object(MNB.Path, "home", return_value=base), \
                 patch.object(MNB, "git", return_value="a" * 40), \
                 patch.object(MNB, "prepare_worktree"), \
                 patch.object(MNB, "command", side_effect=MNB.BuildError("compiler failed")), \
                 patch.object(MNB, "quit_bundle") as quit_app, \
                 patch.object(MNB, "install_staged") as install:
                with self.assertRaisesRegex(MNB.BuildError, "compiler failed"):
                    MNB.run_build(base, base / "installed.app", worktree, receipt)
                quit_app.assert_not_called()
                install.assert_not_called()
            self.assertEqual(receipt["phases"][-1]["status"], "failed")

    def test_running_worktree_package_prevents_source_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            worktree = base / "managed"
            (worktree / ".build/personal" / MNB.PRODUCT).mkdir(parents=True)
            receipt = {"phases": []}
            with patch.object(MNB, "common_dir", return_value=base / "common"), \
                 patch.object(MNB.Path, "home", return_value=base), \
                 patch.object(MNB, "git", return_value="a" * 40), \
                 patch.object(MNB, "bundle_processes", return_value={"native": [42], "server": []}), \
                 patch.object(MNB, "prepare_worktree") as prepare:
                with self.assertRaisesRegex(MNB.BuildError, "running package"):
                    MNB.run_build(base, base / "installed.app", worktree, receipt)
                prepare.assert_not_called()

    def test_failure_receipt_is_written_with_failed_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            def fail(*args, **kwargs):
                with MNB.phase(args[3], "build"):
                    raise MNB.BuildError("intentional test failure")
            with patch.object(MNB, "run_build", side_effect=fail):
                self.assertEqual(MNB.main(["--receipt", str(path)]), 1)
            receipt = json.loads(path.read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["phases"][0]["status"], "failed")
            self.assertEqual(receipt["error"], "intentional test failure")

    def test_symlink_entrypoint_finds_its_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "mnb"
            entry.symlink_to(ROOT / "scripts/mnb.sh")
            output = subprocess.check_output(["bash", str(entry), "--help"], text=True)
            self.assertIn("--receipt", output)


if __name__ == "__main__":
    unittest.main()
