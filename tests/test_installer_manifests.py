"""Release channel contracts, exercised through the public generator command."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from test_linux_arch_packaging import fixture as linux_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate-installers.py"
REPO_URL = "https://github.com/reville/lighttable-digital-darkroom"


class InstallerManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "release"
        self.artifacts.mkdir()
        self.output = self.root / "installers"

    def artifact(self, suffix, content=b"release-test-fixture"):
        path = self.artifacts / f"LightTable-1.2.3-{suffix}"
        path.write_bytes(content)
        return path

    def portable(self, include_cli=True):
        path = self.artifacts / "LightTable-1.2.3-windows-x64.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("LightTable/LightTable.exe", b"test shell")
            if include_cli:
                archive.writestr("LightTable/lighttable.cmd", "@echo off\n")
        return path

    def generate(self, *args, version="1.2.3", successful=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--version", version,
             "--artifacts-dir", str(self.artifacts), "--output-dir", str(self.output), *args],
            capture_output=True, text=True, check=False,
        )
        if successful:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_mac_only_needs_no_license_and_exposes_bundled_cli(self):
        artifact = self.artifact("macos-arm64.dmg", b"macOS fixture")
        self.generate()
        cask = (self.output / "homebrew/Casks/lighttable.rb").read_text()
        expected_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertIn(f'sha256 "{expected_hash}"', cask)
        self.assertIn(f"{REPO_URL}/releases/download/v1.2.3/{artifact.name}", cask)
        self.assertIn('binary "#{appdir}/LightTable.app/Contents/MacOS/lighttable-cli", target: "lighttable"', cask)
        self.assertIn("depends_on arch: :arm64", cask)
        self.assertIn("depends_on macos: :ventura", cask)
        self.assertIn("auto_updates true", cask)
        self.assertNotIn("zap", cask)
        self.assertEqual((self.output / "SHA256SUMS").read_text(), f"{expected_hash}  {artifact.name}\n")
        self.assertFalse((self.output / "winget").exists())
        self.assertFalse((self.output / "scoop").exists())

    def test_all_windows_channels_have_real_hashes_and_consistent_identity(self):
        portable = self.portable()
        installer = self.artifact("windows-x64-setup.exe", b"NSIS fixture")
        self.generate("--license", "MIT", "--license-url", "https://example.org/license?a=1&b=2")
        scoop = json.loads((self.output / "scoop/bucket/lighttable.json").read_text())
        self.assertEqual(scoop["architecture"]["64bit"]["hash"], hashlib.sha256(portable.read_bytes()).hexdigest())
        self.assertEqual(scoop["extract_dir"], "LightTable")
        self.assertEqual(scoop["bin"], [["lighttable.cmd", "lighttable"]])
        self.assertNotIn("persist", scoop)  # The app's user-data directories live outside its installation.
        prefix = self.output / "winget/manifests/n/NicholasReville/LightTable/1.2.3"
        manifests = list(prefix.glob("*.yaml"))
        self.assertEqual(len(manifests), 3)
        for path in manifests:
            content = path.read_text()
            self.assertIn("PackageIdentifier: NicholasReville.LightTable\n", content)
            self.assertIn('PackageVersion: "1.2.3"\n', content)
        winget = (prefix / "NicholasReville.LightTable.installer.yaml").read_text()
        exe_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
        self.assertIn(f'InstallerSha256: "{exe_hash.upper()}"', winget)
        self.assertIn("InstallerType: nullsoft", winget)
        self.assertIn("Scope: user", winget)
        self.assertIn("ProductCode: LightTable", winget)
        choco_root = self.output / "chocolatey/lighttable"
        choco = (choco_root / "tools/chocolateyinstall.ps1").read_text()
        self.assertIn(f"checksum64 = '{exe_hash}'", choco)
        self.assertIn("silentArgs = '/S /UPDATEOWNER=chocolatey'", choco)
        self.assertIn('Custom: /UPDATEOWNER=winget', winget)
        self.assertIn('install-channel.txt', scoop['post_install'])
        self.assertIn("'scoop'", scoop['post_install'])
        xml = ET.parse(choco_root / "lighttable.nuspec").getroot()
        ns = {"n": "http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd"}
        self.assertEqual(xml.find("n:metadata/n:licenseUrl", ns).text, "https://example.org/license?a=1&b=2")
        self.assertEqual(xml.find("n:metadata/n:version", ns).text, "1.2.3")
        sums = (self.output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(sums), 2)  # The same EXE is shared by two channels.
        for line in sums:
            checksum, filename = line.split("  ")
            self.assertEqual(checksum, hashlib.sha256((self.artifacts / filename).read_bytes()).hexdigest())

    def test_missing_requested_channel_does_not_write_partial_output(self):
        self.artifact("macos-arm64.dmg")
        result = self.generate("--channels", "homebrew", "winget", successful=False)
        self.assertIn("winget requires", result.stderr)
        self.assertFalse(self.output.exists())

    def test_no_supported_artifacts_fails(self):
        self.artifact("unsupported.bin")
        result = self.generate(successful=False)
        self.assertIn("no supported release artifacts", result.stderr)
        self.assertFalse(self.output.exists())

    def test_linux_only_generates_verified_aur_recipe_and_checksums(self):
        artifact = self.artifacts / "LightTable-0.5.0-linux-x86_64.tar.gz"
        revision = "a" * 40
        linux_fixture(artifact, manifest={"version": "0.5.0", "source_revision": revision,
                                         "source_dirty": False})
        self.generate("--source-revision", revision, version="0.5.0")
        recipe = self.output / "aur/lighttable-bin"
        self.assertEqual({p.name for p in recipe.iterdir()},
                         {"PKGBUILD", ".SRCINFO", "app.lighttable.LightTable.desktop"})
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertIn(checksum, (recipe / "PKGBUILD").read_text())
        self.assertIn(f"{REPO_URL}/releases/download/v0.5.0/{artifact.name}",
                      (recipe / ".SRCINFO").read_text())
        self.assertEqual((self.output / "SHA256SUMS").read_text(), f"{checksum}  {artifact.name}\n")
        self.assertFalse((self.output / "homebrew").exists())

    def test_linux_release_requires_matching_clean_source_before_writing_any_channel(self):
        self.artifact("macos-arm64.dmg")
        artifact = self.artifacts / "LightTable-1.2.3-linux-x86_64.tar.gz"
        linux_fixture(artifact, manifest={"version": "1.2.3", "source_revision": "a" * 40,
                                         "source_dirty": False})
        result = self.generate(successful=False)
        self.assertIn("--source-revision", result.stderr)
        result = self.generate("--source-revision", "b" * 40, successful=False)
        self.assertIn("source revision", result.stderr)
        self.assertFalse(self.output.exists())

    def test_license_is_never_invented_for_windows(self):
        self.portable()
        result = self.generate(successful=False)
        self.assertIn("require --license", result.stderr)
        self.assertFalse(self.output.exists())
        self.artifact("windows-x64-setup.exe")
        result = self.generate("--license", "MIT", successful=False)
        self.assertIn("requires --license-url", result.stderr)
        self.assertFalse(self.output.exists())

    def test_portable_cli_must_really_be_packaged(self):
        self.portable(include_cli=False)
        result = self.generate("--license", "MIT", successful=False)
        self.assertIn("LightTable/lighttable.cmd", result.stderr)
        self.assertFalse(self.output.exists())

    def test_malformed_portable_zip_fails(self):
        self.artifact("windows-x64.zip", b"not a ZIP")
        result = self.generate("--license", "MIT", successful=False)
        self.assertIn("invalid portable ZIP", result.stderr)

    def test_invalid_versions_are_rejected_before_output(self):
        for version in ("v1.2.3", "01.2.3", "1.2", "1.2.3-rc.1", "1.2.3+build", "65536.0.0", "1.2.3\n"):
            with self.subTest(version=version):
                result = self.generate(version=version, successful=False)
                self.assertIn("version", result.stderr)
                self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.artifact("macos-arm64.dmg")
        self.output.mkdir()
        sentinel = self.output / "do-not-overwrite"
        sentinel.write_text("old release")
        result = self.generate(successful=False)
        self.assertIn("new or empty", result.stderr)
        self.assertEqual(sentinel.read_text(), "old release")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_empty_artifact_fails(self):
        self.artifact("macos-arm64.dmg", b"")
        result = self.generate(successful=False)
        self.assertIn("artifact is empty", result.stderr)

    def test_update_files_are_included_in_checksums(self):
        self.artifact("macos-arm64.dmg")
        self.artifact("macos-arm64.zip", b"Sparkle fixture")
        (self.artifacts / "appcast.xml").write_text("<rss/>")
        self.generate()
        lines = (self.output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            checksum, filename = line.split("  ")
            self.assertEqual(checksum, hashlib.sha256((self.artifacts / filename).read_bytes()).hexdigest())

    def test_unsafe_license_metadata_is_rejected(self):
        self.portable()
        for args in (("--license", "MIT\nInjected: true"),
                     ("--license", "MIT", "--license-url", "https://user:secret@example.org/license"),
                     ("--license", "MIT", "--license-url", "file:///private/license")):
            with self.subTest(args=args):
                self.generate(*args, successful=False)
                self.assertFalse(self.output.exists())

    def test_license_punctuation_is_serialized_as_data(self):
        self.portable()
        self.generate("--channels", "scoop", "--license", 'A "quoted": license')
        manifest = json.loads((self.output / "scoop/bucket/lighttable.json").read_text())
        self.assertEqual(manifest["license"], 'A "quoted": license')


if __name__ == "__main__":
    unittest.main()
