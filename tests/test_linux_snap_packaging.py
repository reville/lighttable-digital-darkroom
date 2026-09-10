from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_linux_arch_packaging import fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("snap_package", ROOT / "scripts/linux/make-snap-package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
REVISION = "be537f2f3e2e431ae6b42af716c2a8b365f57bab"


class SnapPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "LightTable-0.5.0-linux-x86_64.tar.gz"
        self.output = self.root / "snap project"
        fixture(self.archive, manifest={"version": "0.5.0", "source_revision": REVISION,
                                        "source_dirty": False})

    def generate(self, **changes):
        return package.generate(self.archive, self.output,
                                **({"version": "0.5.0", "source_revision": REVISION} | changes))

    def test_full_bundle_preserves_native_layout_and_removes_portable_installer(self):
        manifest = self.generate().read_text()
        self.assertIn("version: '0.5.0'", manifest)
        self.assertIn("grade: devel", manifest)
        self.assertIn("confinement: strict", manifest)
        self.assertNotIn("classic", manifest)
        self.assertIn("private: true", manifest)
        self.assertIn("- libopenblas0-pthread", manifest)
        bundle = self.output / "payload/LightTable"
        self.assertTrue((bundle / "Python/bin/python3").is_file())
        self.assertTrue((bundle / "Resources/LightTable/engine/lighttable-engine").is_file())
        self.assertFalse((bundle / "install.sh").exists())
        self.assertFalse((bundle / "uninstall.sh").exists())
        notices = bundle / "Resources/LightTable/licenses/native-rust"
        provenance = json.loads((notices / "provenance.json").read_text())
        self.assertEqual(provenance["source_revision"], REVISION)
        self.assertTrue(any(row["path"].startswith("crates/rfd-0.17.2/") for row in provenance["files"]))
        import hashlib
        for row in provenance["files"]:
            self.assertEqual(hashlib.sha256((notices / row["path"]).read_bytes()).hexdigest(), row["sha256"])
        self.assertTrue((bundle / "share/applications/app.lighttable.LightTable.desktop").is_file())
        self.assertFalse(json.loads((self.output / "candidate.json").read_text())["store_published"])

    def test_wrong_source_or_checksum_leaves_no_partial_project(self):
        with self.assertRaisesRegex(ValueError, "source revision"):
            self.generate(source_revision="b" * 40)
        self.assertFalse(self.output.exists())
        self.archive.with_name(self.archive.name + ".sha256").write_text("0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "existing"
        sentinel.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            self.generate()
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_corrupted_native_notice_rejects_package_without_partial_output(self):
        from unittest.mock import patch
        original = package.NOTICES.regular_bytes
        def corrupted(path):
            data = original(path)
            return data + b"corrupted" if path.parent.name == "objects" else data
        with patch.object(package.NOTICES, "regular_bytes", side_effect=corrupted):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.generate()
        self.assertFalse(self.output.exists())

    def test_environment_keeps_catalog_location_across_snap_revisions_and_quotes_arguments(self):
        self.generate()
        launcher = self.output / "payload/bin/lighttable-snap-environment"
        common = self.root / "user data"
        for revision in ("1", "2"):
            environment = os.environ | {"SNAP": str(self.root / revision), "SNAP_USER_COMMON": str(common),
                                        "PYTHONHOME": "/invalid", "PYTHONPATH": "/invalid",
                                        "LD_LIBRARY_PATH": "/gnome/gpu-provider"}
            result = subprocess.run(["sh", str(launcher), "sh", "-c",
                                     'printf "%s\\n" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "${PYTHONHOME-unset}" "$1" "$LD_LIBRARY_PATH"',
                                     "check", "photo with spaces.raw"], env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(common / "data"), str(common / "state"),
                                                         "unset", "photo with spaces.raw",
                                                         f"{self.root / revision}/usr/lib/x86_64-linux-gnu/openblas-pthread:"
                                                         f"{self.root / revision}/usr/lib/x86_64-linux-gnu:"
                                                         f"{self.root / revision}/usr/lib:/gnome/gpu-provider"])


if __name__ == "__main__":
    unittest.main()
