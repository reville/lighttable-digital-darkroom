"""Trash plans must protect retained originals and their shared metadata."""
import shutil
import subprocess
from pathlib import Path

import catalog as catalog_module
import server
from test_server_catalog import CatalogServerTestCase


class TrashSafetyTests(CatalogServerTestCase):
    def test_rejected_virtual_copy_cannot_trash_approved_original(self):
        original = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.save_state(original, {"status": "approved"})
        variant = self.catalog.add_virtual_copy(original, "alternate", "Alternate")
        self.catalog.save_state(variant, {"status": "skipped"})
        name = catalog_module.qualified_name(self.source, "a.jpg", "alternate")
        result = server.trash_photos({"names": [name]})
        self.assertEqual(result["paths"], [])
        self.assertEqual(result["photoCount"], 0)
        self.assertEqual(self.catalog.state_for(original)["status"], "approved")
        self.assertTrue((self.root / "a.jpg").is_file())
        self.assertIsNotNone(self.catalog.image_id_for(self.source, "a.jpg", "alternate"))

    def test_shared_uppercase_xmp_stays_with_unselected_companion(self):
        (self.root / "a.CR3").write_bytes(b"synthetic RAW")
        (self.root / "a.XMP").write_text("shared metadata")
        (self.root / "a.CR3.XMP").write_text("RAW metadata")
        (self.root / "a.CR3.lighttable.json").write_text("{}")
        (self.root / "a.dop").write_text("unrecognized metadata")
        result = server.trash_photos({"names": [self.qualified("a.CR3")]})
        self.assertEqual({Path(path).name for path in result["paths"]},
                         {"a.CR3", "a.CR3.XMP", "a.CR3.lighttable.json"})
        self.assertEqual(result["photoCount"], 1)
        self.assertTrue((self.root / "a.XMP").is_file())

    def test_selecting_both_companions_carries_shared_xmp_once(self):
        (self.root / "a.CR3").write_bytes(b"synthetic RAW")
        (self.root / "a.XMP").write_text("shared metadata")
        result = server.trash_photos({"names": [self.qualified("a.CR3"),
                                               self.qualified("a.jpg"),
                                               self.qualified("a.jpg")]})
        self.assertEqual({Path(path).name for path in result["paths"]},
                         {"a.CR3", "a.jpg", "a.XMP"})
        self.assertEqual((result["photoCount"], result["count"]), (2, 3))

    def test_mixed_virtual_and_original_names_do_not_duplicate_paths(self):
        original = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.add_virtual_copy(original, "alternate", "Alternate")
        result = server.trash_photos({"names": [self.qualified("a.jpg"),
            catalog_module.qualified_name(self.source, "a.jpg", "alternate"),
            "a.jpg", "missing.jpg"]})
        self.assertEqual(result["paths"], [str((self.root / "a.jpg").resolve())])
        self.assertEqual(result["photoCount"], 1)

    def test_ui_waits_for_saves_and_never_dispatches_failed_plans(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name(
            "trash-safety.test.mjs"))], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
