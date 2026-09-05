"""Tests for Match Total Exposure calculation and endpoint."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import catalog as catalog_module
import catalog_scan
import server


def make_photo(path: Path, colour=(120, 90, 60)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), colour).save(path, quality=95)
    return path


class MatchExposureMathTests(unittest.TestCase):
    def test_parse_camera_ev_standard(self):
        # f/2.8, 1/500s, ISO 400
        # N^2 / t = 7.84 / 0.002 = 3920. log2(3920) ~= 11.9366
        # ISO 400 / 100 = 4. log2(4) = 2.0.
        # EV = 11.9366 - 2.0 = 9.9366
        meta = {"FNumber": "2.8", "ExposureTime": "1/500", "ISO": "400"}
        ev = server.parse_camera_ev(meta)
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev, 9.9366, places=3)

    def test_parse_camera_ev_fractional_and_decimal_times(self):
        meta1 = {"FNumber": "f/4.0", "ExposureTime": "1/250", "ISO": "100"}
        meta2 = {"FNumber": "4.0", "ExposureTime": "0.004", "ISO": "100"}
        ev1 = server.parse_camera_ev(meta1)
        ev2 = server.parse_camera_ev(meta2)
        self.assertIsNotNone(ev1)
        self.assertAlmostEqual(ev1, ev2, places=4)
        # N^2 / t = 16 / 0.004 = 4000. log2(4000) ~= 11.9658
        self.assertAlmostEqual(ev1, 11.9658, places=3)

    def test_parse_camera_ev_handles_missing_or_corrupt(self):
        self.assertIsNone(server.parse_camera_ev({}))
        self.assertIsNone(server.parse_camera_ev({"FNumber": "bad"}))
        self.assertIsNone(server.parse_camera_ev({"FNumber": "0", "ExposureTime": "0", "ISO": "0"}))


class MatchExposureEndpointTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "photos"
        self.root.mkdir()
        make_photo(self.root / "ref.jpg", (200, 200, 200))
        make_photo(self.root / "t1.jpg", (100, 100, 100))
        make_photo(self.root / "t2.jpg", (50, 50, 50))
        self.catalog = catalog_module.Catalog(Path(self._dir.name) / "library.sqlite3")
        self.source = self.catalog.add_source(self.root)
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        self._patches = [
            mock.patch.object(server, "FOLDER", self.root),
            mock.patch.object(server, "CATALOG", self.catalog),
            mock.patch.object(server, "PRIMARY_SOURCE_ID", self.source),
            mock.patch.object(server, "CATALOG_MIRROR", False),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self.catalog.close()
        self._dir.cleanup()

    def qualified(self, name: str) -> str:
        return catalog_module.qualified_name(self.source, name)

    def test_match_exposure_with_camera_ev(self):
        ref = self.qualified("ref.jpg")
        t1 = self.qualified("t1.jpg")

        # Ref: f/2.8, 1/1000s, ISO 100 -> EV = log2(7.84 * 1000) - 0 = 12.9366 (darker capture)
        # T1:  f/2.8, 1/500s, ISO 100  -> EV = log2(7.84 * 500) - 0 = 11.9366 (1 stop brighter capture)
        def fake_exif(name):
            if "ref" in name:
                return {"FNumber": "2.8", "ExposureTime": "1/1000", "ISO": "100"}
            return {"FNumber": "2.8", "ExposureTime": "1/500", "ISO": "100"}

        with mock.patch.object(server, "exif_for", side_effect=fake_exif):
            # Target is 1 stop brighter in capture; to match reference, it should be lowered by 1 stop
            handler = server.Handler.__new__(server.Handler)
            handler._body = lambda: {"reference": ref, "targets": [t1]}
            responses = []
            handler._json = lambda data, code=200: responses.append((data, code))

            import urllib.parse
            u = urllib.parse.urlparse("/api/match-exposure")
            server.Handler.do_POST.__code__

            # Execute the endpoint logic directly or via Handler
            # Let's call the endpoint through the standard handler invocation
            with mock.patch.object(handler, "_body", return_value={"reference": ref, "targets": [t1]}):
                with mock.patch.object(handler, "_json") as mock_json:
                    handler.path = "/api/match-exposure"
                    handler.headers = {"content-type": "application/json"}
                    handler.do_POST()
                    mock_json.assert_called_once()
                    res, status = mock_json.call_args[0][0], (mock_json.call_args[0][1] if len(mock_json.call_args[0]) > 1 else 200)
                    self.assertEqual(status, 200)
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["count"], 1)
                    # delta should be approx -1.0
                    self.assertAlmostEqual(res["deltas"][t1], -1.0, places=2)

            # Check that T1 state was updated
            t1_state = server.catalog_entry_for(t1)
            adj = t1_state["params"]["exposure_ev"] if t1_state["params"].get("film_profile_on", True) else t1_state["grade"]["exposure"]
            self.assertAlmostEqual(adj, -1.0, places=2)


if __name__ == "__main__":
    unittest.main()
