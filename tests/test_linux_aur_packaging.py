from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path
import tempfile
import tarfile
import unittest

from test_linux_arch_packaging import fixture


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aur_package", ROOT / "scripts/linux/make-aur-package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
REVISION = "a" * 40


def metadata(text):
    result = {}
    for line in text.splitlines():
        if " = " in line:
            key, value = line.strip().split(" = ", 1)
            result.setdefault(key, []).append(value)
    return result


class AurPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "release.tar.gz"
        self.output = self.root / "aur"
        self.manifest = {"version": "0.5.0", "source_revision": REVISION, "source_dirty": False}
        fixture(self.archive, manifest=self.manifest)

    def generate(self, **kwargs):
        return package.generate(self.archive, self.output, version="0.5.0", source_revision=REVISION, **kwargs)

    def test_release_recipe_pins_official_asset_and_srcinfo_matches_runtime_metadata(self):
        recipe = self.generate()
        info = metadata((self.output / ".SRCINFO").read_text())
        url = "https://github.com/reville/lighttable-digital-darkroom/releases/download/v0.5.0/LightTable-0.5.0-linux-x86_64.tar.gz"
        self.assertEqual(info["source"], [url, "app.lighttable.LightTable.desktop"])
        self.assertEqual(info["pkgver"], ["0.5.0"])
        self.assertEqual(info["pkgrel"], ["1"])
        self.assertEqual(info["pkgname"], ["lighttable-bin"])
        self.assertEqual(info["arch"], ["x86_64"])
        self.assertEqual(info["provides"], ["lighttable=0.5.0"])
        self.assertEqual(info["conflicts"], ["lighttable"])
        self.assertIn("webkit2gtk-4.1", info["depends"])
        self.assertIn("lcms2", info["depends"])
        desktop = self.output / "app.lighttable.LightTable.desktop"
        self.assertEqual(info["sha256sums"], [hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                                              hashlib.sha256(desktop.read_bytes()).hexdigest()])
        self.assertEqual({p.name for p in self.output.iterdir()}, {"PKGBUILD", ".SRCINFO", desktop.name})
        subprocess.run(["bash", "-n", str(recipe)], check=True, capture_output=True)
        if shutil.which("makepkg"):
            result = subprocess.run(["makepkg", "--printsrcinfo"], cwd=self.output,
                                    check=True, text=True, capture_output=True)
            self.assertEqual(metadata(result.stdout), info)

    def test_pkgrel_supports_recipe_only_upgrades(self):
        self.generate(pkgrel=2)
        info = metadata((self.output / ".SRCINFO").read_text())
        self.assertEqual(info["pkgver"], ["0.5.0"])
        self.assertEqual(info["pkgrel"], ["2"])

    def test_dirty_missing_or_mismatched_release_identity_is_rejected(self):
        cases = ({"version": "0.5.0-ci.2"}, {"version": "0.5.1"},
                 {"source_dirty": True}, {"source_dirty": "false"}, {"source_dirty": None},
                 {"source_revision": "b" * 40}, {"source_revision": None},
                 {"platform": "darwin"})
        for changes in cases:
            with self.subTest(changes=changes):
                fixture(self.archive, manifest=self.manifest | changes)
                with self.assertRaises(ValueError):
                    self.generate()
                self.assertFalse(self.output.exists())

    def test_release_rejects_arm64_even_with_valid_elf(self):
        fixture(self.archive, architecture="aarch64", manifest=self.manifest)
        with self.assertRaisesRegex(ValueError, "x86_64"):
            self.generate()

    def test_release_verifier_rejects_mislabeled_elf_and_unsafe_archive_paths(self):
        fixture(self.archive, binary_architecture="aarch64", manifest=self.manifest)
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.generate()
        outside = tarfile.TarInfo("LightTable/../../outside")
        fixture(self.archive, manifest=self.manifest, extra=outside)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_canonical_input_version_source_revision_and_pkgrel_are_required(self):
        for version, revision in (("0.5", REVISION), ("0.5.0-ci.1", REVISION),
                                  ("00.5.0", REVISION), ("0.5.0", "main")):
            with self.subTest(version=version, revision=revision), self.assertRaises(ValueError):
                package.generate(self.archive, self.output, version=version, source_revision=revision)
        with self.assertRaisesRegex(ValueError, "pkgrel"):
            self.generate(pkgrel=0)
        self.assertFalse(self.output.exists())

    def test_checksum_mismatch_and_existing_recipe_are_preserved(self):
        sidecar = self.archive.with_name(self.archive.name + ".sha256")
        sidecar.write_text("0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.generate()
        self.assertFalse(self.output.exists())
        sidecar.unlink()
        self.output.mkdir()
        existing = self.output / "PKGBUILD"
        existing.write_text("preserve existing recipe")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.generate()
        self.assertEqual(existing.read_text(), "preserve existing recipe")


if __name__ == "__main__":
    unittest.main()
