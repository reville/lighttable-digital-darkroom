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
SPEC = importlib.util.spec_from_file_location("rpm_package", ROOT / "scripts/linux/make-rpm-package.py")
rpm_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rpm_package)


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


class RpmPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "LightTable-0.1.0-ci.1-linux-x86_64.tar.gz"
        self.output = self.root / "rpm-build"

    def test_spec_pins_archive_and_declares_fedora_dependencies(self):
        fixture(self.archive)
        spec_path = rpm_package.generate(self.archive, self.output)
        self.assertTrue(spec_path.is_file())
        content = spec_path.read_text()

        self.assertIn("Name:           lighttable\n", content)
        self.assertIn("Version:        0.1.0.ci.1\n", content)
        self.assertIn("Release:        1%{?dist}\n", content)
        self.assertIn("ExclusiveArch:  x86_64\n", content)
        self.assertIn("Source0:        LightTable-0.1.0-ci.1-linux-x86_64.tar.gz\n", content)
        self.assertIn("Requires:       webkit2gtk4.1\n", content)
        self.assertIn("Requires:       openblas\n", content)
        self.assertIn("Requires:       gtk3\n", content)
        self.assertIn('printf \'{"owner":"rpm"}\\n\' > %{buildroot}/opt/lighttable/installation-owner.json', content)

        desktop_path = self.output / "SOURCES/app.lighttable.LightTable.desktop"
        self.assertTrue(desktop_path.is_file())
        desktop = desktop_path.read_text()
        self.assertIn("StartupWMClass=app.lighttable.LightTable\n", desktop)
        self.assertIn("x-scheme-handler/lighttable", desktop)

    def test_arm64_requires_explicit_experimental_flag(self):
        arm_archive = self.root / "LightTable-0.1.0-ci.1-linux-aarch64.tar.gz"
        fixture(arm_archive, "aarch64")
        with self.assertRaisesRegex(ValueError, "experimental-aarch64"):
            rpm_package.generate(arm_archive, self.output)
        spec_path = rpm_package.generate(arm_archive, self.output, experimental_aarch64=True)
        content = spec_path.read_text()
        self.assertIn("ExclusiveArch:  aarch64\n", content)
        self.assertIn("experimental ARM64", content)

    def test_mislabeled_native_binaries_are_rejected(self):
        fixture(self.archive, "x86_64", binary_architecture="aarch64")
        with self.assertRaisesRegex(ValueError, "does not match manifest architecture"):
            rpm_package.generate(self.archive, self.output)

    def test_checksum_mismatch_is_rejected(self):
        fixture(self.archive)
        self.archive.with_name(self.archive.name + ".sha256").write_text("0" * 64 + f"  {self.archive.name}\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            rpm_package.generate(self.archive, self.output)

    def test_archive_traversal_rejected(self):
        outside = tarfile.TarInfo("LightTable/../../outside")
        fixture(self.archive, extra=outside)
        with self.assertRaises(ValueError):
            rpm_package.generate(self.archive, self.output)


if __name__ == "__main__":
    unittest.main()
