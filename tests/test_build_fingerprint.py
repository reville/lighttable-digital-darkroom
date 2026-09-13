# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import json
import os
import plistlib
import subprocess
from unittest import mock
from pathlib import Path
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_fingerprint.py"
SPEC = importlib.util.spec_from_file_location("build_fingerprint", MODULE_PATH)
assert SPEC and SPEC.loader
build_fingerprint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_fingerprint)


class BuildFingerprintTests(unittest.TestCase):
    def make_tree(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "rust-engine/src").mkdir(parents=True)
        (root / "rust-engine/data").mkdir()
        (root / "rust-engine/Cargo.toml").write_text("[package]\nname='engine'\n")
        (root / "rust-engine/src/lib.rs").write_text("pub fn render() {}\n")
        (root / "rust-engine/build.rs").write_text("fn main() {}\n")
        (root / "rust-engine/data/profile.json").write_text("{}\n")
        (root / "rust-engine/vendor/rawloader/src").mkdir(parents=True)
        (root / "rust-engine/vendor/rawloader/src/lib.rs").write_text("pub fn decode() {}\n")
        (root / "rust-engine/target").mkdir()
        (root / "rust-engine/target/stale").write_text("ignore me")
        (root / ".git").mkdir()
        (root / ".git/ignored").write_text("ignore me")
        files = [
            "app_version.py",
            "app/main.swift", "app/NativePreview.swift", "app/DiagnosticReports.swift",
            "app/NativePreview.metal", "app/Info.plist",
            "LightTable.xcodeproj/project.pbxproj",
            "LightTable.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
            "scripts/update-personal-app.sh", "scripts/build_fingerprint.py",
            "film_lab_ai/vision_helper.swift", "film_lab_ai/enhance_helper.swift",
            "media-formats.json", "lighttable", "lighttable_cli/cli.py",
            "web/index.html", "profiles/default.json", "presets/default.json",
            "scripts/models/models.json", "scripts/models/SCUNet-CODE-LICENSE.txt",
            "scripts/models/SCUNet-WEIGHTS-LICENSE.txt",
            "scripts/models/selfie_multiclass_256x256.tflite",
            "scripts/models/HairSegmentation-APACHE-2.0.txt",
            "scripts/models/hair-model.json",
        ]
        for relative in files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        (root / "scripts/models/denoise.mlpackage/weights").mkdir(parents=True)
        (root / "scripts/models/denoise.mlpackage/weights/model.bin").write_text("fixture")
        (root / "app/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleVersion": "1", "CFBundleShortVersionString": "0.7.6",
            "LSMinimumSystemVersion": "13.0", "CFBundleIdentifier": "com.example.app",
        }))
        (root / "LightTable.xcodeproj/project.pbxproj").write_text(
            "{\n  MARKETING_VERSION = 0.7.6;\n  CURRENT_PROJECT_VERSION = 1;\n  ENABLE_HARDENED_RUNTIME = YES;\n}\n")
        return directory, root

    def test_vendored_rust_sources_assets_and_build_script_are_included(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        for relative in ("rust-engine/vendor/rawloader/src/lib.rs", "rust-engine/build.rs",
                         "rust-engine/data/profile.json"):
            with self.subTest(input=relative):
                before = build_fingerprint.build_fingerprints(root, {})
                path = root / relative
                path.write_bytes(path.read_bytes() + b"changed")
                after = build_fingerprint.build_fingerprints(root, {})
                self.assertNotEqual(before["ENGINE_HASH"], after["ENGINE_HASH"])
                self.assertNotEqual(before["SOURCE_TREE_HASH"], after["SOURCE_TREE_HASH"])
                self.assertEqual(before["NATIVE_HASH"], after["NATIVE_HASH"])

    def test_presets_and_hair_assets_invalidate_their_packaged_fingerprints(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        for relative, key in (
            ("presets/default.json", "SOURCE_TREE_HASH"),
            ("scripts/models/selfie_multiclass_256x256.tflite", "MODEL_HASH"),
            ("scripts/models/HairSegmentation-APACHE-2.0.txt", "MODEL_HASH"),
            ("scripts/models/hair-model.json", "MODEL_HASH"),
        ):
            with self.subTest(input=relative):
                before = build_fingerprint.build_fingerprints(root, {})
                path = root / relative
                path.write_bytes(path.read_bytes() + b"changed")
                after = build_fingerprint.build_fingerprints(root, {})
                self.assertNotEqual(before[key], after[key])
                self.assertEqual(before["ENGINE_HASH"], after["ENGINE_HASH"])

    def test_generated_cargo_output_does_not_invalidate_engine(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        before = build_fingerprint.build_fingerprints(root, {})
        (root / "rust-engine/target/stale").write_text("new compiler output")
        self.assertEqual(before, build_fingerprint.build_fingerprints(root, {}))

    def test_required_missing_and_dangling_inputs_fail_closed(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        for missing in ("missing.swift", "missing/*.swift"):
            with self.subTest(input=missing), self.assertRaises(build_fingerprint.FingerprintError):
                build_fingerprint.fingerprint(root, [missing], component="test")
        (root / "rust-engine/src/broken.rs").symlink_to("absent.rs")
        with self.assertRaises(build_fingerprint.FingerprintError):
            build_fingerprint.fingerprint(root, ["rust-engine"], component="engine")

    def test_cargo_configuration_contents_change_identity(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        cargo_home = root / "cargo-home"
        cargo_home.mkdir()
        config = cargo_home / "config.toml"
        config.write_text('[build]\nrustflags = ["--cfg=first"]\n')
        with mock.patch.dict(os.environ, {"CARGO_HOME": str(cargo_home)}):
            before = build_fingerprint.cargo_configuration(root)
            config.write_text('[build]\nrustflags = ["--cfg=second"]\n')
            after = build_fingerprint.cargo_configuration(root)
        self.assertNotEqual(before, after)
        self.assertNotIn("rustflags", json.dumps(after))

    def test_tool_discovery_failure_does_not_create_a_cache_identity(self):
        with mock.patch.object(build_fingerprint.shutil, "which", return_value=None):
            with self.assertRaises(build_fingerprint.FingerprintError):
                build_fingerprint._run_capture(["absent-compiler", "--version"])

    def commit_packaging_base(self, root):
        # make_tree includes a placeholder .git; replace it with a fixture repo.
        (root / ".git/ignored").unlink()
        (root / ".git").rmdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "--quiet")
        git("add", "app", "LightTable.xcodeproj")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
        return git("rev-parse", "HEAD")

    def test_only_named_packaging_version_fields_are_exempt(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        revision = self.commit_packaging_base(root)
        plist = root / "app/Info.plist"
        values = plistlib.loads(plist.read_bytes())
        values.update(CFBundleVersion="2", CFBundleShortVersionString="0.7.7")
        plist.write_bytes(plistlib.dumps(values))
        project = root / "LightTable.xcodeproj/project.pbxproj"
        project.write_text(project.read_text().replace("0.7.6", "0.7.7").replace("VERSION = 1", "VERSION = 2"))
        self.assertTrue(build_fingerprint.version_only_change(root, revision))
        for key, value in (("LSMinimumSystemVersion", "14.0"), ("CFBundleIdentifier", "different")):
            with self.subTest(field=key):
                plist.write_bytes(plistlib.dumps(values | {key: value}))
                self.assertFalse(build_fingerprint.version_only_change(root, revision))
        plist.write_bytes(plistlib.dumps(values))
        project.write_text(project.read_text().replace("RUNTIME = YES", "RUNTIME = NO"))
        self.assertFalse(build_fingerprint.version_only_change(root, revision))

    def test_dependency_changes_and_unavailable_packaging_bases_are_rejected(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        revision = self.commit_packaging_base(root)
        self.assertFalse(build_fingerprint.version_only_change(root, "-HEAD"))
        self.assertFalse(build_fingerprint.version_only_change(root, "f" * 40))
        package = root / build_fingerprint.PACKAGING_INPUTS[2]
        package.write_text("new dependency lock")
        self.assertFalse(build_fingerprint.version_only_change(root, revision))

    def test_full_build_provenance_matches_the_legacy_shell_hash(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        import hashlib
        # The historical shell sorted complete `shasum` lines, then hashed them.
        expected_lines = sorted(
            hashlib.sha256((root / path).read_bytes()).hexdigest() + "  " + path
            for path in build_fingerprint.PACKAGING_INPUTS
        )
        expected = hashlib.sha256(("\n".join(expected_lines) + "\n").encode()).hexdigest()
        result = subprocess.run(
            [build_fingerprint.sys.executable, str(MODULE_PATH), "--root", str(root),
             "--packaging-only", "--json"], check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout), {"LEGACY_XCODE_CONFIG_HASH": expected})
        self.assertEqual(build_fingerprint.build_fingerprints(root, {})["LEGACY_XCODE_CONFIG_HASH"], expected)

    def test_unrelated_file_and_mtime_do_not_change_fingerprint(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        toolchain = {"version": 1, "compiler": "swift 6", "sdk": "macos"}
        before = build_fingerprint.fingerprint(root, ["rust-engine"], component="engine", toolchain=toolchain)
        (root / "unrelated.txt").write_text("outside the selected input")
        original = (root / "rust-engine/src/lib.rs").stat()
        time.sleep(0.01)
        (root / "rust-engine/src/lib.rs").touch()
        self.assertNotEqual(original.st_mtime_ns, (root / "rust-engine/src/lib.rs").stat().st_mtime_ns)
        after = build_fingerprint.fingerprint(root, ["rust-engine"], component="engine", toolchain=toolchain)
        self.assertEqual(before, after)

    def test_toolchain_change_invalidates_compiled_component(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        first = build_fingerprint.fingerprint(root, ["rust-engine"], component="engine", toolchain={"version": 1, "rustc": "rustc 1.80"})
        second = build_fingerprint.fingerprint(root, ["rust-engine"], component="engine", toolchain={"version": 1, "rustc": "rustc 1.81"})
        self.assertNotEqual(first, second)

    def test_same_content_in_different_checkouts_is_stable(self):
        first_temporary, first = self.make_tree()
        second_temporary, second = self.make_tree()
        self.addCleanup(first_temporary.cleanup)
        self.addCleanup(second_temporary.cleanup)
        toolchain = {"version": 1, "rustc": "rustc 1.80"}
        self.assertEqual(
            build_fingerprint.fingerprint(first, ["rust-engine"], component="engine", toolchain=toolchain),
            build_fingerprint.fingerprint(second, ["rust-engine"], component="engine", toolchain=toolchain),
        )

    def test_symlinked_source_is_rejected_instead_of_skipped(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        (root / "rust-engine/src/linked.rs").symlink_to(root / "rust-engine/src/lib.rs")
        with self.assertRaises(build_fingerprint.FingerprintError):
            build_fingerprint.fingerprint(root, ["rust-engine"], component="engine", toolchain={"version": 1})

    def test_cli_accepts_injected_toolchain_data_without_tool_discovery(self):
        temporary, root = self.make_tree()
        self.addCleanup(temporary.cleanup)
        injected = json.dumps({
            "native": {"version": 1, "compiler": "test-swift"},
            "helper": {"version": 1, "compiler": "test-swift"},
            "engine": {"version": 1, "compiler": "test-rust"},
        })
        values = build_fingerprint.build_fingerprints(root, json.loads(injected))
        self.assertEqual(values["FINGERPRINT_FORMAT"], "build-fingerprint-v2")
        self.assertTrue(values["NATIVE_TOOLCHAIN_HASH"])
        self.assertTrue(values["ENGINE_TOOLCHAIN_HASH"])


if __name__ == "__main__":
    unittest.main()
