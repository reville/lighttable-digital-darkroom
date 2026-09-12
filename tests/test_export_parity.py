# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile
from PIL import Image, ImageCms

import color_pipeline
import film_pipeline
import render_cli
import server


class ExportParityTests(unittest.TestCase):
    def test_direct_rust_export_is_limited_to_parity_safe_recipes(self):
        basic = {
            "format": "jpeg", "outputSpace": "srgb",
            "optics": {}, "heals": [], "masks": [],
            "watermark": {"enabled": False},
        }
        self.assertTrue(server.rust_direct_export_supported(basic))
        self.assertFalse(server.rust_direct_export_supported(
            dict(basic, outputSpace="prophoto")))
        self.assertFalse(server.rust_direct_export_supported(
            dict(basic, format="tif")))
        self.assertFalse(server.rust_direct_export_supported(
            dict(basic, heals=[{"enabled": True}])))
        self.assertFalse(server.rust_direct_export_supported(dict(
            basic, masks=[{"type": "brush", "strokes": [{
                "points": [[0.5, 0.5]]}], "grade": {"exposure": 1}}])))
        # Rust has no elliptical/rotated radial, no collapsed-linear guard,
        # and no depth-interval ramp; those recipes must use the Python path.
        self.assertTrue(server.rust_direct_export_supported(dict(
            basic, masks=[{"type": "radial", "radius": 0.3}])))
        for mask in (
            {"type": "radial", "radius": 0.3, "radiusX": 0.5},
            {"type": "radial", "radius": 0.3, "radiusY": 0.1},
            {"type": "radial", "radius": 0.3, "angle": 45},
            {"type": "linear", "start": [0.5, 0.5], "end": [0.50001, 0.5]},
            {"type": "depth", "bitmap": {
                "width": 1, "height": 1, "data": "AA=="},
             "depthLow": 0.2, "depthHigh": 0.8},
        ):
            with self.subTest(mask=mask.get("type"), extra=mask):
                self.assertFalse(server.rust_direct_export_supported(
                    dict(basic, masks=[mask])))
        self.assertTrue(server.rust_direct_export_supported(dict(
            basic, masks=[{"type": "linear", "start": [0.1, 0.5],
                           "end": [0.9, 0.5]}])))
        self.assertTrue(server.rust_direct_export_supported(dict(
            basic, masks=[{"type": "subject", "bitmap": {
                "width": 1, "height": 1, "data": "AA=="}}])))

    @unittest.skipUnless(os.name == "posix", "POSIX shared memory transport")
    def test_resident_raw_render_uses_shared_input_without_building_tiff(self):
        seen = []

        @contextmanager
        def shared(*_args, **_kwargs):
            yield {"input_shm": "pixels", "input_shm_len": 64,
                   "input_cache_key": "source"}

        engine = mock.Mock()
        engine.render.side_effect = lambda request: (
            seen.append(dict(request)) or {"width": 2, "height": 1})
        with mock.patch.object(server, "is_raw", return_value=True), \
                mock.patch.object(server, "raw_shared_input", shared), \
                mock.patch.object(server, "BACKGROUND_ENGINE", engine), \
                mock.patch.object(server, "tiff_for") as tiff:
            metrics = server._resident_render_full("frame.dng", {}, {})
        tiff.assert_not_called()
        self.assertEqual(seen[0]["input_shm"], "pixels")
        self.assertEqual(metrics["input_transport"], "shared-memory-rgb16")

    @unittest.skipUnless(os.name == "posix", "POSIX shared memory transport")
    def test_resident_raw_render_falls_back_when_shared_memory_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.tif"
            source.write_bytes(b"cached input")
            engine = mock.Mock()
            engine.render.return_value = {"width": 2, "height": 1}
            with mock.patch.object(server, "is_raw", return_value=True), \
                    mock.patch.object(server, "raw_shared_input",
                                      side_effect=PermissionError("denied")), \
                    mock.patch.object(server, "BACKGROUND_ENGINE", engine), \
                    mock.patch.object(server, "tiff_for",
                                      return_value=source):
                metrics = server._resident_render_full("frame.dng", {}, {})
            self.assertEqual(engine.render.call_args.args[0]["input"],
                             str(source))
            self.assertEqual(metrics["input_transport"], "tiff-fallback")
            self.assertEqual(metrics["input_fallback"], "PermissionError")

    def test_direct_jpeg_path_finishes_in_rust_and_embeds_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery.jpg"
            profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")).tobytes()

            def render(_name, _params, request):
                Image.new("RGB", (8, 6), (20, 40, 60)).save(
                    request["output"], "JPEG")
                return {"width": 8, "height": 6, "total_ms": 3.0}

            job = {
                "params": dict(server.fp.DEFAULT_PARAMS),
                "grade": {"exposure": 0.2}, "masks": [],
                "crop": None, "optics": {}, "heals": [],
                "format": "jpeg", "quality": 92,
                "outputSpace": "srgb", "longEdge": None,
                "watermark": {"enabled": False}, "metadata": "none",
            }
            with mock.patch.object(server, "_resident_render_full",
                                   side_effect=render), \
                    mock.patch.object(server, "expansion_anchor_for",
                                      return_value=None), \
                    mock.patch.object(server.color_pipeline, "icc_bytes",
                                      return_value=profile), \
                    mock.patch.object(server, "finish_export") as finish:
                result = server.export_with_resident_engine(
                    "frame.jpg", output, job)
            finish.assert_not_called()
            self.assertTrue(result["direct_export"])
            with Image.open(output) as image:
                self.assertEqual(image.size, (8, 6))
                self.assertEqual(image.info["icc_profile"], profile)

    def test_direct_export_failure_returns_to_precision_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            (cache / "export-film").mkdir(parents=True)
            output = root / "delivery.jpg"
            job = {
                "params": dict(server.fp.DEFAULT_PARAMS),
                "grade": {"exposure": 0.2}, "masks": [],
                "crop": None, "optics": {}, "heals": [],
                "format": "jpeg", "quality": 92,
                "outputSpace": "srgb", "longEdge": None,
                "watermark": {"enabled": False}, "metadata": "none",
            }

            def resident(_name, _params, request):
                if request.get("grade") is not None:
                    raise RuntimeError("direct path unavailable")
                tifffile.imwrite(request["output"],
                                 np.zeros((2, 3, 3), dtype=np.float32),
                                 photometric="rgb")
                return {"width": 3, "height": 2, "total_ms": 4.0}

            def finish(_film, destination, _job):
                Image.new("RGB", (3, 2), "black").save(destination, "JPEG")
                return 3, 2

            with mock.patch.object(server, "CACHE", cache), \
                    mock.patch.object(server, "render_key", return_value="key"), \
                    mock.patch.object(server, "expansion_anchor_for",
                                      return_value=None), \
                    mock.patch.object(server, "_resident_render_full",
                                      side_effect=resident), \
                    mock.patch.object(server, "finish_export",
                                      side_effect=finish):
                result = server.export_with_resident_engine(
                    "frame.jpg", output, job)
            self.assertFalse(result["direct_export"])
            self.assertEqual(result["direct_fallback"], "RuntimeError")
            with Image.open(output) as image:
                self.assertEqual(image.size, (3, 2))

    def test_resident_finish_and_cli_match_for_prophoto_tiff(self):
        """External-edit TIFFs must not depend on which export path ran."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            resident = root / "resident.tif"
            worker = root / "worker.tif"
            job_path = root / "job.json"
            y, x = np.mgrid[:48, :64]
            image = np.stack((x / 63, y / 47, (x + y) / 110), axis=2)
            tifffile.imwrite(source, (image * 65535 + 0.5).astype(np.uint16),
                             photometric="rgb")
            mask = np.zeros((12, 16), dtype=np.uint8)
            mask[:, :10] = 255
            params = film_pipeline.clean_params({
                "profile_enabled": False, "linear_input": False,
            })
            job = {
                "params": params,
                "grade": {"pointColor": [{
                    "hue": 30, "range": 45, "hueShift": 8,
                    "uniformHue": 0.3, "uniformSaturation": 0.25,
                    "uniformLuminance": 0.2,
                    "refSaturation": 0.55, "refLuminance": 0.7,
                }]},
                "masks": [{
                    "id": "range", "type": "subject", "enabled": True,
                    "opacity": 0.8, "lumaLow": 0.1, "lumaHigh": 0.95,
                    "colorHue": 35, "colorRange": 40, "colorAmount": 0.7,
                    "bitmap": {"width": 16, "height": 12,
                               "data": __import__("base64").b64encode(
                                   mask.tobytes()).decode()},
                    "grade": {"exposure": 0.2, "texture": -0.2,
                              "clarity": 0.15},
                }],
                "format": "tif", "quality": 100,
                "outputSpace": "prophoto", "metadata": "none",
                "watermark": {"enabled": False},
            }
            server.finish_export(source, resident, job)
            job_path.write_text(json.dumps(job))
            previous = sys.argv
            try:
                sys.argv = ["render_cli.py", str(source), str(worker),
                            str(job_path)]
                with redirect_stdout(StringIO()):
                    render_cli.main()
            finally:
                sys.argv = previous

            resident_pixels = tifffile.imread(resident)
            worker_pixels = tifffile.imread(worker)
            self.assertEqual(resident_pixels.dtype, np.uint16)
            self.assertTrue(np.array_equal(resident_pixels, worker_pixels))

    def test_cli_export_embeds_camera_exif_from_the_named_capture(self):
        """The worker renders from an intermediate TIFF, so the job names the
        capture; the export must carry its EXIF minus location."""
        try:
            import exiv2
        except Exception:  # noqa: BLE001 - the binding is optional
            self.skipTest("the exiv2 binding is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.linspace(
                0.05, 0.95, 12 * 16 * 3, dtype=np.float32).reshape(12, 16, 3)
            capture = root / "capture.jpg"
            color_pipeline.save_export_image(
                pixels, capture, fmt="jpeg", quality=90, output_space="srgb")
            image = exiv2.ImageFactory.open(str(capture))
            image.readMetadata()
            exif = image.exifData()
            exif["Exif.Image.Make"] = "Ricoh"
            exif["Exif.Image.Model"] = "GR III"
            exif["Exif.GPSInfo.GPSLatitudeRef"] = "N"
            image.setExifData(exif)
            image.writeMetadata()
            render_source = root / "render.tif"
            tifffile.imwrite(render_source,
                             (pixels * 65535 + 0.5).astype(np.uint16),
                             photometric="rgb")
            output = root / "delivery.jpg"
            job_path = root / "job.json"
            job_path.write_text(json.dumps({
                "params": film_pipeline.clean_params({
                    "profile_enabled": False, "linear_input": False,
                }),
                "grade": {}, "format": "jpeg", "quality": 90,
                "outputSpace": "srgb", "metadata": "all-except-location",
                "watermark": {"enabled": False},
                "metadataSource": str(capture),
            }))
            previous = sys.argv
            try:
                sys.argv = ["render_cli.py", str(render_source), str(output),
                            str(job_path)]
                with redirect_stdout(StringIO()):
                    render_cli.main()
            finally:
                sys.argv = previous

            written = exiv2.ImageFactory.open(str(output))
            written.readMetadata()
            keys = {datum.key(): datum.toString()
                    for datum in written.exifData()}
            self.assertEqual(keys.get("Exif.Image.Make"), "Ricoh")
            self.assertEqual(keys.get("Exif.Image.Model"), "GR III")
            self.assertNotIn("Exif.GPSInfo.GPSLatitudeRef", keys)


if __name__ == "__main__":
    unittest.main()
