"""Behavioral contracts for cold decoding, disposable writes and engine isolation."""
import http.client
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageCms
import color_pipeline
import durable_io
import platform_image
import server


class ColdPreviewTests(unittest.TestCase):
    def test_raw_input_and_match_share_one_embedded_decode_and_invalidate_on_change(self):
        import rawpy
        buffer = io.BytesIO()
        Image.new("RGB", (4000, 3000), (90, 140, 180)).save(buffer, "JPEG")
        thumb = mock.Mock(format=rawpy.ThumbFormat.JPEG, data=buffer.getvalue())
        raw = mock.MagicMock()
        raw.__enter__.return_value.extract_thumb.return_value = thumb
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frame.nef"
            source.write_bytes(b"raw")
            with mock.patch.object(rawpy, "imread", return_value=raw) as decode:
                first = color_pipeline.raw_embedded_preview(source, 1100)
                before = color_pipeline.raw_embedded_preview(source, 1600)
                self.assertEqual(first.shape, (825, 1100, 3))
                self.assertEqual(before.shape, (1200, 1600, 3))
                self.assertEqual(decode.call_count, 1)
                self.assertLess(abs(float(first.mean()) - float(before.mean())), 1)
                source.write_bytes(b"raw changed")
                color_pipeline.raw_embedded_preview(source, 1100)
                self.assertEqual(decode.call_count, 2)

    def test_processed_draft_preserves_orientation_and_color_without_full_tiff(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.jpg"
            exif = Image.Exif(); exif[274] = 6
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            Image.new("RGB", (4000, 3000), (80, 140, 200)).save(source, exif=exif, icc_profile=profile)
            with mock.patch.object(platform_image, "convert_processed_to_tiff", side_effect=AssertionError("full decode")):
                image = platform_image.processed_preview(source, 600, app_root=server.APP, output_space="srgb")
            self.assertEqual(image.shape, (800, 600, 3))
            np.testing.assert_allclose(image[0, 0], np.array([80, 140, 200]) / 255, atol=2/255)

    def test_cache_publication_is_atomic_but_durable_writes_still_fsync(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_bytes(b"old")
            with mock.patch.object(durable_io.os, "fsync") as flush:
                durable_io.cache_write_json(path, {"ready": True})
                flush.assert_not_called()
                durable_io.atomic_write_json(path, {"state": True})
                self.assertGreaterEqual(flush.call_count, 2)
            with mock.patch.object(durable_io.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    durable_io.cache_write_json(path, {"partial": True})
            self.assertEqual(durable_io.load_json(path, {}), {"state": True})
            self.assertFalse(list(Path(directory).glob(".*.cache.*")))

    def test_incomplete_generated_images_are_discarded(self):
        import tifffile
        with tempfile.TemporaryDirectory() as directory:
            tiff = Path(directory) / "preview.tif"
            jpeg = Path(directory) / "before.jpg"
            tifffile.imwrite(tiff, np.zeros((10, 20, 3), dtype=np.uint16))
            Image.new("RGB", (20, 10)).save(jpeg)
            self.assertTrue(server.valid_tiff_cache(tiff))
            self.assertTrue(server.valid_jpeg_cache(jpeg))
            tiff.write_bytes(tiff.read_bytes()[:-20])
            jpeg.write_bytes(jpeg.read_bytes()[:-20])
            self.assertFalse(server.valid_tiff_cache(tiff))
            self.assertFalse(server.valid_jpeg_cache(jpeg))
            self.assertFalse(tiff.exists())
            self.assertFalse(jpeg.exists())

    def test_warmup_executes_native_pipeline_and_removes_scratch_files(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = server.RustEngineClient(Path("worker"))
            with mock.patch.object(server, "CACHE", Path(directory)), mock.patch.object(engine, "render") as render:
                engine.warm()
            render.assert_called_once()
            request = render.call_args.args[0]
            self.assertIn("native_output", request)
            self.assertNotIn("output", request)
            self.assertNotIn("command", request)
            self.assertFalse(Path(request["input"]).exists())

    def test_catalog_lens_identity_avoids_metadata_process_until_correction_enabled(self):
        cat = mock.Mock()
        cat.image_row.return_value = {"camera_make": "Canon", "camera_model": "EOS R", "lens": "RF 50mm"}
        with mock.patch.object(server, "catalog_handle", return_value=cat), mock.patch.object(server, "catalog_image_id", return_value=1), mock.patch.object(server, "exif_for", return_value={"FocalLength": "50 mm"}) as exif:
            self.assertEqual(server.preview_lens_metadata("1:frame.cr3")["LensModel"], "RF 50mm")
            exif.assert_not_called()
            self.assertEqual(server.preview_lens_metadata("1:frame.cr3", {"profileEnabled": True})["FocalLength"], "50 mm")


class BackgroundEngineTests(unittest.TestCase):
    def test_export_probe_hit_does_not_decode_again(self):
        engine = mock.Mock()
        engine.probe_input.return_value = True
        engine.render.return_value = {"width": 10, "height": 8}
        with mock.patch.object(server, "BACKGROUND_ENGINE", engine), mock.patch.object(server, "is_raw", return_value=True), mock.patch.object(server, "file_key", return_value="source"), mock.patch.object(server, "raw_shared_input") as decode:
            result = server._resident_render_full("frame.dng", {}, {})
        decode.assert_not_called()
        self.assertEqual(result["input_transport"], "resident-cache")
        self.assertIn("input_cache_key", engine.render.call_args.args[0])

    def test_background_request_runs_while_interactive_admission_is_held(self):
        engine = server.RustEngineClient(Path("worker"), lock=server.BACKGROUND_RENDER_LOCK)
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        done = threading.Event()
        errors = []
        def render():
            try:
                engine.render({})
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        with mock.patch.object(engine, "_start", return_value=process), mock.patch.object(engine, "_readline", return_value='{"ok":true}'):
            with server.RENDER_LOCK:
                worker = threading.Thread(target=render)
                worker.start()
                self.assertTrue(done.wait(2), "export still shares interactive lock")
            worker.join(2)
        self.assertEqual(errors, [])

    def test_keep_alive_reuses_the_same_http_connection(self):
        class Handler(server.Handler):
            def do_GET(self):
                self._json({"ok": True})
        httpd = server.LightTableServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.version, 11)
            response.read()
            socket = connection.sock
            connection.request("GET", "/")
            self.assertEqual(connection.getresponse().read(), b'{"ok": true}')
            self.assertIs(connection.sock, socket)
        finally:
            connection.close()
            httpd.shutdown(); httpd.server_close(); worker.join(2)


class DecodedViewportTests(unittest.TestCase):
    def test_edge_clipping_uses_rotated_decoded_pixels_without_rescaling(self):
        for quarters in range(4):
            with self.subTest(quarters=quarters):
                width, height = ((100, 60) if quarters % 2 == 0 else (60, 100))
                requested = {"x": width - 20, "y": height - 10, "width": 40, "height": 30}
                actual = server.clamp_decoded_viewport(requested, 100, 60, quarters)
                self.assertEqual(actual, {"x": width - 20, "y": height - 10,
                                          "width": 20, "height": 10})
                self.assertEqual(requested["width"], 40, "cache identity input was mutated")
                self.assertEqual(server.clamp_decoded_viewport(actual, 100, 60, quarters), actual)
        self.assertEqual(server.clamp_decoded_viewport(
            {"x": 1000, "y": 1000, "width": 40, "height": 30}, 100, 60, 0),
            {"x": 99, "y": 59, "width": 1, "height": 1})

    def test_resident_probe_supplies_dimensions_before_edge_render(self):
        engine = mock.Mock()
        engine.render.side_effect = [
            {"input_cache_hit": True, "width": 10, "height": 6},
            {"width": 2, "height": 2, "full_width": 10, "full_height": 6},
        ]
        requested = {"x": 8, "y": 4, "width": 8, "height": 6}
        with mock.patch.object(server, "preview_engine", return_value=engine), \
                mock.patch.object(server, "file_key", return_value="source"), \
                mock.patch.object(server, "raw_shared_input") as decode:
            result = server.render_viewport_rust("frame.dng", {}, None, Path("unused.rgba"), requested)
        decode.assert_not_called()
        self.assertEqual(engine.render.call_args_list[0].args[0]["command"], "probe_input")
        request = engine.render.call_args_list[1].args[0]
        self.assertEqual(request["viewport"], {"x": 8, "y": 4, "width": 2, "height": 2})
        self.assertEqual(result["viewport"], request["viewport"])
        self.assertNotIn("input", request)
        self.assertEqual(requested["width"], 8)

    def test_first_raw_decode_exposes_actual_dimensions_without_a_second_decode(self):
        from contextlib import contextmanager
        pixels = np.zeros((6, 10, 3), dtype=np.uint16)
        @contextmanager
        def shared_array(rgb, key):
            self.assertEqual(rgb.shape, (6, 10, 3))
            yield {"input_shm": "test-only", "input_shm_len": 376, "input_cache_key": key}
        engine = mock.Mock()
        engine.render.side_effect = [
            {"input_cache_hit": False},
            {"width": 2, "height": 2, "full_width": 6, "full_height": 10},
        ]
        with mock.patch.object(server, "preview_engine", return_value=engine), \
                mock.patch.object(server, "file_key", return_value="source"), \
                mock.patch.object(server, "src_path", return_value=Path("frame.dng")), \
                mock.patch.object(server.color_pipeline, "decode_raw", return_value=pixels) as decode, \
                mock.patch.object(server, "array_shared_input", side_effect=shared_array):
            result = server.render_viewport_rust("frame.dng", {"rotate": 90}, None,
                Path("unused.rgba"), {"x": 4, "y": 8, "width": 6, "height": 8})
        decode.assert_called_once()
        request = engine.render.call_args_list[1].args[0]
        self.assertEqual(request["rotate_quarters_ccw"], 3)
        self.assertEqual(request["viewport"], {"x": 4, "y": 8, "width": 2, "height": 2})
        self.assertEqual(result["viewport"], request["viewport"])
        self.assertNotIn("input_width", request, "Python metadata escaped into worker protocol")
        self.assertNotIn("input_height", request)

    def test_processed_tiff_header_clamps_first_rotated_edge_without_reading_pixels(self):
        import tifffile as tf
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decoded.tif"
            tf.imwrite(path, np.zeros((6, 10, 3), dtype=np.uint16))
            engine = mock.Mock()
            engine.render.return_value = {"width": 2, "height": 2, "full_width": 6, "full_height": 10}
            with mock.patch.object(server, "preview_engine", return_value=engine), \
                    mock.patch.object(server, "tiff_for", return_value=path), \
                    mock.patch.object(tf.TiffPage, "asarray", side_effect=AssertionError("full TIFF read")):
                result = server.render_viewport_rust("frame.jpg", {"rotate": 270}, None,
                    Path("unused.rgba"), {"x": 4, "y": 8, "width": 20, "height": 20})
            request = engine.render.call_args.args[0]
            self.assertEqual(request["rotate_quarters_ccw"], 1)
            self.assertEqual(request["input"], str(path))
            self.assertEqual(result["viewport"], {"x": 4, "y": 8, "width": 2, "height": 2})

    def test_clipped_delivery_geometry_survives_render_cache_hit(self):
        requested = {"x": 8, "y": 4, "width": 8, "height": 6}
        actual = {"x": 8, "y": 4, "width": 2, "height": 2}
        def render(name, params, width, output, native, viewport):
            self.assertEqual(viewport, requested)
            native.parent.mkdir(parents=True, exist_ok=True)
            server.write_native_surface(native, np.zeros((2, 2, 3), dtype=np.uint8))
            return {"mean": 0.5, "width": 2, "height": 2, "full_width": 10, "full_height": 6,
                    "viewport": actual, "viewport_accelerated": True, "backend": "test"}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(server, "CACHE", Path(directory)), \
                mock.patch.object(server, "RUST_AVAILABLE", True), \
                mock.patch.object(server, "file_key", return_value="source"), \
                mock.patch.object(server, "prune_render_cache_throttled"), \
                mock.patch.object(server, "render_rust", side_effect=render) as worker:
            first = server._render_preview("frame.dng", {"profile_enabled": True}, 1100,
                engine="rs", native=True, viewport=requested)
            second = server._render_preview("frame.dng", {"profile_enabled": True}, 1100,
                engine="rs", native=True, viewport=requested)
        worker.assert_called_once()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["native"]["viewport"], dict(actual, fullWidth=10, fullHeight=6))
        self.assertEqual(second["native"]["viewport"], first["native"]["viewport"])
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(requested, {"x": 8, "y": 4, "width": 8, "height": 6})
