# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("snap_native_licenses", ROOT / "scripts/linux/snap-native-licenses.py")
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


class SnapNativeNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "LightTable"
        self.bundle.mkdir()
        (self.bundle / "build-manifest.json").write_text(json.dumps({
            "version": notices.VERSION, "source_revision": notices.SOURCE_REVISION,
            "source_dirty": False, "platform": "linux", "architecture": "x86_64"}))

    def install(self, **changes):
        return notices.install(self.bundle, **({"version": notices.VERSION,
                               "source_revision": notices.SOURCE_REVISION} | changes))

    def test_verified_release_installs_original_rfd_notice_and_provenance(self):
        destination = self.install()
        license_text = (destination / "crates/rfd-0.17.2/LICENSE").read_text()
        self.assertIn("Copyright (c) 2022 Bartłomiej Maryńczak", license_text)
        self.assertIn("The above copyright notice and this permission notice", license_text)
        inventory = json.loads((destination / "provenance.json").read_text())
        self.assertEqual(inventory["source_revision"], notices.SOURCE_REVISION)
        self.assertEqual(inventory["unresolved"], [])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.install()
        self.assertEqual((destination / "crates/rfd-0.17.2/LICENSE").read_text(), license_text)

    def test_wrong_source_identity_is_rejected_before_install(self):
        with self.assertRaisesRegex(ValueError, "pinned to the exact"):
            self.install(source_revision="a" * 40)
        manifest = json.loads((self.bundle / "build-manifest.json").read_text())
        manifest["source_revision"] = "a" * 40
        (self.bundle / "build-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Bundle identity"):
            self.install()
        self.assertFalse((self.bundle / "Resources").exists())

    def test_corrupted_upstream_notice_is_rejected_before_install(self):
        source = self.root / "notices"
        shutil.copytree(notices.NOTICES, source)
        inventory = json.loads((source / "provenance.json").read_text())
        rfd = next(row for row in inventory["files"] if row["path"] == "crates/rfd-0.17.2/LICENSE")
        (source / "objects" / rfd["sha256"]).write_text("corrupted upstream notice")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.install(source=source)
        self.assertFalse((self.bundle / "Resources").exists())

    def test_wrong_release_lock_is_rejected(self):
        source = self.root / "notices"
        shutil.copytree(notices.NOTICES, source)
        path = source / "provenance.json"
        inventory = json.loads(path.read_text())
        inventory["graphs"][0]["lock"] = inventory["graphs"][1]["lock"]
        path.write_text(json.dumps(inventory))
        with self.assertRaisesRegex(ValueError, "wrong release lockfile"):
            self.install(source=source)
        self.assertFalse((self.bundle / "Resources").exists())


if __name__ == "__main__":
    unittest.main()
