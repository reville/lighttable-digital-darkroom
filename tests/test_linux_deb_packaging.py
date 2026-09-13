# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deb_package", ROOT / "scripts/linux/make-deb-package.py")
deb_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deb_package)


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


class DebPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "LightTable-0.1.0-ci.1-linux-x86_64.tar.gz"
        self.output = self.root / "deb-out"

    def test_deb_generation_and_internal_structure(self):
        fixture(self.archive)
        deb_file = deb_package.generate(self.archive, self.output)
        self.assertTrue(deb_file.is_file())
        self.assertEqual(deb_file.name, "lighttable_0.1.0.ci.1-1_amd64.deb")

        # Verify ar format
        content = deb_file.read_bytes()
        self.assertTrue(content.startswith(b"!<arch>\n"))
        self.assertIn(b"debian-binary", content)
        self.assertIn(b"control.tar.gz", content)
        self.assertIn(b"data.tar.gz", content)

    def test_arm64_requires_explicit_experimental_flag(self):
        arm_archive = self.root / "LightTable-0.1.0-ci.1-linux-aarch64.tar.gz"
        fixture(arm_archive, "aarch64")
        with self.assertRaisesRegex(ValueError, "experimental-aarch64"):
            deb_package.generate(arm_archive, self.output)
        deb_file = deb_package.generate(arm_archive, self.output, experimental_aarch64=True)
        self.assertTrue(deb_file.name.endswith("_arm64.deb"))

    def test_mislabeled_native_binaries_are_rejected(self):
        fixture(self.archive, "x86_64", binary_architecture="aarch64")
        with self.assertRaisesRegex(ValueError, "does not match manifest architecture"):
            deb_package.generate(self.archive, self.output)

    def test_checksum_mismatch_is_rejected(self):
        fixture(self.archive)
        self.archive.with_name(self.archive.name + ".sha256").write_text("0" * 64 + f"  {self.archive.name}\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            deb_package.generate(self.archive, self.output)

    def test_archive_traversal_rejected(self):
        outside = tarfile.TarInfo("LightTable/../../outside")
        fixture(self.archive, extra=outside)
        with self.assertRaises(ValueError):
            deb_package.generate(self.archive, self.output)


if __name__ == "__main__":
    unittest.main()
