"""Compare must use accurate pixels at the edited preview's resolution."""
import http.client
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import server


ROOT = Path(__file__).resolve().parents[1]


class CompareOriginalTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_browser_and_native_original_url_retains_requested_detail(self):
        javascript = (ROOT / "web/app.js").read_text()
        start = javascript.index("function originalPreviewURL(")
        function = javascript[start:javascript.index("\nlet browserOriginal", start)]
        script = """
const S = {params: {rotate: 90}};
const cur = () => ({name: '3:photo & detail.ARW', fileKey: 'changed-source'});
const INTERACTIVE_PREVIEW_WIDTH = 1100;
const requestedPreviewWidth = () => 6000;
""" + function + """
console.log(JSON.stringify([800, 3000, 6000, undefined].map(width =>
  Object.fromEntries(new URL(originalPreviewURL(width), 'http://local').searchParams))));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True,
                                text=True, check=True, timeout=10)
        urls = json.loads(result.stdout)
        self.assertEqual([int(url["w"]) for url in urls], [800, 3000, 6000, 6000])
        for url in urls:
            self.assertEqual(url["quality"], "full")
            self.assertEqual(url["name"], "3:photo & detail.ARW")
            self.assertEqual(url["rot"], "90")
            self.assertEqual(url["key"], "changed-source")

    def test_raw_original_matches_unedited_develop_pixels_and_rotation(self):
        # A detailed decoded RAW and a deliberately different camera thumbnail
        # catch both lost resolution and accidentally using the camera look.
        y, x = np.indices((1280, 1920))
        pixels = np.stack([(x % 16) / 16, (y % 16) / 16,
                           ((x + y) % 16) / 16], axis=-1).astype(np.float32)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(server, "CACHE", Path(directory)),
            mock.patch.object(server, "file_key", return_value="source"),
            mock.patch.object(server, "guard_local_photo"),
            mock.patch.object(server, "src_path", return_value=Path("frame.dng")),
            mock.patch.object(server.color_pipeline, "decode_raw", return_value=pixels),
            mock.patch.object(server.color_pipeline, "linear_prophoto_to_display_srgb",
                              side_effect=lambda image, params: image),
            mock.patch.object(server, "raw_display", side_effect=AssertionError("camera thumbnail")),
        ):
            for rotation, dimensions in ((0, (1920, 1280)), (90, (1280, 1920))):
                with self.subTest(rotation=rotation):
                    expected = server.build_neutral_preview(
                        "frame.dng", 1920, rotation,
                        server.fp.clean_params({"profile_enabled": False})).read_bytes()
                    actual = server.orig_jpeg("frame.dng", 1920, rotation, quality="full")
                    self.assertEqual(actual, expected)
                    self.assertEqual(Image.open(io.BytesIO(actual)).size, dimensions)

    def test_quick_raw_opening_preview_remains_a_draft(self):
        with mock.patch.object(server, "guard_local_photo"), \
                mock.patch.object(server, "_orig_jpeg", return_value=b"draft") as draft, \
                mock.patch.object(server, "build_neutral_preview") as accurate:
            self.assertEqual(server.orig_jpeg("frame.dng", 1100), b"draft")
            draft.assert_called_once_with("frame.dng", 1100, 0)
            accurate.assert_not_called()

    def test_processed_original_reuses_exact_unedited_pixels(self):
        with mock.patch.object(server, "guard_local_photo"), \
                mock.patch.object(server, "_orig_jpeg", return_value=b"source") as original, \
                mock.patch.object(server, "build_neutral_preview") as raw:
            self.assertEqual(server.orig_jpeg("frame.jpg", 4000, 90, quality="full"), b"source")
            original.assert_called_once_with("frame.jpg", 4000, 90)
            raw.assert_not_called()

    def test_original_http_route_passes_quality_without_changing_draft_requests(self):
        class Handler(server.Handler):
            def log_message(self, *_args):
                pass

        httpd = server.LightTableServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
        try:
            with mock.patch.object(server, "orig_jpeg", return_value=b"pixels") as original:
                for suffix, quality in (("&quality=full", "full"), ("", "draft")):
                    connection.request("GET", "/api/orig?name=frame.dng&w=3000&rot=90" + suffix)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
                    self.assertEqual(response.read(), b"pixels")
                    original.assert_called_with("frame.dng", 3000, 90.0, quality=quality)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
