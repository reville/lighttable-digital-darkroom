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
from PIL import Image, ImageCms

import server


ROOT = Path(__file__).resolve().parents[1]


class CompareOriginalTests(unittest.TestCase):
    def test_processed_preview_preserves_srgb_and_invalidates_old_pixels(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for old_cache in (False, True):
            with self.subTest(old_cache=old_cache), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                source = Image.new("RGB", (64, 64))
                colors = [(200, 40, 20), (30, 180, 60), (40, 80, 220), (128, 128, 128)]
                for index, color in enumerate(colors):
                    source.paste(color, (index * 16, 0, (index + 1) * 16, 64))
                source.save(folder / "photo.jpg", icc_profile=profile, quality=100, subsampling=0)
                decoded = Image.open(folder / "photo.jpg")
                cache = folder / "cache"
                (cache / "orig").mkdir(parents=True)
                if old_cache:
                    Image.new("RGB", (64, 64), "black").save(cache / "orig/source_64.jpg")
                with mock.patch.object(server, "FOLDER", folder), \
                        mock.patch.object(server, "CACHE", cache), \
                        mock.patch.object(server, "file_key", return_value="source"), \
                        mock.patch.object(server, "guard_local_photo"):
                    image = Image.open(io.BytesIO(server.orig_jpeg("photo.jpg", 64)))
                for x in (8, 24, 40, 56):
                    np.testing.assert_allclose(image.getpixel((x, 32)),
                                               decoded.getpixel((x, 32)), atol=1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_raw_opens_accurately_and_cache_hit_does_not_replace_pixels(self):
        accurate = mock.Mock()
        accurate.exists.side_effect = [False, True]
        with mock.patch.object(server, "neutral_preview_path", return_value=accurate), \
                mock.patch.object(server, "orig_jpeg", side_effect=AssertionError("camera flash")), \
                mock.patch.object(server, "build_neutral_preview", return_value=accurate), \
                mock.patch.object(server, "guard_local_photo"), \
                mock.patch.object(server, "file_key", return_value="source"):
            responses = [server.render_preview("photo.dng", {"profile_enabled": False}, 1100)
                         for _ in range(2)]
        javascript = (ROOT / "web/app.js").read_text()
        source = javascript[javascript.index("function rememberPresentedRender("):
                            javascript.index("function setWebGLBaseImage(")]
        script = """
const assert = require('node:assert/strict'), vm = require('node:vm');
const responses = RESPONSE;
(async () => { for (const native of [true, false]) {
  const uploads = [], S = {seq:1, renderState:'pending'};
  const context = {S, performance, nativePreviewActive:() => native, drawGrade:() => {},
    setNativeBaseImage:async render => {uploads.push(render.img); return {};},
    setWebGLBaseImage:async url => {uploads.push(url); return {};}};
  vm.runInNewContext(SOURCE + '\\nglobalThis.load=setBaseImage;', context);
  await context.load(responses[0], 1);
  S.renderState='ready'; S.seq=2;
  await context.load(responses[1], 2);
  assert.equal(uploads.length, 1, 'cached neutral preview must not upload again');
  assert.match(uploads[0], /api\\/neutral/);
}})().catch(e => {console.error(e); process.exitCode=1;});
""".replace("RESPONSE", json.dumps(responses)).replace("SOURCE", json.dumps(source))
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_before_uses_accurate_raw_at_requested_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full, draft = folder / "full.jpg", folder / "draft.jpg"
            Image.new("RGB", (3000, 2000), (60, 90, 120)).save(full)
            Image.new("RGB", (1600, 1067), (30, 40, 50)).save(draft)
            with mock.patch.object(server, "CACHE", folder), \
                    mock.patch.object(server, "file_key", return_value="source"), \
                    mock.patch.object(server, "guard_local_photo"), \
                    mock.patch.object(server, "catalog_entry_for", return_value={}), \
                    mock.patch.object(server, "build_neutral_preview", return_value=full), \
                    mock.patch.object(server, "raw_display", return_value=draft):
                result = server.program_render_image({"name": "photo.dng", "w": 3000, "before": True})
            self.assertEqual(result.size, (3000, 2000))
            np.testing.assert_array_equal(result, Image.open(full))

    def test_cli_after_waits_for_accurate_raw_on_a_cold_cache(self):
        for film in (False, True):
            with self.subTest(film=film), tempfile.TemporaryDirectory() as directory:
                image = Path(directory) / "accurate.jpg"
                Image.new("RGB", (90, 60), (60, 90, 120)).save(image)
                with mock.patch.object(server, "catalog_entry_for", return_value={}), \
                        mock.patch.object(server, "render_preview", side_effect=[
                            {"refining": True}, {"refining": False}]) as render, \
                        mock.patch.object(server, "build_neutral_preview") as neutral, \
                        mock.patch.object(server, "build_raw_preview") as raw, \
                        mock.patch.object(server, "_preview_source_bytes", return_value=image.read_bytes()), \
                        mock.patch.object(server, "exif_for", return_value={}):
                    server.program_render_image({"name": "photo.dng", "w": 90,
                        "state": {"params": {"profile_enabled": film}}})
                self.assertEqual(render.call_count, 2, "CLI returned the draft without finishing RAW")
                self.assertEqual(raw.call_count, int(film))
                self.assertEqual(neutral.call_count, int(not film))

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
            self.assertEqual(url["v"], str(server.ORIGINAL_PREVIEW_CACHE_VERSION))
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
