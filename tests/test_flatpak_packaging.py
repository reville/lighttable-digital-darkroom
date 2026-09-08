"""Offline validation of direct Flatpak generation and sandbox boundaries."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SHA = "1" * 40


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts/flatpak" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


CANDIDATE = module("make-candidate")
INSTALL = module("install-candidate")
AUDIT = module("audit-python-sources")


def fixture(path, **patch):
    manifest = dict(version="0.5.0", source_revision=SHA, source_dirty=False,
                    architecture="x86_64", platform="linux")
    manifest.update(patch)
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62 if manifest["architecture"] == "x86_64" else 183).to_bytes(2, "little")
    files = {"build-manifest.json": json.dumps(manifest).encode(),
             "bin/lighttable-desktop-shell": bytes(header),
             "Resources/LightTable/engine/lighttable-engine": bytes(header),
             "Resources/LightTable/engine/spektrafilm-rs": bytes(header),
             "Resources/LightTable/build/icon-1024.png": b"icon",
             "Resources/LightTable/licenses/notice.txt": b"dependency notice",
             "LICENSE": b"GPL", "THIRD_PARTY_NOTICES.md": b"notices",
             "install.sh": b"installer", "uninstall.sh": b"uninstaller",
             "desktop-integration.py": b"registration"}
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo("LightTable/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FlatpakPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "bundle.tar.gz"
        self.digest = fixture(self.archive)

    def generate(self, **changes):
        values = dict(version="0.5.0", source_revision=SHA, sha256=self.digest)
        values.update(changes)
        return CANDIDATE.generate(self.archive, self.root / "candidate", **values)

    def test_candidate_preserves_exact_archive_and_records_non_flathub_provenance(self):
        path = self.generate()
        document = json.loads(path.read_text())
        source = document["modules"][-1]["sources"][0]
        self.assertEqual(source["sha256"], self.digest)
        self.assertEqual(hashlib.sha256((path.parent / source["path"]).read_bytes()).hexdigest(), self.digest)
        provenance = json.loads((path.parent / "candidate-provenance.json").read_text())
        self.assertFalse(provenance["flathub_source_build"])
        self.assertEqual(provenance["bundle_manifest"]["source_revision"], SHA)
        self.assertEqual(document["app-id"], "app.lighttable.LightTable")
        self.assertFalse((path.parent / "flathub.json").exists())

    def test_wrong_hash_version_or_source_is_rejected_before_staging(self):
        for changes in ({"sha256": "0" * 64}, {"version": "0.5.1"}, {"source_revision": "2" * 40}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.generate(**changes)
            self.assertFalse((self.root / "candidate").exists())

    def test_dirty_and_wrong_architecture_are_not_relabelled(self):
        for patch in ({"source_dirty": True}, {"architecture": "aarch64"}):
            self.digest = fixture(self.archive, **patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.generate()

    def test_existing_output_is_preserved(self):
        output = self.root / "candidate"
        output.mkdir()
        (output / "existing").write_text("keep")
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual((output / "existing").read_text(), "keep")

    def test_runtime_has_no_host_filesystem_ipc_or_execution_escape(self):
        document = json.loads(self.generate().read_text())
        self.assertEqual(set(document["finish-args"]), {
            "--socket=wayland", "--socket=fallback-x11", "--device=dri",
            "--share=network", "--env=GTK_USE_PORTAL=1"})
        for entry in document["modules"]:
            self.assertNotIn("build-args", entry.get("build-options", {}))
            for source in entry["sources"]:
                self.assertTrue(source.get("sha256") or source.get("commit"))
        self.assertNotIn("flatpak-spawn", (ROOT / "scripts/flatpak/lighttable-desktop").read_text())

    def test_install_preserves_layout_and_registers_only_inside_app_prefix(self):
        manifest = self.generate()
        # The test fixture contains only regular files created by this test.
        with tarfile.open(self.archive) as archive:
            for member in archive:
                output = manifest.parent / member.name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.extractfile(member).read())
        prefix = self.root / "app"
        INSTALL.install(manifest.parent, prefix)
        self.assertTrue((prefix / "LightTable/Resources/LightTable/engine/lighttable-engine").is_file())
        self.assertFalse((prefix / "share/icons/hicolor/1024x1024").exists())
        self.assertTrue((prefix / "LightTable/Resources/LightTable/build/icon-1024.png").is_file())
        for size in (64, 128, 256):
            icon = prefix / f"share/icons/hicolor/{size}x{size}/apps/app.lighttable.LightTable.png"
            data = icon.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))
            self.assertEqual(data, (manifest.parent / f"icon-{size}.png").read_bytes())
        self.assertEqual((prefix / "share/licenses/app.lighttable.LightTable/lighttable/dependencies/notice.txt").read_text(), "dependency notice")
        self.assertFalse((prefix / "LightTable/install.sh").exists())
        self.assertTrue(os.access(prefix / "bin/lighttable-desktop", os.X_OK))

    def test_launchers_require_private_xdg_paths_and_use_shared_instance_location(self):
        for name in ("lighttable-desktop", "lighttable-cli"):
            path = ROOT / "scripts/flatpak" / name
            subprocess.run(["sh", "-n", str(path)], check=True)
            environment = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
            result = subprocess.run(["sh", str(path)], env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("XDG_DATA_HOME", result.stderr)
            self.assertIn('app/app.lighttable.LightTable/instances', path.read_text())

    def test_metainfo_uses_linux_screenshots_and_desktop_matches_supported_protocol(self):
        tree = ET.parse(ROOT / "packaging/flatpak/app.lighttable.LightTable.metainfo.xml")
        self.assertEqual(tree.findtext("id"), "app.lighttable.LightTable")
        self.assertEqual(tree.findtext("metadata_license"), "CC0-1.0")
        self.assertEqual(tree.find("./releases/release").get("type"), "development")
        for image in tree.findall("./screenshots/screenshot/image"):
            self.assertTrue(image.text.startswith("https://lighttable.app/screenshots/linux/"))
        desktop = (ROOT / "packaging/flatpak/app.lighttable.LightTable.desktop").read_text()
        self.assertIn("Exec=lighttable-desktop %u", desktop)
        self.assertIn("MimeType=x-scheme-handler/lighttable;", desktop)
        self.assertNotIn("image/jpeg", desktop)  # shell accepts preset URLs, not file arguments

    def test_source_inventory_covers_current_lock_without_claiming_a_complete_build(self):
        AUDIT.check(json.loads(AUDIT.TARGET.read_text()))


if __name__ == "__main__":
    unittest.main()
