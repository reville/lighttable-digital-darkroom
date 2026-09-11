# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("arch_package", ROOT / "scripts/linux/make-arch-package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


def fixture(path: Path, architecture="x86_64", *, binary_architecture=None, extra=None, manifest=None, extra_files=None):
    machine = {"x86_64": 62, "aarch64": 183}[binary_architecture or architecture]
    elf = b"\x7fELF\x02\x01" + bytes(12) + machine.to_bytes(2, "little")
    files = {
        "build-manifest.json": json.dumps({"platform": "linux", "architecture": architecture,
                                            "version": "0.1.0-ci.1", **(manifest or {})}).encode(),
        "bin/lighttable-desktop-shell": elf,
        "Resources/LightTable/engine/lighttable-engine": elf,
        "Resources/LightTable/engine/spektrafilm-rs": elf,
        "Python/bin/python3": elf,
        "bin/lighttable": b"#!/bin/sh\nexit 0\n",
        "bin/lighttable-desktop": b"#!/bin/sh\nexit 0\n",
        "share/icons/lighttable.png": b"icon", "LICENSE": b"license",
        "THIRD_PARTY_NOTICES.md": b"notices", "install.sh": b"portable",
        "uninstall.sh": b"portable", "desktop-integration.py": b"portable",
    }
    files.update(extra_files or {})
    with tarfile.open(path, "w:gz") as archive:
        for relative, data in files.items():
            item = tarfile.TarInfo("LightTable/" + relative)
            item.size = len(data)
            item.mode = 0o755 if "/bin/" in item.name or "/engine/" in item.name else 0o644
            archive.addfile(item, io.BytesIO(data))
        if extra:
            archive.addfile(extra)


class ArchPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "bundle.tar.gz"
        self.output = self.root / "recipe"

    def test_recipe_pins_local_archive_and_declares_real_architecture(self):
        fixture(self.archive)
        recipe = package.generate(self.archive, self.output)
        result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s\\n" "$pkgver" "${arch[0]}" "${source[0]}" "${sha256sums[0]}"',
                                 "check", str(recipe)], check=True, text=True, capture_output=True)
        version, arch, source, checksum = result.stdout.splitlines()
        self.assertEqual((version, arch), ("0.1.0_ci.1", "x86_64"))
        self.assertEqual(source, "LightTable-0.1.0-ci.1-linux-x86_64.tar.gz")
        self.assertEqual(len(checksum), 64)
        self.assertTrue((self.output / source).is_file())
        desktop = (self.output / "app.lighttable.LightTable.desktop").read_text()
        self.assertIn("StartupWMClass=app.lighttable.LightTable\n", desktop)
        self.assertIn("x-scheme-handler/lighttable", desktop)
        self.assertNotIn("image/", desktop)

    def test_arm64_requires_explicit_experimental_flag(self):
        fixture(self.archive, "aarch64")
        with self.assertRaisesRegex(ValueError, "experimental-aarch64"):
            package.generate(self.archive, self.output)
        recipe = package.generate(self.archive, self.output, experimental_aarch64=True)
        self.assertIn("arch=(aarch64)", recipe.read_text())
        self.assertIn("experimental ARM64", recipe.read_text())

    def test_mislabeled_native_binaries_are_rejected(self):
        fixture(self.archive, "x86_64", binary_architecture="aarch64")
        with self.assertRaisesRegex(ValueError, "does not match manifest architecture"):
            package.generate(self.archive, self.output)
        self.assertFalse(self.output.exists())

    def test_checksum_mismatch_is_rejected_without_creating_package(self):
        fixture(self.archive)
        self.archive.with_name(self.archive.name + ".sha256").write_text("0" * 64 + "  bundle.tar.gz\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            package.generate(self.archive, self.output)
        self.assertFalse(self.output.exists())

    def test_archive_traversal_and_external_symlinks_are_rejected(self):
        outside = tarfile.TarInfo("LightTable/../../outside")
        link = tarfile.TarInfo("LightTable/link")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
        for member in (outside, link):
            with self.subTest(member=member.name):
                fixture(self.archive, extra=member)
                with self.assertRaises(ValueError):
                    package.generate(self.archive, self.output)

    @unittest.skipUnless(sys.platform == "linux", "GNU install is required")
    def test_package_stages_owned_system_files_without_touching_user_data(self):
        fixture(self.archive)
        recipe = package.generate(self.archive, self.output)
        source = self.root / "src"
        source.mkdir()
        with tarfile.open(self.archive) as archive:
            archive.extractall(source, filter="data")
        desktop = "app.lighttable.LightTable.desktop"
        (source / desktop).write_bytes((self.output / desktop).read_bytes())
        target = self.root / "pkg"
        personal = self.root / "user-data/library.sqlite3"
        personal.parent.mkdir()
        personal.write_text("keep catalog")
        subprocess.run(["bash", "-e", "-c", 'source "$1"; srcdir=$2; pkgdir=$3; package',
                        "stage", str(recipe), str(source), str(target)], check=True, capture_output=True)
        self.assertEqual(os.readlink(target / "usr/bin/lighttable"), "/opt/lighttable/bin/lighttable")
        self.assertTrue((target / "usr/share/applications" / desktop).is_file())
        self.assertTrue((target / "opt/lighttable/bin/lighttable-desktop-shell").is_file())
        self.assertFalse((target / "opt/lighttable/install.sh").exists())
        self.assertFalse((target / "opt/lighttable/uninstall.sh").exists())
        self.assertEqual({path.name for path in target.iterdir()}, {"opt", "usr"})
        self.assertEqual(personal.read_text(), "keep catalog")


if __name__ == "__main__":
    unittest.main()
