# SPDX-License-Identifier: GPL-3.0-only
"""Video cataloguing and XMP sidecar write-back."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import catalog as catalog_module
import catalog_scan
import durable_io
import server
import xmp_sidecar


class VideoClassificationTests(unittest.TestCase):
    def test_video_extensions_are_accepted_but_not_raw(self):
        for name in ("clip.mov", "clip.MP4", "clip.m4v", "clip.avi"):
            self.assertTrue(server.is_video(name), name)
            self.assertFalse(server.is_raw(name), name)
        self.assertFalse(server.is_video("frame.jpg"))

    def test_scanner_labels_video_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            (root / "clip.mov").write_bytes(b"not really a movie" * 20)
            Image.new("RGB", (8, 8)).save(root / "still.jpg")
            cat = catalog_module.Catalog(Path(directory) / "library.sqlite3")
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            kinds = {item["filename"]: item["kind"]
                     for item in cat.query()["items"]}
            self.assertEqual(kinds["clip.mov"], "video")
            self.assertEqual(kinds["still.jpg"], "processed")
            self.assertEqual(cat.query({"filter": {"kind": "video"}})["total"], 1)
            cat.close()

    def test_extension_lists_agree_on_video(self):
        import ingest_workflow
        self.assertTrue(server.VIDEO_EXTS <= set(server.EXTS))
        self.assertTrue(server.VIDEO_EXTS <= set(ingest_workflow.PHOTO_EXTENSIONS))
        self.assertTrue(server.VIDEO_EXTS <= catalog_scan.ALL_EXTS)

    def test_catalog_query_can_exclude_video_without_forgetting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            (root / "clip.mov").write_bytes(b"not really a movie" * 20)
            Image.new("RGB", (8, 8)).save(root / "still.jpg")
            cat = catalog_module.Catalog(Path(directory) / "library.sqlite3")
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)

            result = cat.query({"excludeKinds": ["video"]})

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["kind"], "processed")
            self.assertEqual(cat.query({"excludeKinds": None})["total"], 2)
            self.assertEqual(
                cat.query({"filter": {"kind": "video"}})["total"], 1)
            cat.close()


class VideoRangeTests(unittest.TestCase):
    """Seeking is what makes a catalogued clip usable, and that needs Range."""

    class FakeWFile:
        def __init__(self):
            self.chunks = []

        def write(self, data):
            self.chunks.append(data)

    def _handler(self, path: Path, range_header: str | None):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Range": range_header} if range_header else {}
        handler.wfile = self.FakeWFile()
        handler.sent = {}
        handler.status = None
        handler.send_response = lambda code: handler.sent.__setitem__("code", code)
        handler.send_header = lambda key, value: handler.sent.setdefault(
            "headers", {}).__setitem__(key, value)
        handler.end_headers = lambda: None
        return handler

    def _run(self, payload: bytes, range_header: str | None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "clip.mp4"
            clip.write_bytes(payload)
            handler = self._handler(clip, range_header)
            with mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(server, "CATALOG", None):
                handler._send_video("clip.mp4")
            return handler, b"".join(handler.wfile.chunks)

    def test_full_request_sends_everything(self):
        payload = bytes(range(256)) * 8
        handler, body = self._run(payload, None)
        self.assertEqual(handler.sent["code"], 200)
        self.assertEqual(body, payload)
        self.assertEqual(handler.sent["headers"]["Accept-Ranges"], "bytes")
        self.assertEqual(handler.sent["headers"]["Content-Type"], "video/mp4")

    def test_range_request_sends_the_slice(self):
        payload = bytes(range(256)) * 8
        handler, body = self._run(payload, "bytes=100-199")
        self.assertEqual(handler.sent["code"], 206)
        self.assertEqual(body, payload[100:200])
        self.assertEqual(handler.sent["headers"]["Content-Range"],
                         f"bytes 100-199/{len(payload)}")
        self.assertEqual(handler.sent["headers"]["Content-Length"], "100")

    def test_open_ended_range_runs_to_the_end(self):
        payload = b"x" * 500
        handler, body = self._run(payload, "bytes=400-")
        self.assertEqual(handler.sent["code"], 206)
        self.assertEqual(len(body), 100)

    def test_suffix_range_returns_the_tail(self):
        payload = bytes(range(256))
        handler, body = self._run(payload, "bytes=-10")
        self.assertEqual(body, payload[-10:])

    def test_nonsense_range_falls_back_to_the_whole_file(self):
        payload = b"abcdef"
        handler, body = self._run(payload, "bytes=99-1")
        self.assertEqual(body, payload)

    def test_a_still_is_refused_by_the_video_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (4, 4)).save(root / "still.jpg")
            handler = self._handler(root / "still.jpg", None)
            with mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(server, "CATALOG", None):
                with self.assertRaises(ValueError):
                    handler._send_video("still.jpg")


class SidecarWritingTests(unittest.TestCase):
    RECORD = {
        "rating": 4, "label": "blue", "status": "approved",
        "keywords": ["Places > Italy > Rome", "Travel"],
        "grade": {"exposure": 0.5, "contrast": 0.25, "saturation": -0.1},
        "crop": {"x": 0.1, "y": 0.2, "w": 0.7, "h": 0.6},
        "iptc": {"creator": "Nicholas", "copyright": "(c) 2026 Nicholas",
                 "title": "Roman light", "caption": "Late afternoon",
                 "city": "Rome"},
    }

    def test_document_round_trips_through_the_parser(self):
        document = xmp_sidecar.build_sidecar(self.RECORD)
        parsed = xmp_sidecar.parse(document)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["rating"], 4)
        self.assertEqual(parsed["label"], "blue")
        self.assertEqual(parsed["creator"], "Nicholas")
        self.assertEqual(parsed["title"], "Roman light")
        self.assertEqual(parsed["city"], "Rome")
        self.assertEqual(parsed["keywordPaths"], ["Places|Italy|Rome", "Travel"])
        self.assertAlmostEqual(parsed["crop"]["x"], 0.1, places=5)
        self.assertAlmostEqual(parsed["crop"]["w"], 0.7, places=5)

    def test_grade_values_survive_the_round_trip(self):
        parsed = xmp_sidecar.parse(xmp_sidecar.build_sidecar(self.RECORD))
        patch = xmp_sidecar.as_edit_patch(parsed)
        self.assertAlmostEqual(patch["grade"]["exposure"], 0.5, places=3)
        self.assertAlmostEqual(patch["grade"]["contrast"], 0.25, places=3)
        self.assertAlmostEqual(patch["grade"]["saturation"], -0.1, places=3)

    def test_native_payload_is_marked_authoritative(self):
        document = xmp_sidecar.build_sidecar(self.RECORD)
        self.assertIn("lighttable:edit=", document)
        self.assertIn("approximate", document)

    def test_defaults_produce_a_minimal_document(self):
        document = xmp_sidecar.build_sidecar({"rating": 0, "label": "none"})
        self.assertIn('xmp:Rating="0"', document)
        self.assertIn('xmp:Label=""', document)
        self.assertNotIn("crs:HasCrop", document)
        parsed = xmp_sidecar.parse(document)
        self.assertEqual(parsed["rating"], 0)
        self.assertIn("rating", parsed["metadataPresent"])
        self.assertIn("label", parsed["metadataPresent"])

    def test_write_sidecar_creates_the_file_beside_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frame.RAF"
            source.write_bytes(b"raw bytes")
            self.assertTrue(xmp_sidecar.write_sidecar(source, self.RECORD))
            target = source.with_suffix(".xmp")
            self.assertTrue(target.is_file())
            self.assertEqual(xmp_sidecar.parse(target.read_text())["rating"], 4)

    def test_write_sidecar_reuses_the_existing_naming_form(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frame.CR2"
            source.write_bytes(b"raw")
            existing = Path(str(source) + ".xmp")
            original = xmp_sidecar.build_sidecar({"rating": 2})
            existing.write_text(original)
            self.assertTrue(xmp_sidecar.write_sidecar(source, self.RECORD))
            self.assertFalse(source.with_suffix(".xmp").exists())
            self.assertIn("xmp:Rating", existing.read_text())
            self.assertEqual(
                durable_io.backup_path(existing).read_text(), original,
                "the pre-LightTable sidecar must remain recoverable",
            )

    def test_repeated_writes_do_not_replace_the_original_sidecar_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frame.RAF"
            source.write_bytes(b"raw")
            target = source.with_suffix(".xmp")
            original = xmp_sidecar.build_sidecar({"rating": 2})
            target.write_text(original)

            self.assertTrue(xmp_sidecar.write_sidecar(source, self.RECORD))
            newer = dict(self.RECORD, rating=1)
            self.assertTrue(xmp_sidecar.write_sidecar(source, newer))

            self.assertEqual(durable_io.backup_path(target).read_text(),
                             original)
            self.assertEqual(xmp_sidecar.parse(target.read_text())["rating"], 1)

    def test_write_sidecar_on_a_read_only_folder_reports_false(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "locked"
            folder.mkdir()
            source = folder / "frame.jpg"
            source.write_bytes(b"jpeg")
            os.chmod(folder, 0o500)
            try:
                self.assertFalse(xmp_sidecar.write_sidecar(source, self.RECORD))
            finally:
                os.chmod(folder, 0o700)

    def test_special_characters_are_escaped(self):
        record = dict(self.RECORD,
                      iptc={"creator": 'Nick & "Co" <tags>'})
        parsed = xmp_sidecar.parse(xmp_sidecar.build_sidecar(record))
        self.assertEqual(parsed["creator"], 'Nick & "Co" <tags>')


class SidecarServerTests(unittest.TestCase):
    def test_server_writes_sidecars_for_pending_photos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            Image.new("RGB", (8, 8)).save(root / "a.jpg")
            cat = catalog_module.Catalog(Path(directory) / "library.sqlite3")
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            with mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(server, "CATALOG", cat), \
                    mock.patch.object(server, "PRIMARY_SOURCE_ID", source), \
                    mock.patch.object(server, "CATALOG_MIRROR", False):
                server.save_image_state("a.jpg", {"rating": 5,
                                                  "label": "green"})
                written = server.write_pending_sidecars()
            self.assertEqual(written, 1)
            parsed = xmp_sidecar.parse((root / "a.xmp").read_text())
            self.assertEqual(parsed["rating"], 5)
            self.assertEqual(parsed["label"], "green")
            cat.close()


if __name__ == "__main__":
    unittest.main()
