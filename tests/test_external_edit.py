from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import catalog
import catalog_scan
import server


class ExternalEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "photos"
        self.source.mkdir()
        self.original = self.source / "portrait.jpg"
        Image.new("RGB", (24, 18), "gray").save(self.original)
        self.cat = catalog.Catalog(self.root / "catalog.sqlite3")
        self.addCleanup(self.cat.close)
        self.source_id = self.cat.add_source(self.source)
        self.original_id = catalog_scan.register_file(self.cat, self.original)
        self.name = catalog.qualified_name(self.source_id, self.original.name)
        self.cat.save_state(self.original_id, {
            "status": "approved", "rating": 4, "label": "blue",
            "keywords": ["People > Portrait"],
        })
        self.cat.save_iptc(self.original_id, {"caption": "Studio portrait"})

    def _patch_server(self):
        def render(_name, destination, _job):
            Image.new("RGB", (24, 18), "silver").save(destination, "TIFF")

        return (
            mock.patch.object(server, "CATALOG", self.cat),
            mock.patch.object(server, "PRIMARY_SOURCE_ID", self.source_id),
            mock.patch.object(server, "FOLDER", self.source),
            mock.patch.object(server, "CACHE", self.root / "cache"),
            mock.patch.object(server, "_render_external_job", side_effect=render),
        )

    def test_adjusted_round_trip_registers_stacks_and_copies_state(self):
        patches = self._patch_server()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        server.EXTERNAL_EDIT.update(running=True, total=1, done=0,
                                    paths=[], names=[], errors=[])
        server._run_external_edits([self.name], "prophoto")

        self.assertEqual(server.EXTERNAL_EDIT["errors"], [])
        derivative = Path(server.EXTERNAL_EDIT["paths"][0])
        self.assertEqual(derivative.name, "portrait-Edit.tif")
        derivative_id = catalog_scan.register_file(self.cat, derivative)
        state = self.cat.state_for(derivative_id)
        self.assertEqual((state["status"], state["rating"], state["label"]),
                         ("approved", 4, "blue"))
        self.assertEqual(state["keywords"], ["People > Portrait"])
        self.assertEqual(self.cat.iptc_for(derivative_id)["caption"],
                         "Studio portrait")
        self.assertEqual(self.cat.stack_id_for(self.original_id),
                         self.cat.stack_id_for(derivative_id))

    def test_stack_with_original_can_be_disabled(self):
        patches = self._patch_server()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        server.EXTERNAL_EDIT.update(running=True, total=1, done=0,
                                    paths=[], names=[], errors=[])
        server._run_external_edits(
            [self.name], "prophoto", 8, stack_with_original=False)
        derivative = Path(server.EXTERNAL_EDIT["paths"][0])
        derivative_id = catalog_scan.register_file(self.cat, derivative)
        self.assertIsNone(self.cat.stack_id_for(self.original_id))
        self.assertIsNone(self.cat.stack_id_for(derivative_id))

    def test_external_job_carries_requested_tiff_depth(self):
        patches = self._patch_server()[:-1]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        job = server._external_job(self.name, "display_p3", 8)
        self.assertEqual(job["bitDepth"], 8)
        self.assertEqual(job["outputSpace"], "display_p3")

    def test_in_place_rewrite_keeps_state_and_stack(self):
        patches = self._patch_server()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        server.EXTERNAL_EDIT.update(running=True, total=1, done=0,
                                    paths=[], names=[], errors=[])
        server._run_external_edits([self.name], "prophoto")
        derivative = Path(server.EXTERNAL_EDIT["paths"][0])
        before_id = catalog_scan.register_file(self.cat, derivative)
        before_hash = self.cat.image_row(before_id)["header_hash"]
        stack_id = self.cat.stack_id_for(before_id)
        Image.new("RGB", (25, 18), "black").save(derivative, "TIFF")
        after_id = catalog_scan.register_file(self.cat, derivative)
        self.assertEqual(after_id, before_id)
        self.assertEqual(self.cat.stack_id_for(after_id), stack_id)
        self.assertNotEqual(self.cat.image_row(after_id)["header_hash"],
                            before_hash)
        self.assertEqual(self.cat.state_for(after_id)["rating"], 4)

    def test_raw_original_is_refused(self):
        result = server.start_external_edit({
            "names": ["capture.dng"], "mode": "original"})
        self.assertFalse(result["ok"])
        self.assertIn("RAW", result["error"])

    def test_unwritable_source_folder_reports_its_path(self):
        patches = self._patch_server()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.source.chmod(0o555)
        self.addCleanup(self.source.chmod, 0o755)
        server.EXTERNAL_EDIT.update(running=True, total=1, done=0,
                                    paths=[], names=[], errors=[])
        server._run_external_edits([self.name], "prophoto")
        self.assertIn(str(self.source), server.EXTERNAL_EDIT["errors"][0])


if __name__ == "__main__":
    unittest.main()
