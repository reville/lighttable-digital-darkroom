"""Offline proof of immutable, independently resumable platform publication."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release/release_process.py"
SPEC = importlib.util.spec_from_file_location("release_process", SCRIPT)
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
PREP_SPEC = importlib.util.spec_from_file_location("prepare_candidate", SCRIPT.with_name("prepare_candidate.py"))
PREPARE = importlib.util.module_from_spec(PREP_SPEC)
with patch.dict(sys.modules, {"release_process": RELEASE}):
    PREP_SPEC.loader.exec_module(PREPARE)
REVISION = "a" * 40


class ReleaseProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {"schema_version": 1, "version": "1.2.3", "source_revision": REVISION,
                         "platforms": {"linux-x86_64": self.entry("linux-x86_64")}}
        self.platform = "linux-x86_64"

    def entry(self, platform):
        return {"version": "1.2.3", "channel": "stable", "minimum_os": "Ubuntu 24.04+",
                "update_owner": "app", "state": "ready", "signing": "ed25519" if platform == "linux-x86_64" else "authenticode",
                "gates": [], "validation": {"status": "passed", "receipts": ["https://github.com/reville/lighttable-digital-darkroom/actions/runs/1"]}, "artifacts": []}

    def artifact(self, name=None, content=b"signed installer definitions", platform=None):
        platform = platform or self.platform
        name = name or f"LightTable-1.2.3-{platform}-installers.tar.gz"
        path = self.root / name
        path.write_bytes(content)
        identity = RELEASE.file_identity(path)
        self.manifest["platforms"][platform]["artifacts"].append({**identity, "url": RELEASE.asset_url(self.manifest, name, platform)})
        return path

    def archive(self, **overrides):
        payload = {"version": "1.2.3", "source_revision": REVISION, "source_dirty": False, "platform": "linux", "architecture": "x86_64", **overrides}
        content = json.dumps(payload).encode()
        path = self.root / "LightTable-1.2.3-linux-x86_64.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("LightTable/build-manifest.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        self.manifest["platforms"][self.platform]["artifacts"].append({**RELEASE.file_identity(path), "url": RELEASE.asset_url(self.manifest, path.name, self.platform)})
        return path

    def manifest_path(self):
        path = self.root / RELEASE.platform_names("1.2.3", self.platform)["manifest"]
        RELEASE.write_json(path, self.manifest)
        return path

    def capabilities(self):
        return {"source_revision": REVISION, "source_clean": True, "tag_revision": REVISION,
                "platforms": {self.platform: {"release_environment_allowed": True, "build_runner_ready": True,
                                              "native_acceptance_ready": True, "credentials": {"linux_update_signing": True}}}}

    def test_release_schema_and_binary_round_trip(self):
        self.archive()
        result = RELEASE.verify_artifacts(self.manifest, self.root)
        self.assertEqual(len(result), 1)

    def test_tampering_and_size_mismatch_rejected(self):
        path = self.artifact()
        path.write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            RELEASE.verify_artifacts(self.manifest, self.root)
        self.manifest["platforms"][self.platform]["artifacts"][0].update(RELEASE.file_identity(path))
        self.manifest["platforms"][self.platform]["artifacts"][0]["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            RELEASE.verify_artifacts(self.manifest, self.root)

    def test_embedded_version_source_clean_and_platform_are_independent_gates(self):
        for overrides, message in [({"version": "1.2.2"}, "version mismatch"),
                                   ({"source_revision": "b" * 40}, "source mismatch"),
                                   ({"source_dirty": True}, "not clean"),
                                   ({"platform": "windows"}, "platform mismatch"),
                                   ({"architecture": "aarch64"}, "architecture mismatch")]:
            with self.subTest(overrides=overrides):
                self.manifest["platforms"][self.platform]["artifacts"] = []
                self.archive(**overrides)
                with self.assertRaisesRegex(ValueError, message):
                    RELEASE.verify_artifacts(self.manifest, self.root)

    def test_windows_legacy_or_unsigned_bundle_is_not_promotable(self):
        self.platform = "windows-x64"
        self.manifest["platforms"] = {self.platform: self.entry(self.platform)}
        payload = {"version": "1.2.3", "source_revision": REVISION, "architecture": "x64"}
        for clean_fields, expected in [({}, "not clean"), ({"source_dirty": False, "platform": "windows"}, "Authenti")]:
            self.manifest["platforms"][self.platform]["artifacts"] = []
            path = self.root / "LightTable-1.2.3-windows-x64.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("LightTable/build-manifest.json", json.dumps({**payload, **clean_fields}))
            self.manifest["platforms"][self.platform]["artifacts"].append({**RELEASE.file_identity(path), "url": RELEASE.asset_url(self.manifest, path.name, self.platform)})
            with self.assertRaisesRegex(ValueError, expected):
                RELEASE.verify_artifacts(self.manifest, self.root)

    def test_archive_duplicate_metadata_rejected(self):
        path = self.root / "LightTable-1.2.3-linux-x86_64.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for _ in range(2):
                info = tarfile.TarInfo("LightTable/build-manifest.json")
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
        self.manifest["platforms"][self.platform]["artifacts"].append({**RELEASE.file_identity(path), "url": RELEASE.asset_url(self.manifest, path.name, self.platform)})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            RELEASE.verify_artifacts(self.manifest, self.root)

    def test_symlink_artifacts_rejected(self):
        path = self.artifact()
        path.rename(self.root / "actual")
        path.symlink_to(self.root / "actual")
        with self.assertRaisesRegex(ValueError, "regular"):
            RELEASE.verify_artifacts(self.manifest, self.root)

    def test_wrong_url_owner_tag_query_and_version_rejected(self):
        self.artifact()
        valid = copy.deepcopy(self.manifest)
        asset = valid["platforms"][self.platform]["artifacts"][0]
        for url in [asset["url"].replace("reville/", "someone/"), asset["url"].replace("v1.2.3", "v1.2.2"), asset["url"] + "?token=secret", asset["url"].replace("https:", "http:")]:
            self.manifest = copy.deepcopy(valid)
            self.manifest["platforms"][self.platform]["artifacts"][0]["url"] = url
            with self.assertRaisesRegex(ValueError, "ownership"):
                RELEASE.validate_manifest(self.manifest)
        self.manifest = copy.deepcopy(valid)
        self.manifest["platforms"][self.platform]["version"] = "2.0.0"
        with self.assertRaisesRegex(ValueError, "version differs"):
            RELEASE.validate_manifest(self.manifest)

    def test_name_collision_and_unscoped_checksums_rejected(self):
        self.artifact()
        entry = self.manifest["platforms"][self.platform]
        entry["artifacts"].append(copy.deepcopy(entry["artifacts"][0]))
        with self.assertRaisesRegex(ValueError, "name collision"):
            RELEASE.validate_manifest(self.manifest)
        entry["artifacts"] = []
        self.artifact("SHA256SUMS")
        with self.assertRaisesRegex(ValueError, "does not own"):
            RELEASE.validate_manifest(self.manifest)

    def test_arch_package_filename_preserves_public_convention(self):
        self.artifact("lighttable-bin-1.2.3-1-x86_64.pkg.tar.zst")
        self.artifact("lighttable-bin-1.2.3-1-x86_64.pkg.tar.zst.sha256")
        RELEASE.validate_manifest(self.manifest)
        self.manifest["platforms"][self.platform]["artifacts"][0]["name"] = "lighttable-bin-1.2.3-0-x86_64.pkg.tar.zst"
        with self.assertRaisesRegex(ValueError, "does not own"):
            RELEASE.validate_manifest(self.manifest)

    def test_beta_mac_aggregate_keeps_explicit_tag_and_same_tree_provenance(self):
        self.artifact()
        entry = self.entry("macos-arm64")
        entry.update(version="1.2.3-beta.1", channel="beta", update_owner="manual", signing="ad-hoc", tag="macos-v1.2.3-beta.1", build_source_revision="b" * 40, source_tree="c" * 40)
        self.manifest["platforms"]["macos-arm64"] = entry
        self.artifact("LightTable-1.2.3-beta.1-macos-arm64.dmg", platform="macos-arm64")
        RELEASE.validate_manifest(self.manifest)
        self.assertEqual(RELEASE.promotion_gates("macos-arm64", entry), [])
        entry["update_owner"] = "app"
        self.assertTrue(RELEASE.promotion_gates("macos-arm64", entry))

    def test_aggregate_preserves_older_published_platform_with_own_source_and_tag(self):
        self.artifact()
        old = copy.deepcopy(self.manifest["platforms"][self.platform])
        old.update(tag="v1.2.3", source_revision=REVISION)
        self.manifest.update(version="1.3.0", source_revision="b" * 40)
        self.manifest["platforms"][self.platform] = old
        RELEASE.validate_manifest(self.manifest)
        del old["source_revision"]
        with self.assertRaisesRegex(ValueError, "version differs"):
            RELEASE.validate_manifest(self.manifest)

    def test_partial_release_resume_skips_identical_and_adds_missing(self):
        first = self.artifact()
        self.artifact("LightTable-1.2.3-linux-x86_64-SHA256SUMS")
        plan = RELEASE.publication_plan(self.manifest, [RELEASE.file_identity(first)], [self.platform])
        actions = {item["name"]: item["action"] for item in plan}
        self.assertEqual(actions[first.name], "skip")
        self.assertEqual(list(actions.values()).count("add"), 1)
        again = RELEASE.publication_plan(self.manifest, plan, [self.platform])
        self.assertEqual({item["action"] for item in again}, {"skip"})

    def test_published_binary_mismatch_fails_even_when_size_matches(self):
        path = self.artifact()
        previous = {**RELEASE.file_identity(path), "sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "immutable asset collision"):
            RELEASE.publication_plan(self.manifest, [previous], [self.platform])

    def test_selected_ready_platform_does_not_inherit_other_platform_gate(self):
        self.artifact()
        entry = self.entry("windows-x64")
        entry.update(state="blocked", gates=["Windows 10/11 native client acceptance"])
        self.manifest["platforms"]["windows-x64"] = entry
        self.assertTrue(RELEASE.publication_plan(self.manifest, [], [self.platform]))
        with self.assertRaisesRegex(ValueError, "Windows 10/11"):
            RELEASE.publication_plan(self.manifest, [], ["windows-x64"])

    def test_receipts_and_signing_cannot_be_silently_weakened(self):
        self.artifact()
        entry = self.manifest["platforms"][self.platform]
        entry["validation"]["receipts"] = []
        with self.assertRaisesRegex(ValueError, "receipts"):
            RELEASE.publication_plan(self.manifest, [], [self.platform])
        entry["validation"]["receipts"] = ["receipt.json"]
        entry["signing"] = "unsigned"
        with self.assertRaisesRegex(ValueError, "ed25519"):
            RELEASE.publication_plan(self.manifest, [], [self.platform])

    def test_prebuild_preflight_needs_no_artifacts_or_acceptance_receipt(self):
        entry = self.manifest["platforms"][self.platform]
        entry.update(state="candidate", validation={"status": "pending", "receipts": []})
        caps = self.capabilities()
        caps["platforms"][self.platform]["native_acceptance_ready"] = False
        self.assertTrue(RELEASE.preflight(self.manifest, caps, [self.platform], "build")["ready"])
        self.assertFalse(RELEASE.preflight(self.manifest, caps, [self.platform], "publish")["ready"])
        with self.assertRaisesRegex(ValueError, "no artifacts"):
            RELEASE.verify_artifacts(self.manifest, self.root)

    def test_preflight_reports_missing_credential_without_values(self):
        self.artifact()
        caps = self.capabilities()
        caps["platforms"][self.platform]["credentials"]["linux_update_signing"] = False
        result = RELEASE.preflight(self.manifest, caps, [self.platform])
        self.assertFalse(result["ready"])
        self.assertIn("missing credential capability", json.dumps(result))
        caps["platforms"][self.platform]["credentials"]["linux_update_signing"] = "PRIVATE KEY"
        with self.assertRaisesRegex(ValueError, "booleans"):
            RELEASE.preflight(self.manifest, caps, [self.platform])

    def test_source_tag_and_environment_fail_preflight_before_build(self):
        self.artifact()
        caps = self.capabilities()
        caps.update(source_clean=False, tag_revision="b" * 40)
        caps["platforms"][self.platform]["release_environment_allowed"] = False
        result = RELEASE.preflight(self.manifest, caps, [self.platform])
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["platforms"][self.platform]["build_gates"]), 3)

    def test_publish_plan_does_not_mutate_and_apply_only_adds(self):
        path = self.artifact()
        manifest_path = self.manifest_path()
        remote = [RELEASE.file_identity(path)]
        with patch.object(RELEASE, "remote_assets", return_value=remote), patch.object(RELEASE, "gh") as gh:
            result = RELEASE.publish(manifest_path, self.root, [self.platform])
            self.assertFalse(result["applied"])
            gh.assert_not_called()
        after = remote + [RELEASE.file_identity(manifest_path)]
        with patch.object(RELEASE, "remote_assets", side_effect=[remote, after]), patch.object(RELEASE, "gh") as gh:
            RELEASE.publish(manifest_path, self.root, [self.platform], apply=True)
            self.assertEqual(gh.call_count, 1)
            args = gh.call_args.args
            self.assertEqual(args[:3], ("release", "upload", "v1.2.3"))
            self.assertNotIn("--clobber", args)
            self.assertIn(str(manifest_path.resolve()), args)

    def test_changed_immutable_manifest_rejected_before_any_upload(self):
        self.artifact()
        path = self.manifest_path()
        old = {**RELEASE.file_identity(path), "sha256": "0" * 64}
        with patch.object(RELEASE, "remote_assets", return_value=[old]), patch.object(RELEASE, "gh") as gh:
            with self.assertRaisesRegex(ValueError, "immutable manifest collision"):
                RELEASE.publish(path, self.root, [self.platform], apply=True)
            gh.assert_not_called()

    def test_remote_tag_mismatch_prevents_publication(self):
        self.artifact()
        with patch.object(RELEASE, "gh", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "tag source mismatch"):
                RELEASE.remote_assets(self.manifest)

    def test_remote_digest_and_legacy_download_fallback(self):
        path = self.artifact()
        identity = RELEASE.file_identity(path)
        for digest in ["sha256:" + identity["sha256"], None]:
            calls = []
            def fake_gh(*args):
                calls.append(args)
                if args[0] == "api":
                    return REVISION
                if args[:2] == ("release", "view"):
                    return json.dumps({"tagName": "v1.2.3", "assets": [{"name": path.name, "size": identity["bytes"], "digest": digest}]})
                destination = Path(args[args.index("--dir") + 1])
                (destination / path.name).write_bytes(path.read_bytes())
                return ""
            with patch.object(RELEASE, "gh", side_effect=fake_gh):
                self.assertEqual(RELEASE.remote_assets(self.manifest), [identity])
            self.assertEqual(any(call[:2] == ("release", "download") for call in calls), digest is None)

    def test_cli_create_verify_and_offline_plan(self):
        artifact = self.archive()
        output = self.root / "created.json"
        command = [sys.executable, str(SCRIPT), "create-manifest", "--version", "1.2.3", "--source-revision", REVISION,
                   "--platform", self.platform, "--channel", "stable", "--minimum-os", "Ubuntu 24.04+", "--update-owner", "app",
                   "--state", "ready", "--signing", "ed25519", "--validation-status", "passed", "--receipt", "native.json",
                   "--artifact", artifact.name, "--artifacts-dir", str(self.root), "--output", str(output)]
        created = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        verified = subprocess.run([sys.executable, str(SCRIPT), "verify", "--manifest", str(output), "--artifacts-dir", str(self.root)], capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        existing = self.root / "existing.json"
        existing.write_text("[]")
        planned = subprocess.run([sys.executable, str(SCRIPT), "plan", "--manifest", str(output), "--existing", str(existing), "--platform", self.platform], capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(json.loads(planned.stdout)["assets"][0]["action"], "add")


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_installer_archive_is_deterministic(self):
        one, two = self.root / "one.tar.gz", self.root / "two.tar.gz"
        PREPARE.deterministic_archive({"z/installer": "last", "a/installer": "first"}, one)
        PREPARE.deterministic_archive({"a/installer": "first", "z/installer": "last"}, two)
        self.assertEqual(one.read_bytes(), two.read_bytes())
        with tarfile.open(one) as archive:
            self.assertEqual(archive.getnames(), ["a/installer", "z/installer"])
            self.assertTrue(all(member.mtime == 0 and member.uid == 0 for member in archive))

    def test_existing_public_feed_is_reused_only_when_digest_and_size_match(self):
        content = b'{"signed":"immutable feed"}'
        source = self.root / "source.json"
        source.write_bytes(content)
        identity = RELEASE.file_identity(source)
        metadata = {"draft": False, "assets": [{"name": "linux-x86_64.json", "size": identity["bytes"], "digest": "sha256:" + identity["sha256"]}]}
        def gh(*args):
            if args[0] == "api":
                return json.dumps(metadata)
            destination = Path(args[args.index("--dir") + 1])
            (destination / "linux-x86_64.json").write_bytes(content)
        output = self.root / "linux-x86_64.json"
        with patch.object(RELEASE, "gh", side_effect=gh):
            self.assertTrue(PREPARE.reuse_public_feed("v1.2.3", output.name, output))
        self.assertEqual(output.read_bytes(), content)
        metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
        with patch.object(RELEASE, "gh", side_effect=gh):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                PREPARE.reuse_public_feed("v1.2.3", output.name, output)

    def test_missing_release_allows_new_feed_but_auth_failure_does_not(self):
        for stderr, expected in [("HTTP 404: Not Found", False), ("HTTP 403: Forbidden", None)]:
            error = subprocess.CalledProcessError(1, ["gh"], stderr=stderr)
            with patch.object(RELEASE, "gh", side_effect=error):
                if expected is False:
                    self.assertFalse(PREPARE.reuse_public_feed("v1.2.3", "linux-x86_64.json", self.root / "feed"))
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        PREPARE.reuse_public_feed("v1.2.3", "linux-x86_64.json", self.root / "feed")

    def test_failed_fork_and_unrelated_build_runs_rejected(self):
        base = {"status": "completed", "event": "workflow_dispatch", "conclusion": "success", "head_repository": {"full_name": RELEASE.REPOSITORY}, "path": ".github/workflows/windows-build.yml", "head_sha": REVISION}
        for change in [{"status": "in_progress"}, {"event": "pull_request"}, {"head_repository": {"full_name": "someone/fork"}}, {"path": ".github/workflows/unrelated.yml"}]:
            with patch.object(RELEASE, "gh", return_value=json.dumps({**base, **change})):
                with self.assertRaises(ValueError):
                    PREPARE.build_run("123", "windows-x64")

    def test_other_platform_failure_does_not_block_successful_selected_package(self):
        run = {"status": "completed", "event": "workflow_dispatch", "conclusion": "failure", "head_repository": {"full_name": RELEASE.REPOSITORY}, "path": ".github/workflows/release.yml", "head_sha": REVISION}
        jobs = [{"jobs": [{"id": 1, "name": "macos", "conclusion": "failure"}, {"id": 2, "name": "windows / package", "conclusion": "success", "steps": [{"name": "Require native edit, export and restart", "conclusion": "success"}]}]}]
        with patch.object(RELEASE, "gh", side_effect=[json.dumps(run), json.dumps(jobs)]):
            result = PREPARE.build_run("123", "windows-x64")
        self.assertEqual(result["selected_job_id"], 2)
        jobs[0]["jobs"][1]["steps"][0]["conclusion"] = "skipped"
        with patch.object(RELEASE, "gh", side_effect=[json.dumps(run), json.dumps(jobs)]):
            with self.assertRaisesRegex(ValueError, "proof step"):
                PREPARE.build_run("123", "windows-x64")

    def test_prepare_preserves_candidate_bytes_and_does_not_claim_acceptance(self):
        platform, version = "windows-x64", "1.2.3"
        stem = f"LightTable-{version}-{platform}"
        original = self.root / "original"
        original.mkdir()
        with zipfile.ZipFile(original / (stem + ".zip"), "w") as archive:
            archive.writestr("LightTable/LightTable.exe", b"signed executable")
            archive.writestr("LightTable/lighttable.cmd", "@echo off")
            archive.writestr("LightTable/build-manifest.json", json.dumps({"version": version, "source_revision": REVISION, "source_dirty": False, "platform": "windows", "architecture": "x64", "authenticode_signed": True}))
        (original / (stem + "-setup.exe")).write_bytes(b"immutable signed installer")
        (original / "appcast-windows-x64.xml").write_text("<signed-feed />")
        (original / "windows-signatures.json").write_text('{"fixture":"signature evidence verified at promotion"}')
        before = {path.name: RELEASE.file_identity(path) for path in original.iterdir()}
        run = {"head_sha": "b" * 40}
        def download(*args):
            if args[0] == "api":
                return REVISION
            self.assertEqual(args[:2], ("run", "download"))
            destination = Path(args[args.index("--dir") + 1])
            for path in original.iterdir():
                (destination / path.name).write_bytes(path.read_bytes())
        args = SimpleNamespace(platform=platform, version=version, build_run_id="123", native_run_id="456", source_revision=REVISION, output_dir=self.root / "prepared")
        with patch.object(PREPARE, "build_run", return_value=run), patch.object(RELEASE, "gh", side_effect=download):
            result = PREPARE.prepare(args)
        manifest = RELEASE.read_json(result["manifest"])
        entry = manifest["platforms"][platform]
        self.assertEqual(entry["state"], "candidate")
        self.assertEqual(entry["validation"]["status"], "pending")
        self.assertEqual(entry["native_run_id"], "456")
        self.assertEqual(entry["build_workflow_revision"], "b" * 40)
        self.assertEqual(manifest["source_revision"], REVISION)
        for path in original.iterdir():
            output_name = stem + "-" + path.name if path.name == "windows-signatures.json" else path.name
            after = RELEASE.file_identity(args.output_dir / output_name)
            self.assertEqual(after["sha256"], before[path.name]["sha256"])
        RELEASE.verify_artifacts(manifest, args.output_dir)
        with self.assertRaisesRegex(ValueError, "blocked"):
            RELEASE.publication_plan(manifest, [], [platform])


if __name__ == "__main__":
    unittest.main()
