# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import tempfile
from unittest import mock
import zipfile

import catalog as catalog_module
import server
from test_server_catalog import CatalogServerTestCase


class BackupManifestTests(CatalogServerTestCase):
    def test_backup_reopens_without_caches_and_retains_masks_history_and_collections(self):
        image_id = server.catalog_image_id(self.qualified("a.jpg"))
        masks = [{"id": "accepted-mask", "type": "brush", "bitmap": {"data": "accepted"}}]
        self.catalog.save_state(image_id, {"masks": masks, "rating": 4})
        self.catalog.add_history(image_id, "Accepted result", {"masks": masks})
        with tempfile.TemporaryDirectory() as destination:
            archive = self.catalog.backup(destination)
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertIn("Original photo files", manifest["scope"]["excluded"])
                self.assertEqual(len(manifest["catalog"]["sha256"]), 64)
            verification = catalog_module.verify_backup(archive)
            self.assertEqual(verification["summary"]["history"], 1)
            restored_path = Path(destination) / "fresh-machine" / "library.sqlite3"
            restored_path.parent.mkdir()
            catalog_module.restore_backup(restored_path, archive)
            restored = catalog_module.Catalog(restored_path)
            try:
                self.assertEqual(restored.state_for(image_id)["masks"], masks)
                self.assertEqual(restored.state_for(image_id)["rating"], 4)
                self.assertEqual(restored.history_state(restored.history_for(image_id)[0]["id"]), {"masks": masks})
            finally:
                restored.close()

    def test_corrupt_manifest_and_failed_publication_do_not_replace_a_verified_backup(self):
        with tempfile.TemporaryDirectory() as destination:
            good = self.catalog.backup(destination)
            with self.catalog.connection as conn:
                previous = conn.execute("SELECT value FROM meta WHERE key='lastVerifiedBackup'").fetchone()[0]
            with mock.patch.object(catalog_module.durable_io, "publish_file_no_replace", side_effect=OSError("NAS disconnected")):
                with self.assertRaisesRegex(OSError, "NAS disconnected"):
                    self.catalog.backup(destination)
            self.assertTrue(good.is_file())
            self.assertEqual(self.catalog.connection.execute("SELECT value FROM meta WHERE key='lastVerifiedBackup'").fetchone()[0], previous)
            bad = Path(destination) / "tampered.zip"
            with zipfile.ZipFile(good) as original, zipfile.ZipFile(bad, "w") as altered:
                altered.writestr("library.sqlite3", original.read("library.sqlite3"))
                manifest = json.loads(original.read("manifest.json"))
                manifest["catalog"]["sha256"] = "0" * 64
                altered.writestr("manifest.json", json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "verification"):
                catalog_module.verify_backup(bad)
