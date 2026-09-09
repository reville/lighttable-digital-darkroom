from __future__ import annotations

import io
from html import unescape
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import server


ROOT = Path(__file__).resolve().parents[1]


class ShortcutContractTests(unittest.TestCase):
    def test_speed_keys_are_stable_and_do_not_overlap_culling(self):
        source = (ROOT / "web" / "labels.js").read_text()
        expected = {
            "e": "exposure", "j": "contrast", "h": "highlights",
            "s": "shadows", "w": "whites", "k": "blacks",
            "t": "temp", "i": "tint", "v": "vibrance",
            "l": "saturation", "y": "clarity", "z": "dehaze",
        }
        schemes = re.findall(
            r"(?:lighttable|classic):\s*\{(.*?)(?=\n  \},)",
            source,
            flags=re.DOTALL,
        )
        self.assertEqual(len(schemes), 2)
        for scheme in schemes:
            speed_source = re.search(r"speed:\s*\{(.*?)\}", scheme,
                                     flags=re.DOTALL).group(1)
            speed = dict(re.findall(r"([a-z]):\s*'([^']+)'", speed_source))
            self.assertEqual(speed, expected)
            culling = set(re.findall(
                r"'(\w)'", " ".join(re.findall(
                    r"(?:pick|reject|unflag):\s*\[(.*?)\]", scheme))))
            survey = re.search(r"survey:\s*'(.)'", scheme).group(1)
            self.assertTrue(set(speed).isdisjoint(culling | {survey} |
                                                  set("0123456789")))


class CacheIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX shared memory transport")
    def test_full_raw_exchange_is_an_unlinked_rgb16_shared_surface(self):
        from multiprocessing import shared_memory

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "frame.dng").write_bytes(b"source")
            pixels = np.array([[[0, 32768, 65535], [7, 8, 9]]],
                              dtype=np.uint16)
            with mock.patch.object(server, "FOLDER", folder), \
                    mock.patch.object(server.color_pipeline, "decode_raw",
                                      return_value=pixels), \
                    mock.patch.object(
                        server.color_pipeline, "raw_decode_fingerprint",
                        return_value="capture"):
                try:
                    with server.raw_shared_input("frame.dng", {}) as request:
                        attached = shared_memory.SharedMemory(
                            name=request["input_shm"], create=False)
                        try:
                            magic, width, height, row_bytes = \
                                server.RAW_SHARED_HEADER.unpack_from(attached.buf)
                            mapped = np.ndarray(
                                (height, width, 3), dtype="<u2",
                                buffer=attached.buf,
                                offset=server.RAW_SHARED_HEADER.size)
                            self.assertEqual(magic, server.RAW_SHARED_MAGIC)
                            self.assertEqual(row_bytes, width * 6)
                            np.testing.assert_array_equal(mapped, pixels)
                            del mapped
                        finally:
                            attached.close()
                except PermissionError as error:
                    self.skipTest(f"host blocks POSIX shared memory: {error}")
                with self.assertRaises(FileNotFoundError):
                    shared_memory.SharedMemory(
                        name=request["input_shm"], create=False)

    def test_source_content_change_invalidates_file_and_render_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "frame.jpg"
            source.write_bytes(b"first")
            with mock.patch.object(server, "FOLDER", folder):
                first_file = server.file_key(source.name)
                first_render = server.render_key(source.name, {}, 1100, "rs")
                source.write_bytes(b"second and larger")
                second_file = server.file_key(source.name)
                second_render = server.render_key(source.name, {}, 1100, "rs")
            self.assertNotEqual(first_file, second_file)
            self.assertNotEqual(first_render, second_render)

    def test_render_key_is_pinned_to_renderer_and_profile_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "frame.jpg"
            source.write_bytes(b"source")
            with mock.patch.object(server, "FOLDER", folder):
                key = server.render_key(source.name, {}, 1100, "rs")
            self.assertEqual(len(key), 32)
            self.assertEqual(len(server.RENDERER_IDENTITY), 64)
            self.assertEqual(
                server.renderer_provenance()["profileCatalogSha256"],
                server.fp.PROFILE_CATALOG_DIGEST,
            )

    def test_processed_files_ignore_raw_only_white_balance_in_render_key(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            processed = folder / "frame.jpg"
            raw = folder / "frame.dng"
            processed.write_bytes(b"processed")
            raw.write_bytes(b"raw")
            custom = {
                "wb_mode": "custom", "wb_temperature": 3200.0,
                "wb_tint": 0.25,
            }
            with mock.patch.object(server, "FOLDER", folder):
                self.assertEqual(
                    server.render_key(processed.name, {}, 1100, "rs"),
                    server.render_key(processed.name, custom, 1100, "rs"),
                )
                self.assertNotEqual(
                    server.render_key(raw.name, {}, 1100, "rs"),
                    server.render_key(raw.name, custom, 1100, "rs"),
                )

    def test_cache_pruning_uses_bytes_and_keeps_newest_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            oldest = folder / "old.tif"
            newest = folder / "new.tif"
            oldest.write_bytes(b"o" * 8)
            newest.write_bytes(b"n" * 8)
            now = time.time()
            os.utime(oldest, (now - 60, now - 60))
            os.utime(newest, (now, now))
            server.prune_cache(folder, "*.tif", 8)
            self.assertFalse(oldest.exists())
            self.assertTrue(newest.exists())

    def test_cache_pruning_never_deletes_an_in_progress_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            complete = folder / "complete.tif"
            staged = folder / ".frame.decode.123.tif"
            complete.write_bytes(b"complete")
            staged.write_bytes(b"still being written")

            server.prune_cache(folder, "*.tif", 0)

            self.assertFalse(complete.exists())
            self.assertTrue(staged.exists())


class GenerationTests(unittest.TestCase):
    def setUp(self):
        with server.GENERATION_LOCK:
            server.LATEST_GENERATION.clear()

    def test_newer_generation_supersedes_queued_work(self):
        client = "test-client"
        with server.GENERATION_LOCK:
            server.LATEST_GENERATION[client] = 9
            stale = 8 < server.LATEST_GENERATION.get(client, 8)
            current = 9 < server.LATEST_GENERATION.get(client, 9)
        self.assertTrue(stale)
        self.assertFalse(current)

    def test_prefetch_does_not_wait_for_the_background_render_lock(self):
        lock = mock.Mock()
        lock.acquire.return_value = False
        with mock.patch.object(server, "BACKGROUND_RENDER_LOCK", lock):
            with (
                mock.patch.object(server, "is_raw", return_value=False),
                mock.patch.object(server, "render_key", return_value="a" * 32),
                mock.patch.object(server, "src_path"),
                mock.patch.object(server, "guard_local_photo"),
            ):
                response = server.render_preview(
                    "frame.jpg", {}, 1100, priority="prefetch")
        self.assertTrue(response["cancelled"])
        lock.acquire.assert_called_once_with(
            blocking=False, priority="prefetch", cancelled=mock.ANY)
        lock.release.assert_not_called()


class ResponsivePreviewTests(unittest.TestCase):
    def test_native_surface_round_trip_is_tightly_packed_rgba(self):
        source = np.array([
            [[0, 64, 255], [255, 128, 1]],
            [[4, 8, 12], [16, 32, 48]],
        ], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.rgba"
            metadata = server.write_native_surface(path, source)
            rgba, decoded = server.read_native_surface(path)
        self.assertEqual(metadata, {
            "width": 2, "height": 2, "rowBytes": 8,
        })
        self.assertEqual(decoded, metadata)
        np.testing.assert_array_equal(rgba[..., :3], source)
        np.testing.assert_array_equal(rgba[..., 3], 255)

    def test_native_response_can_omit_large_browser_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            native = folder / "render.rgba"
            server.write_native_surface(
                native, np.zeros((3, 4, 3), dtype=np.uint8))
            response = server.preview_response(
                {"ms": 1}, "a" * 32, folder / "missing.jpg", native,
                cached=False, refining=False)
        self.assertNotIn("img", response)
        self.assertEqual(response["helper"],
                         f"/api/render/helper?key={'a' * 32}")
        self.assertEqual(response["native"]["width"], 4)
        self.assertEqual(response["native"]["height"], 3)
        self.assertEqual(response["native"]["headerBytes"], 16)

    def test_deferred_webgl_helper_encodes_from_native_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            native = folder / "render.rgba"
            helper = folder / "render.jpg"
            server.write_native_surface(
                native, np.full((9, 13, 3), 127, dtype=np.uint8))
            server.ensure_jpeg_surface(helper, native)
            with Image.open(helper) as image:
                self.assertEqual(image.size, (13, 9))

    def test_deferred_webgl_helper_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            native = folder / "render.rgba"
            helper = folder / "render.jpg"
            server.write_native_surface(
                native, np.full((600, 900, 3), 127, dtype=np.uint8))
            server.ensure_jpeg_surface(helper, native, 256)
            with Image.open(helper) as image:
                self.assertEqual(image.size, (256, 171))

    def test_every_native_surface_offers_a_sampling_helper(self):
        """Scopes, Auto tone, and the samplers read a WebGL helper texture.

        A photo is rendered twice: an interactive preview and, once the view
        settles, a wider full-resolution one. Both have to offer a helper. When
        the response for the settled render omitted it, the browser was left
        with no sampled pixels at all and every scope drew an empty canvas.
        """
        interactive, settled = 1100, 3000
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for width in (interactive, settled):
                native = folder / f"render-{width}.rgba"
                server.write_native_surface(
                    native, np.zeros((120, width, 3), dtype=np.uint8))
                response = server.preview_response(
                    {"ms": 1}, "a" * 32, folder / "missing.jpg", native,
                    cached=False, refining=False)
                self.assertEqual(response["native"]["width"], width)
                self.assertEqual(
                    response.get("helper"),
                    f"/api/render/helper?key={'a' * 32}",
                    f"a {width}px native surface must offer a sampling helper",
                )

    def test_sampling_helper_cost_does_not_grow_with_the_settled_surface(self):
        """The helper is always encoded at the sampling width, so serving one
        for a full-resolution surface stays as cheap as the interactive one."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sizes = []
            for width, height in ((1100, 874), (3000, 2382)):
                native = folder / f"render-{width}.rgba"
                helper = folder / f"render-{width}.jpg"
                server.write_native_surface(
                    native, np.full((height, width, 3), 127, dtype=np.uint8))
                server.ensure_jpeg_surface(
                    helper, native, server.NATIVE_BROWSER_HELPER_OUTPUT_WIDTH)
                with Image.open(helper) as image:
                    sizes.append(image.size)
        self.assertEqual(sizes[0][0], server.NATIVE_BROWSER_HELPER_OUTPUT_WIDTH)
        self.assertEqual(sizes[1][0], server.NATIVE_BROWSER_HELPER_OUTPUT_WIDTH)
        self.assertEqual(sizes[0], sizes[1])

    def test_resampling_edits_fall_back_to_the_edited_image(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            render = cache / "render"
            edit = cache / "edit"
            render.mkdir()
            edit.mkdir()
            key = "b" * 32
            server.write_native_surface(
                render / f"{key}.rgba",
                np.full((9, 13, 3), 127, dtype=np.uint8))
            result = {
                "native": {"url": f"/api/render/native?key={key}"},
                "helper": f"/api/render/helper?key={key}",
            }
            with (
                mock.patch.object(server, "CACHE", cache),
                mock.patch.object(server, "file_key", return_value="source"),
                mock.patch.object(server, "exif_for", return_value={}),
                mock.patch.object(server.edits, "base_edits_are_identity",
                                  return_value=False),
                mock.patch.object(server.edits, "clean_optics",
                                  return_value={"distortion": 0.2}),
                mock.patch.object(server.edits, "clean_heals", return_value=[]),
                mock.patch.object(server.edits, "lens_profile_for", return_value=None),
                mock.patch.object(server.edits, "apply_base",
                                  side_effect=lambda image, *_: image),
            ):
                response = server.apply_preview_edits(
                    result, "frame.jpg", 1100, {}, {"distortion": 0.2}, [])
            self.assertNotIn("native", response)
            self.assertNotIn("helper", response)
            self.assertTrue(response["img"].startswith("/api/render/png?key="))
            self.assertEqual(len(list(edit.glob("*.jpg"))), 0)
            corrected, _ = server.read_native_surface(render / f"{response['key']}.rgba")
            np.testing.assert_array_equal(corrected[..., :3], 127)

    def test_original_mean_does_not_reload_evicted_linear_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "frame.jpg").write_bytes(b"source")
            pixels = np.full((4, 6, 3), 0.25, dtype=np.float32)
            with (
                mock.patch.object(server, "FOLDER", folder),
                mock.patch.object(server, "linear_for", return_value=pixels) as load,
            ):
                server._ORIG_MEAN_CACHE.clear()
                self.addCleanup(server._ORIG_MEAN_CACHE.clear)
                first = server.orig_mean_display("frame.jpg", 1100)
                second = server.orig_mean_display("frame.jpg", 1100)
        self.assertAlmostEqual(first, 0.25)
        self.assertEqual(second, first)
        load.assert_called_once_with("frame.jpg", 1100)

    def test_fast_raw_selection_does_not_start_background_refinement(self):
        full = mock.Mock()
        full.exists.return_value = False
        fast = Path("/tmp/lighttable-fast-preview.tif")
        with (
            mock.patch.object(server, "raw_preview_path", return_value=full),
            mock.patch.object(server, "build_raw_preview", return_value=fast) as build,
            mock.patch.object(server, "schedule_raw_refinement") as schedule,
        ):
            selected = server.selected_preview_tiff("frame.dng", 1100, {})
        self.assertEqual(selected, fast)
        build.assert_called_once_with("frame.dng", 1100, "fast", {})
        schedule.assert_not_called()

    def test_library_scan_is_reused_until_explicit_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "first.jpg").write_bytes(b"first")
            with mock.patch.object(server, "FOLDER", folder):
                server.invalidate_library_cache()
                self.addCleanup(server.invalidate_library_cache)
                self.assertEqual(server.list_images(), ["first.jpg"])
                (folder / "second.jpg").write_bytes(b"second")
                self.assertEqual(server.list_images(), ["first.jpg"])
                server.invalidate_library_cache()
                self.assertEqual(server.list_images(), ["first.jpg", "second.jpg"])

    def test_develop_mode_opens_raw_with_neutral_pixels_without_camera_flash(self):
        missing = mock.Mock()
        missing.exists.return_value = False
        with (
            mock.patch.object(server, "neutral_preview_path", return_value=missing),
            mock.patch.object(server, "orig_jpeg", return_value=b"preview") as original,
            mock.patch.object(server, "build_neutral_preview", return_value=missing) as build,
            mock.patch.object(server, "file_key", return_value="source-key"),
            mock.patch.object(server, "guard_local_photo"),
        ):
            response = server.render_preview(
                "frame.dng", {"profile_enabled": False}, 1100)
        self.assertFalse(response["refining"])
        self.assertFalse(response["cached"])
        self.assertIn("/api/neutral?", response["img"])
        original.assert_not_called()
        build.assert_called_once()

    def test_develop_mode_uses_accurate_neutral_preview_when_ready(self):
        ready = mock.Mock()
        ready.exists.return_value = True
        with (
            mock.patch.object(server, "neutral_preview_path", return_value=ready),
            mock.patch.object(server, "file_key", return_value="source-key"),
            mock.patch.object(server, "guard_local_photo"),
        ):
            response = server.render_preview(
                "frame.dng", {"profile_enabled": False}, 1100)
        self.assertFalse(response["refining"])
        self.assertIn("/api/neutral?", response["img"])

    def test_native_source_preview_carries_its_decoded_geometry(self):
        encoded = io.BytesIO()
        Image.new("RGB", (12, 8), "blue").save(encoded, "JPEG")
        with (
            mock.patch.object(server, "orig_jpeg",
                              return_value=encoded.getvalue()),
            mock.patch.object(server, "file_key", return_value="source-key"),
            mock.patch.object(server, "guard_local_photo"),
        ):
            response = server.render_preview(
                "frame.jpg", {"profile_enabled": False}, 1100, native=True)
        self.assertEqual(response["native"], {
            "url": response["img"], "format": "image",
            "width": 12, "height": 8, "rowBytes": 0, "headerBytes": 0,
        })


class NativePreviewContractTests(unittest.TestCase):
    def test_native_renderer_maps_every_scalar_webgl_grade_control(self):
        webgl = (ROOT / "web" / "gl.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        defaults = re.search(
            r"export const GRADE_DEFAULTS = \{(?P<body>.*?)\};",
            webgl,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(defaults)
        keys = re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]*)\s*:",
                          defaults.group("body"))
        self.assertGreaterEqual(len(keys), 14)
        for key in keys:
            self.assertIn(f'value("{key}"', swift)
        for band in ("red", "orange", "yellow", "green", "aqua", "blue",
                     "purple", "magenta"):
            self.assertIn(f'hsl("{band}")', swift)
        for curve in ("curveL", "curveR", "curveG", "curveB"):
            self.assertIn(f'"{curve}"', swift)

    def test_app_bundle_contract_includes_native_renderer_and_shader(self):
        build = (ROOT / "build-app.sh").read_text()
        shell = (ROOT / "app" / "main.swift").read_text()
        metal = (ROOT / "app" / "NativePreview.metal").read_text()
        self.assertIn("app/NativePreview.swift", build)
        self.assertIn("app/NativePreview.metal", build)
        self.assertIn("NativePreviewRenderer()", shell)
        self.assertIn("nativePreviewVertex", metal)
        self.assertIn("nativePreviewFragment", metal)

    def test_compiled_runtime_cache_stays_outside_the_signed_bundle(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        self.assertIn('env["NUMBA_CACHE_DIR"]', shell)
        self.assertIn('appendingPathComponent("compiled-runtime")', shell)

    def test_browser_subsystems_are_native_es_modules(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        expected = {
            "api.js", "state.js", "render-scheduler.js", "native-bridge.js",
            "library.js", "editor-panels.js", "color-tools.js", "presets.js",
            "labels.js", "survey.js", "history-panel.js", "metadata-panel.js",
            "catalog-ui.js",
        }
        available = {path.name for path in (ROOT / "web").glob("*.js")}
        self.assertTrue((expected | {"local-ai.js"}).issubset(available))
        for filename in expected:
            self.assertIn(f"'/web/{filename}'", javascript)
        self.assertIn("'/web/local-ai.js'", (ROOT / "web" / "library.js").read_text())
        # The point is that every subsystem is imported at the top of the file
        # rather than pulled in lazily from the middle of it; the bound moves
        # with the number of modules, it is not a budget on how many there are.
        import_lines = [index for index, line in enumerate(javascript.splitlines())
                        if line.startswith("import ") or line.startswith("} from ")]
        self.assertTrue(import_lines)
        self.assertLess(max(import_lines), next(index for index, line in enumerate(javascript.splitlines())
                                              if line.startswith("const $ =")))

    def test_compare_wipe_keeps_both_images_in_gpu_texture_passes(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        webgl = (ROOT / "web" / "gl.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        shell = (ROOT / "app" / "main.swift").read_text()
        metal = (ROOT / "app" / "NativePreview.metal").read_text()
        self.assertIn("postNative('nativeCompare'", javascript)
        self.assertIn("original: {", javascript)
        self.assertIn('case "nativeCompare"', shell)
        self.assertIn("originalTexture", swift)
        self.assertIn("updateComparePosition", swift)
        self.assertIn("texture2d<float> original [[texture(2)]]", metal)
        self.assertIn("uv.x <= grade.compare.x", metal)
        self.assertIn("setOriginalImage", webgl)
        self.assertIn("drawCompare", webgl)
        self.assertIn("texture2D(u_original", webgl)

    def test_native_shader_keeps_masks_manual_optics_and_heals_on_gpu(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        shell = (ROOT / "app" / "main.swift").read_text()
        metal = (ROOT / "app" / "NativePreview.metal").read_text()
        self.assertIn("postNative('nativeMasks'", javascript)
        self.assertIn("postNative('nativeEdits'", javascript)
        self.assertIn('case "nativeMasks"', shell)
        self.assertIn('case "nativeEdits"', shell)
        self.assertIn("func updateMasks", swift)
        self.assertIn("func updateEdits", swift)
        self.assertIn("texture2d<float> masks [[texture(3)]]", metal)
        self.assertIn("manualSourceCoordinate", metal)
        self.assertIn('optics["flipHorizontal"] as? Bool', swift)
        self.assertIn("grade.optics2.x > 0.5", metal)
        self.assertIn("applyHeals", metal)
        self.assertIn("applyLocal", metal)
        self.assertIn("nativeMaskChannelPayload", javascript)
        self.assertIn("data.count == width * height", swift)

    def test_native_shader_keeps_advanced_colour_and_proof_on_gpu(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        metal = (ROOT / "app" / "NativePreview.metal").read_text()
        self.assertIn("softProof: S.softProof", javascript)
        self.assertNotIn("&& !advancedColorActive()", javascript)
        self.assertIn("var point0", swift)
        self.assertIn("var colorGrade0", swift)
        self.assertIn("var softProof", swift)
        self.assertIn("applyColorGradeTone", metal)
        self.assertIn("applySoftProof", metal)

    def test_continuous_preview_work_is_frame_bounded(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        scheduler = (ROOT / "web" / "render-scheduler.js").read_text()
        webgl = (ROOT / "web" / "gl.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        self.assertIn("export function createFrameScheduler", scheduler)
        self.assertIn("const previewFrameScheduler = createFrameScheduler", javascript)
        self.assertIn("const cropFrameScheduler = createFrameScheduler", javascript)
        self.assertIn("const viewFrameScheduler = createFrameScheduler", javascript)
        self.assertIn("const FULL_RESOLUTION_SETTLE_MS = 160", javascript)
        self.assertIn("const renderWhenIdle = () =>", javascript)
        self.assertIn("performance.now() - lastContinuousInputAt", javascript)
        self.assertIn("const maskGeometryCache = new Map()", javascript)
        self.assertIn("canvasGeometryValues", javascript)
        self.assertIn("this._curveRefs", webgl)
        self.assertIn("updateCurves: Bool = true", swift)
        self.assertIn("private func scheduleRender()", swift)

    def test_occluded_paint_wait_has_a_bounded_fallback(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the paint-wait contract")
        javascript = (ROOT / "web" / "app.js").read_text()
        scheduler = (ROOT / "web" / "render-scheduler.js").read_text()
        script = scheduler.replace("export ", "") + """
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};
const startedAt = performance.now();
afterVisiblePaint(20).then((paintedAt) => {
  process.stdout.write(JSON.stringify({
    elapsed: performance.now() - startedAt,
    timestampIsFinite: Number.isFinite(paintedAt),
  }));
});
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertTrue(result["timestampIsFinite"])
        self.assertGreaterEqual(result["elapsed"], 10)
        self.assertLess(result["elapsed"], 500)
        self.assertIn("await afterVisiblePaint();", javascript)
        self.assertIn("await executeUICommand('zoomIn');", javascript)
        self.assertIn("for (let attempt = 0; attempt < 3; attempt++)", javascript)

    def test_reference_overlay_is_a_resident_gpu_texture(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        webgl = (ROOT / "web" / "gl.js").read_text()
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        shell = (ROOT / "app" / "main.swift").read_text()
        metal = (ROOT / "app" / "NativePreview.metal").read_text()
        self.assertIn("setReferenceImage", javascript)
        self.assertIn("postNative('nativeReference'", javascript)
        self.assertNotIn("image.style.transform", javascript)
        self.assertIn("uniform sampler2D u_reference", webgl)
        self.assertIn("setReferenceImage", webgl)
        self.assertIn('case "nativeReference"', shell)
        self.assertIn("func updateReference", swift)
        self.assertIn("texture2d<float> reference [[texture(4)]]", metal)

    def test_library_dom_is_bounded_and_incremental(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        css = (ROOT / "web" / "style.css").read_text()
        self.assertIn("const STRIP_OVERSCAN", javascript)
        self.assertIn("list.slice(start, end)", javascript)
        self.assertIn("visibleGridPositions(_gridLayout", javascript)
        self.assertNotIn("_gridRenderLimit", javascript)
        self.assertIn("content-visibility:auto", css)

    def test_library_filtering_preserves_loaded_thumbnail_nodes(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        survey = (ROOT / "web" / "survey.js").read_text()
        strip = re.search(
            r"function renderStrip\(.*?(?=\nfunction createGridCell)",
            javascript,
            flags=re.DOTALL,
        ).group(0)
        grid = re.search(
            r"function renderGrid\(.*?(?=\nlet libraryScrollFrame)",
            javascript,
            flags=re.DOTALL,
        ).group(0)
        self.assertIn("function reconcileChildren", javascript)
        self.assertIn("syncThumbnailImage(element, im)", javascript)
        self.assertIn("reconcileChildren(host, ordered)", strip)
        self.assertIn("reconcileChildren(grid, ordered)", grid)
        self.assertIn("position.top - anchor.top", grid)
        self.assertNotIn("replaceChildren", strip)
        self.assertNotIn("replaceChildren", grid)
        self.assertIn("reconcileSurveyCells(ordered)", survey)
        self.assertIn("syncSurveyCell(cell, image", survey)
        self.assertNotIn("grid.innerHTML", survey)

    def test_compare_requires_explicit_mode_and_only_its_divider_drags(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        html = (ROOT / "web" / "index.html").read_text()
        css = (ROOT / "web" / "style.css").read_text()
        self.assertIn('id="compareBtn"', html)
        self.assertNotIn('id="wipe"', html)
        self.assertIn("$('compareBtn').addEventListener('click'", javascript)
        self.assertIn("bar.addEventListener('pointerdown'", javascript)
        self.assertNotIn("cmp.addEventListener('pointerdown'", javascript)
        self.assertIn("if (compareEditingBlocked()) setCompareActive(false);", javascript)
        self.assertIn('id="compareOverlay" hidden', html)
        self.assertIn("const bar = $('compareBar');", javascript)

    def test_zoom_targets_and_panel_geometry_remain_stable(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        css = (ROOT / "web" / "style.css").read_text()
        gl = (ROOT / "web" / "gl.js").read_text()
        self.assertIn("const baseW = currentZoom > 0 ? r.width / currentZoom", javascript)
        self.assertIn("S.targetPixelScale = 1;", javascript)
        self.assertIn("displaySourcePixelWidth() / baseW", javascript)
        self.assertIn("preservePresentationGeometry", javascript)
        self.assertIn("preserveCanvasSize: preservePresentationGeometry", javascript)
        self.assertIn(".cmp.preview-framed > #cv", css)
        self.assertIn("this.canvas.width !== imageWidth", gl)
        self.assertNotIn("transition:grid-template-columns", css)

    def test_same_photo_preview_swaps_keep_geometry_but_rotation_does_not(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the preview geometry contract")
        javascript = (ROOT / "web" / "app.js").read_text()
        functions = []
        for name in ("previewGeometryKey", "shouldPreservePresentationGeometry"):
            match = re.search(
                rf"function {name}\(.*?\n\}}",
                javascript,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(functions) + """
const base = previewGeometryKey('photo.raw', 0);
process.stdout.write(JSON.stringify({
  same: shouldPreservePresentationGeometry(
    'interactive', base, previewGeometryKey('photo.raw', 180)),
  rotate: shouldPreservePresentationGeometry(
    'interactive', base, previewGeometryKey('photo.raw', 90)),
  photo: shouldPreservePresentationGeometry(
    'interactive', base, previewGeometryKey('other.raw', 0)),
  settled: shouldPreservePresentationGeometry(
    'settled', base, previewGeometryKey('photo.raw', 0)),
}));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(
            json.loads(completed.stdout),
            {"same": True, "rotate": False, "photo": False, "settled": False},
        )
        self.assertIn("$('pw').onchange = () => renderFilm(0);", javascript)

    def test_export_toolbar_opens_configuration_before_running(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        html = (ROOT / "web" / "index.html").read_text()
        export_button = re.search(
            r'<button[^>]*id="exportBtn"[^>]*>(.*?)</button>',
            html,
            flags=re.DOTALL,
        ).group(0)
        self.assertIn('aria-label="Export photos"', export_button)
        self.assertIn("<svg", export_button)
        self.assertNotIn(">Export<", export_button)
        self.assertIn("$('exportBtn').onclick = openExportModal;", javascript)
        # Wrapped, not handed the handler directly: runExport's first argument
        # is the option overrides, and a MouseEvent carries a truthy `which`
        # that would replace the export scope with a mouse button number.
        self.assertIn("$('exportBtn2').onclick = () => runExport();", javascript)
        self.assertIn("$('exportModalRun').onclick = async () => {", javascript)

    def test_crop_pane_immediately_activates_crop_mode(self):
        javascript = (ROOT / "web" / "app.js").read_text()
        self.assertIn("if (id === 'cropPane') { beginCropSession(); setCropMode(true); }", javascript)
        self.assertIn(
            "if (S.activePane === 'cropPane') beginCropSession();",
            javascript,
        )
        self.assertIn("selectPhotoTool('cropPane');", javascript)
        self.assertIn("if (S.activePane === id && !S.compareActive) exitPhotoTool();", javascript)
        blocked = re.search(
            r"function compareEditingBlocked\(\) \{(.*?)\n\}",
            javascript,
            flags=re.DOTALL,
        ).group(1)
        self.assertNotIn("cropPane", blocked)

    def test_primary_sidebar_has_eight_photo_tools_without_section_labels(self):
        html = (ROOT / "web" / "index.html").read_text()
        toolrail = re.search(
            r'<nav class="toolrail".*?</nav>', html, flags=re.DOTALL
        ).group(0)
        panes = re.findall(r'data-pane="([^"]+)"', toolrail)
        self.assertEqual(
            panes,
            [
                "editPane", "presetsPane", "filmPane", "cropPane",
                "healPane", "maskPane", "infoPane", "historyPane",
            ],
        )
        self.assertEqual(
            re.findall(r'<span class="toolrail-group">([^<]+)</span>',
                       toolrail),
            [],
        )

    def test_native_window_chrome_tracks_live_control_bounds(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        javascript = (ROOT / "web" / "window-chrome.js").read_text()
        html = (ROOT / "web" / "index.html").read_text()
        self.assertIn('case "windowChromeLayout"', shell)
        self.assertIn("updateWindowChromeLayout", shell)
        self.assertIn("event.clickCount == 2", shell)
        self.assertIn("window.performDrag(with: event)", shell)
        self.assertIn("topBar.querySelectorAll(CONTROL_SELECTOR)", javascript)
        self.assertIn(
            "!element.matches(':disabled, [aria-disabled=\"true\"]')",
            javascript,
        )
        self.assertIn("new ResizeObserver(schedule)", javascript)
        self.assertIn("new MutationObserver(schedule)", javascript)
        self.assertIn(".modal-backdrop.on", javascript)
        self.assertIn("blocked: dragBlocked", javascript)
        self.assertIn("sendNative('windowChromeLayout'", javascript)
        self.assertIn("data-window-no-drag", html)

    def test_native_traffic_lights_match_the_unified_header_geometry(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        self.assertIn('reference.toolbarStyle = .unified', shell)
        self.assertIn('reference.standardWindowButton($0)?.frame', shell)
        self.assertIn('return ceil(last.maxX + first.minX)', shell)
        self.assertIn('zip(buttons, WindowChrome.nativeTrafficLightFrames)', shell)
        self.assertNotIn('button.sizeToFit()', shell)
        self.assertIn('let centerFromTop = WindowChrome.topBarHeight / 2', shell)
        self.assertIn('func windowDidUpdate', shell)
        self.assertIn('guard !window.styleMask.contains(.fullScreen)', shell)
        self.assertIn('layoutTrafficLights()', shell)

    def test_native_menu_bar_uses_editor_commands_and_state(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        javascript = (ROOT / "web" / "app.js").read_text()
        html = (ROOT / "web" / "index.html").read_text()
        for title in ("File", "Edit", "Library", "Photo", "Develop",
                      "View", "Window", "Help"):
            self.assertIn(f'NSMenu(title: L("{title}"))', shell)
        self.assertIn('case "menuState"', shell)
        self.assertIn('"type": "menuCommand"', shell)
        self.assertIn("func validateMenuItem", shell)
        self.assertIn("function performNativeMenuCommand", javascript)
        self.assertIn("postNative('menuState'", javascript)
        self.assertIn("meta && e.shiftKey && e.key.toLowerCase() === 'c'",
                      javascript)
        self.assertIn("Ctrl/⌘⇧C", html)
        self.assertNotIn('action: #selector(revealExports(_:)), keyEquivalent: "e"',
                         shell)


class UxInteractionContractTests(unittest.TestCase):
    def setUp(self):
        self.javascript = (ROOT / "web" / "app.js").read_text()
        self.html = (ROOT / "web" / "index.html").read_text()
        self.css = (ROOT / "web" / "style.css").read_text()

    def test_navigation_and_prefetch_follow_the_filtered_order(self):
        self.assertIn("function goRelative(direction)", self.javascript)
        self.assertIn("const ordered = visible();", self.javascript)
        self.assertNotIn("go(S.idx + 1)", self.javascript)
        self.assertNotIn("go(S.idx - 1)", self.javascript)

    def test_visible_thumbnails_progress_to_edit_aware_renditions(self):
        self.assertIn("/api/thumb/rendered?name=", self.javascript)
        self.assertIn("new IntersectionObserver", self.javascript)
        self.assertIn("const EDITED_THUMB_CONCURRENCY = 2", self.javascript)
        self.assertIn("const EDITED_THUMB_RETRY_LIMIT = 36", self.javascript)
        self.assertIn("_editedThumbnailObserver.unobserve(thumbnail)",
                      self.javascript)
        self.assertIn("image.dataset.thumbnailKind = 'source'", self.javascript)
        self.assertIn("image.dataset.thumbnailKind = 'edited'", self.javascript)
        sender = self.javascript[
            self.javascript.index("const editSaveQueue = createEditSaveQueue({"):
            self.javascript.index("async function flushEditSaves()")
        ]
        self.assertIn("const result = await api('/api/state', {...payload.state,", sender)
        self.assertIn("expectedRecoverySourceKey: payload.expectedRecoverySourceKey", sender)
        self.assertIn("if (!result?.ok || result.error) throw", sender)
        self.assertIn("invalidateEditedThumbnail(image)", sender)
        self.assertLess(sender.index("if (!result?.ok || result.error) throw"),
                        sender.index("invalidateEditedThumbnail(image)"))

    def test_midi_controls_live_with_hardware_settings_not_topbar(self):
        topbar = self.html.split('<div class="app" id="appShell">', 1)[0]
        hardware_settings = re.search(
            r'<details class="shortcut-details">.*?id="midiPill".*?</details>',
            self.html,
            flags=re.DOTALL,
        )
        self.assertNotIn('id="midiPill"', topbar)
        self.assertIsNotNone(hardware_settings)
        self.assertNotIn(".search-box kbd, .midi-pill", self.css)

    def test_navigation_hides_stale_pixels_until_the_new_photo_is_ready(self):
        self.assertIn("setRenderPresentation('pending', im.name)",
                      self.javascript)
        self.assertIn(".zoomwrap.photo-pending::after", self.css)
        self.assertIn("++S.seq;", self.javascript)

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript state contract")
        visibility_function = re.search(
            r"function nativePreviewCanDraw\(state\) \{.*?\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(visibility_function)
        script = visibility_function.group(0) + """
const states = ['pending', 'ready', 'error', ''];
process.stdout.write(JSON.stringify(states.map(nativePreviewCanDraw)));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(
            json.loads(completed.stdout), [True, True, False, False])
        viewport_function = self.javascript[
            self.javascript.index("function nativeViewportPayload()"):
            self.javascript.index("let lastNativeViewportKey")
        ]
        self.assertIn(
            "nativePreviewCanDraw(S.renderState)", viewport_function)

    def test_native_sampling_helper_preserves_presented_photo_geometry(self):
        renderer = (ROOT / "web" / "gl.js").read_text()
        self.assertIn(
            "setImage(img, { resizeCanvas = true, cacheKey = null } = {})", renderer)
        resize_guard = renderer[
            renderer.index("setImage(img, { resizeCanvas = true, cacheKey = null } = {})"):
            renderer.index("setOriginalImage(img)")
        ]
        self.assertIn("if (resizeCanvas &&", resize_guard)
        self.assertIn("gl.uniform2f(this.uTexel, 1 / imageWidth, 1 / imageHeight)",
                      resize_guard)

        helper = self.javascript[
            self.javascript.index("function scheduleNativeHelper"):
            self.javascript.index("function setNativeBaseImage")
        ]
        self.assertIn("setWebGLBaseImage(url, {", helper)
        self.assertIn("preserveCanvasSize: true", helper)
        self.assertIn("forceWebGLDraw: true", helper)
        self.assertIn("generation,", helper)
        # A response that carries no dedicated helper still presents a JPEG
        # surface, and sampling falls back to it rather than going blind.
        native_loader = self.javascript[
            self.javascript.index("function setNativeBaseImage"):
            self.javascript.index("function originalPreviewURL")
        ]
        self.assertIn("scheduleNativeHelper(render.helper || render.img, generation)",
                      native_loader)
        # Cancelling the pending helper when a render starts stranded the
        # sampling surface, because the early return skips the generation bump.
        render_preamble = self.javascript[
            self.javascript.index("async function doRender("):
            self.javascript.index("const my = ++S.seq;")
        ]
        self.assertNotIn("clearTimeout(nativeHelperTimer)", render_preamble)
        webgl_loader = self.javascript[
            self.javascript.index("function setWebGLBaseImage"):
            self.javascript.index(
                "/* ------------------------------------------------------ reference matching")
        ]
        self.assertIn(
            "S.gl.setImage(img, { resizeCanvas: !preserveCanvasSize, cacheKey })",
            webgl_loader,
        )
        self.assertIn("generation !== S.seq", webgl_loader)
        self.assertIn("forceWebGL: true", webgl_loader)

    def test_sampling_and_scope_controls_keep_the_native_photo_visible(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript state contract")
        native_function = re.search(
            r"function nativePreviewActive\(\) \{.*?\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(native_function)
        script = """
const NATIVE_PREVIEW = true;
const S = {optics: {profileEnabled: false}, heals: [], pointColorPick: false,
  maskColorPick: false, wbPick: false, clip: false};
""" + native_function.group(0) + """
const results = [nativePreviewActive()];
S.pointColorPick = true; results.push(nativePreviewActive());
S.pointColorPick = false; S.maskColorPick = true; results.push(nativePreviewActive());
S.maskColorPick = false; S.wbPick = true; results.push(nativePreviewActive());
S.wbPick = false; S.clip = true; results.push(nativePreviewActive());
S.clip = false; S.optics.profileEnabled = true; results.push(nativePreviewActive());
S.optics.profileEnabled = false;
S.heals = Array.from({length: 17}, () => ({enabled: true}));
results.push(nativePreviewActive());
process.stdout.write(JSON.stringify(results));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(
            json.loads(completed.stdout),
            [True, True, True, True, True, True, True],
        )

    def test_webgl_fallback_is_exposed_only_after_it_was_presented(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript state contract")
        presented_function = re.search(
            r"function useWebGLPreview\(.*?\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(presented_function)
        script = presented_function.group(0) + """
const cases = [[false, 'native-metal'], [false, null], [false, 'webgl'],
  [true, 'webgl'], [true, 'native-metal']];
process.stdout.write(JSON.stringify(cases.map((args) => useWebGLPreview(...args))));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(
            json.loads(completed.stdout), [False, False, True, False, False])
        viewport_function = self.javascript[
            self.javascript.index("function nativeViewportPayload()"):
            self.javascript.index("let lastNativeViewportKey")
        ]
        self.assertIn("!useWebGLPreview", viewport_function)

    def test_backend_return_and_failed_presentations_cannot_reuse_stale_pixels(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript state contract")
        refresh_function = re.search(
            r"function nativePreviewNeedsRefresh\(.*?\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        remember_function = re.search(
            r"function rememberPresentedRender\(.*?\n\}",
            self.javascript,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(refresh_function)
        self.assertIsNotNone(remember_function)
        script = refresh_function.group(0) + remember_function.group(0) + """
const refreshCases = [
  [null, true, null],
  [false, true, 'webgl'],
  [false, true, null],
  [false, true, 'native-metal'],
  [true, true, 'webgl'],
  [true, false, 'native-metal'],
];
const failed = { presentedRenderKey: 'old', presentedBackend: 'native-metal' };
rememberPresentedRender(failed, 'new', 'webgl', { failed: true });
const succeeded = { presentedRenderKey: null, presentedBackend: null };
rememberPresentedRender(succeeded, 'new', 'webgl', { failed: false });
process.stdout.write(JSON.stringify({
  refresh: refreshCases.map((args) => nativePreviewNeedsRefresh(...args)),
  failed, succeeded,
}));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["refresh"], [False, True, True, False, False, False])
        self.assertEqual(
            result["failed"],
            {"presentedRenderKey": None, "presentedBackend": None},
        )
        self.assertEqual(
            result["succeeded"],
            {"presentedRenderKey": "new", "presentedBackend": "webgl"},
        )
        backend_sync = self.javascript[
            self.javascript.index("function syncPreviewBackend()"):
            self.javascript.index("function scheduleNativeViewportLayout()")
        ]
        self.assertIn("setRenderPresentation('pending', cur().name)", backend_sync)
        self.assertIn("renderFilm(0)", backend_sync)
        webgl_loader = self.javascript[
            self.javascript.index("function setWebGLBaseImage"):
            self.javascript.index("/* ------------------------------------------------------ reference matching")
        ]
        self.assertIn("failed: true", webgl_loader)
        self.assertIn("WebGL preview could not be initialized", webgl_loader)
        base_loader = self.javascript[
            self.javascript.index("async function setBaseImage"):
            self.javascript.index("function setWebGLBaseImage")
        ]
        self.assertIn("generation === S.seq && !timing.cancelled", base_loader)

    def test_external_ui_state_reports_the_render_that_is_actually_visible(self):
        report = self.javascript[
            self.javascript.index("function uiStateReport()"):
            self.javascript.index("async function applyServerStateEvent")
        ]
        self.assertIn("state: S.renderState", report)
        self.assertIn("name: S.renderName", report)
        self.assertIn("backend: S.presentedBackend", report)

    def test_one_to_one_uses_source_dimensions_and_has_space_toggle(self):
        self.assertIn("function sourceLongEdge", self.javascript)
        self.assertIn("function requestedPreviewWidth", self.javascript)
        self.assertIn("e.code === 'Space'", self.javascript)
        self.assertIn("sourceLongEdge(photo)", self.javascript)
        self.assertIn("Toggle Fit / 1:1", self.html)

    def test_selection_supports_visible_range_extension(self):
        self.assertIn("let selectionAnchorName", self.javascript)
        self.assertIn("list.slice(first, last + 1)", self.javascript)
        self.assertIn("event.shiftKey", self.javascript)

    def test_survey_activation_uses_normal_navigation(self):
        survey_wiring = self.javascript[self.javascript.index(
            "SURVEY = createSurvey") : self.javascript.index(
            "HISTORY = createHistoryPanel")]
        self.assertIn("if (index >= 0) go(index);", survey_wiring)
        self.assertNotIn("S.idx = index", survey_wiring)
        self.assertIn("currentIndex - 2", self.javascript)
        survey_escape = self.javascript.index(
            "e.key === 'Escape' && SURVEY && SURVEY.isOpen")
        photo_tool_escape = self.javascript.index(
            "(S.compareActive || PHOTO_TOOL_PANES.includes(S.activePane)) &&")
        self.assertLess(survey_escape, photo_tool_escape)

    def test_inspector_and_filmstrip_resizing_preserve_user_layout(self):
        loupe = (ROOT / "web" / "loupe.html").read_text()
        loupe_script = (ROOT / "web" / "loupe.js").read_text()
        self.assertIn("const paneScrollPositions = new Map()", self.javascript)
        self.assertIn("scrollIntoView({ block: 'nearest'", self.javascript)
        self.assertIn('id="filmstripResize"', self.html)
        self.assertIn("applyFilmstripHeight", self.javascript)
        self.assertIn("filmstripHeight: currentFilmstripHeight()",
                      self.javascript)
        self.assertIn("position: absolute; top: 0; left: 0", loupe)
        self.assertNotIn("opacity: 0; pointer-events: none", loupe)
        self.assertIn("hud.addEventListener('pointerenter'", loupe_script)
        self.assertIn("@media (max-width:1000px)", self.css)
        self.assertIn("grid-template-columns:auto minmax(140px,1fr) auto",
                      self.css)

    def test_wrapped_library_actions_grow_the_sticky_header(self):
        self.assertRegex(
            self.css,
            r"\.library-head \{[^}]*flex:0 0 auto;[^}]*flex-wrap:wrap;",
        )

    def test_catalog_dialogs_use_the_shared_visible_class(self):
        catalog_ui = (ROOT / "web" / "catalog-ui.js").read_text()
        self.assertNotIn("dialog.classList.toggle('open'", catalog_ui)
        self.assertEqual(
            catalog_ui.count("dialog.classList.toggle('on', visible)"), 4)

    def test_exif_updates_are_bound_to_the_current_photo(self):
        self.assertIn("if (cur()?.name !== name) return;", self.javascript)
        self.assertIn("S.exif = e;", self.javascript)
        self.assertIn("e.SourceWidth", self.javascript)
        self.assertIn("e.SourceHeight", self.javascript)
        self.assertIn("S.zoomMode === '100' && S.viewMode === 'detail'",
                      self.javascript)

    def test_crop_tool_and_zoom_survive_photo_navigation(self):
        self.assertIn("applyView();", self.javascript)
        self.assertIn("setCropMode(S.activePane === 'cropPane')", self.javascript)
        self.assertIn("if (S.activePane === 'cropPane') beginCropSession();", self.javascript)

    def test_folder_mode_keyword_tree_is_a_clean_empty_response(self):
        server_source = (ROOT / "server.py").read_text()
        self.assertIn(
            'self._json({"keywords": cat.keyword_tree() if cat else []})',
            server_source,
        )

    def test_slider_double_click_reset_and_library_filtering_contracts(self):
        self.assertIn("input.addEventListener('dblclick', resetFilmSlider);", self.javascript)
        self.assertIn("el.addEventListener('dblclick', resetGradeSlider);", self.javascript)
        self.assertIn("input.addEventListener('dblclick', resetLocal);", self.javascript)
        self.assertIn("if (f === 'edited')", self.javascript)
        self.assertIn("if (f === 'virtual')", self.javascript)
        self.assertIn("if (rf === 'unrated')", self.javascript)
        self.assertIn("if (s === 'name')", self.javascript)
        self.assertIn("$('search').addEventListener('search', refreshSearch);", self.javascript)
        markup = (ROOT / "web" / "index.html").read_text()
        for selector, value, label in (
                ("editFilter", "edited", "Edited"),
                ("ratingFilter", "unrated", "Unrated only")):
            select = re.search(rf'<select\b[^>]*\bid="{selector}"[^>]*>(.*?)</select>',
                               markup, re.DOTALL)
            self.assertIsNotNone(select, f"Missing {selector} control")
            option = re.search(rf'<option\b([^>]*\bvalue="{value}"[^>]*)>([^<]*)</option>',
                               select.group(1))
            self.assertIsNotNone(option, f"Missing {selector} option {value}")
            self.assertEqual(unescape(option.group(2)), label)
            template = re.search(r'data-i18n-text="([^"]+)"', option.group(1))
            self.assertIsNotNone(template, f"Missing translation template for {label}")
            self.assertEqual(json.loads(unescape(template.group(1))), {"0": label})

    def test_export_modal_uses_real_thumbnail_route_and_keyboard_focus(self):
        self.assertIn("thumbEl.style.backgroundImage = `url('/api/thumb?name=", self.javascript)
        self.assertIn("requestAnimationFrame(() => $('modalExWhich').focus());", self.javascript)
        self.assertIn("$('exportDialog').addEventListener('keydown'", self.javascript)
        self.assertNotIn('data-preset="original"',
                         (ROOT / "web" / "index.html").read_text())

    def test_undo_uses_the_photo_history_module(self):
        self.assertIn("import { createPhotoUndoHistory } from '/web/photo-undo.js';",
                      self.javascript)
        self.assertIn("photoUndo.push(state);", self.javascript)
        self.assertIn("Object.assign(S, photoUndo.activate(im.name));", self.javascript)

    def test_folder_mode_does_not_send_catalog_history_requests(self):
        history = (ROOT / "web" / "history-panel.js").read_text()
        self.assertIn("if (!isAvailable() || !name || !state) return;", history)
        self.assertIn("enabled: () => S.catalogEnabled", self.javascript)


class PlatformProcessTests(unittest.TestCase):
    def test_windows_engine_binary_uses_exe_suffix(self):
        with mock.patch.object(server, "IS_WINDOWS", True):
            self.assertEqual(
                server._platform_binary(Path("engine/lighttable-engine")),
                Path("engine/lighttable-engine.exe"),
            )

    def test_pipe_reader_returns_lines_without_select(self):
        reader = server.PipeLineReader(io.StringIO('{"ok":true}\n'))
        self.assertEqual(reader.readline(1), '{"ok":true}\n')

    def test_windows_folder_names_reject_reserved_and_invalid_values(self):
        with mock.patch.object(server, "IS_WINDOWS", True):
            for name in ("CON", "photo?", "trailing.", "control\x01"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    server.clean_folder_name(name)
            self.assertEqual(server.clean_folder_name("Scans 2026"), "Scans 2026")

    def test_windows_helper_processes_never_open_console_windows(self):
        """The shell starts the server without a console, so every console
        child (resident engine, one-shot exporter, render worker, git) would
        otherwise pop a command window on the desktop."""
        with mock.patch.object(server, "IS_WINDOWS", True):
            self.assertEqual(server.subprocess_flags(),
                             {"creationflags": 0x08000000})
        with mock.patch.object(server, "IS_WINDOWS", False):
            self.assertEqual(server.subprocess_flags(), {})
        source = (ROOT / "server.py").read_text()
        with mock.patch.object(server, "IS_WINDOWS", True), \
                mock.patch.object(server, "PipeLineReader"), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            client = server.RustEngineClient(Path("resident-worker"))
            client._start()
            self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        with mock.patch.object(server, "IS_WINDOWS", True), \
                mock.patch.object(server.subprocess, "run") as run, \
                mock.patch.object(server.subprocess, "Popen") as popen:
            server._run_export_process(["renderer"], {}, None)
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
            process = popen.return_value.__enter__.return_value
            process.communicate.return_value = ("", "")
            process.returncode = 0
            server._run_export_process(["renderer"], {}, mock.Mock())
            self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        self.assertIn("**_creation_flags()",
                      (ROOT / "render_cli.py").read_text())

    def test_shared_input_is_offered_on_windows_until_the_engine_refuses_it(self):
        server._SHARED_INPUT_DISABLED.clear()
        try:
            with mock.patch.object(server.os, "name", "nt"):
                self.assertTrue(server.shared_input_supported())
            server.note_shared_input_failure(PermissionError("denied"))
            self.assertTrue(server.shared_input_supported())
            server.note_shared_input_failure(RuntimeError(
                "shared RAW input is unavailable on this platform"))
            self.assertFalse(server.shared_input_supported())
        finally:
            server._SHARED_INPUT_DISABLED.clear()

    def test_full_render_latches_to_tiff_after_the_engine_refuses_shared_input(self):
        calls = []

        @contextmanager
        def shared(*_args, **_kwargs):
            calls.append("shared")
            yield {"input_shm": "wnsm_test", "input_shm_len": 64,
                   "input_cache_key": "source"}

        def render(request):
            if "input_shm" in request:
                raise RuntimeError(
                    "shared RAW input is unavailable on this platform")
            return {"width": 2, "height": 1}

        engine = mock.Mock()
        engine.render.side_effect = render
        engine.probe_input.return_value = False
        server._SHARED_INPUT_DISABLED.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "input.tif"
                source.write_bytes(b"cached input")
                with mock.patch.object(server, "is_raw", return_value=True), \
                        mock.patch.object(server, "raw_shared_input", shared), \
                        mock.patch.object(server, "BACKGROUND_ENGINE", engine), \
                        mock.patch.object(server, "file_key", return_value="source"), \
                        mock.patch.object(server, "tiff_for",
                                          return_value=source):
                    first = server._resident_render_full("frame.dng", {}, {})
                    second = server._resident_render_full("frame.dng", {}, {})
            self.assertEqual(first["input_transport"], "tiff-fallback")
            self.assertEqual(second["input_transport"], "tiff")
            self.assertEqual(calls, ["shared"])
        finally:
            server._SHARED_INPUT_DISABLED.clear()

    def test_provenance_skips_git_outside_a_checkout(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(server.subprocess, "run") as run:
            self.assertIsNone(server._git_revision(Path(directory)))
            run.assert_not_called()
        completed = mock.Mock(stdout="abc123\n")
        with mock.patch.object(server.subprocess, "run",
                               return_value=completed) as run:
            self.assertEqual(server._git_revision(ROOT), "abc123")
            run.assert_called_once()

    def test_portable_app_uses_manifest_revision_inside_an_enclosing_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").mkdir()
            bundle = checkout / "dist/LightTable"
            app = bundle / "Resources/LightTable"
            app.mkdir(parents=True)
            (bundle / "Python").mkdir()
            revision = "0123456789abcdef" * 2 + "01234567"
            (bundle / "build-manifest.json").write_text(
                json.dumps({"source_revision": revision}), encoding="utf-8-sig")
            with mock.patch.object(server.subprocess, "run") as run:
                self.assertEqual(server._git_revision(app), revision)
                run.assert_not_called()

    def test_portable_manifest_failures_never_fall_back_to_enclosing_git(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").write_text("gitdir: another-worktree")
            bundle = checkout / "dist/LightTable"
            app = bundle / "Resources/LightTable"
            app.mkdir(parents=True)
            (bundle / "Python").mkdir()
            manifest = bundle / "build-manifest.json"
            invalid = (None, b"{", b"\xff", b"null", b"[]", b"{}",
                       json.dumps({"source_revision": 123}).encode(),
                       json.dumps({"source_revision": "g" * 40}).encode(),
                       json.dumps({"source_revision": "a" * 39}).encode(),
                       json.dumps({"source_revision": "a" * 40 + "\n"}).encode())
            with mock.patch.object(server.subprocess, "run") as run:
                for contents in invalid:
                    with self.subTest(contents=contents):
                        if contents is not None:
                            manifest.write_bytes(contents)
                        self.assertIsNone(server._git_revision(app))
                run.assert_not_called()

    def test_packaged_vendor_never_runs_git_or_inherits_the_app_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").mkdir()
            bundle = checkout / "dist/LightTable"
            vendor = bundle / "Resources/LightTable/vendor/spektrafilm"
            (vendor / "src").mkdir(parents=True)
            (vendor / ".git").mkdir()
            (bundle / "Python").mkdir()
            (bundle / "build-manifest.json").write_text(
                json.dumps({"source_revision": "a" * 40}))
            with mock.patch.object(server.subprocess, "run") as run:
                self.assertIsNone(server._git_revision(vendor))
                self.assertIsNone(server._git_revision(vendor / "src"))
                run.assert_not_called()

    def test_resources_named_development_folder_without_bundled_python_still_uses_git(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").mkdir()
            app = checkout / "Resources/LightTable"
            app.mkdir(parents=True)
            with mock.patch.object(server.subprocess, "run", return_value=mock.Mock(stdout="dev-revision\n")) as run:
                self.assertEqual(server._git_revision(app), "dev-revision")
                (checkout / "Python").write_text("A file is not a packaged interpreter directory")
                self.assertEqual(server._git_revision(app), "dev-revision")
                self.assertEqual(run.call_count, 2)


class EngineSelectionContractTests(unittest.TestCase):
    def test_engine_choice_waits_for_the_library_capability_report(self):
        """The prefs response usually beats the library payload. Forcing the
        Python engine before Rust availability is known persisted the slow
        reference engine on machines that ship the GPU build."""
        source = (ROOT / "web" / "app.js").read_text()
        sync = re.search(
            r"function syncEngineForProfile\(\) \{(.*?)\n\}", source,
            flags=re.DOTALL)
        self.assertIsNotNone(sync)
        body = sync.group(1)
        guard = body.index("if (!S.engineCapabilityKnown) return;")
        self.assertLess(guard, body.index("$('engine').value = 'py'"))
        library_boot = source.index("S.rustAvailable = !!d.rust;")
        self.assertIn("S.engineCapabilityKnown = true;",
                      source[library_boot:library_boot + 200])
        state = (ROOT / "web" / "state.js").read_text()
        self.assertIn("engineCapabilityKnown: false", state)


if __name__ == "__main__":
    unittest.main()


class ExtensionListContractTests(unittest.TestCase):
    """Every shell consumes the shared media-format manifest."""

    def _raw_and_all(self) -> tuple[set[str], set[str]]:
        return (set(server.RAW_EXTS), set(server.EXTS))

    def test_swift_open_panel_matches_server(self):
        swift = (ROOT / "app" / "main.swift").read_text()
        self.assertIn('appendingPathComponent("media-formats.json")', swift)

    def test_windows_shell_matches_server(self):
        rust = (ROOT / "windows-shell" / "src" / "main.rs").read_text()
        self.assertIn('"../../media-formats.json"', rust)

    def test_web_raw_pattern_matches_server(self):
        web = (ROOT / "web" / "app.js").read_text()
        self.assertIn("const isRawInput = () => cur()?.raw === true", web)
        self.assertNotIn("RAW_FILE_RE", web)

    def test_benchmark_raw_suffixes_match_server(self):
        import media_formats
        self.assertEqual(set(media_formats.RAW_EXTENSIONS),
                         self._raw_and_all()[0])

    def test_ingest_extensions_match_server(self):
        try:
            import ingest_workflow
        except ImportError:
            self.skipTest("ingest_workflow not present")
        self.assertEqual(set(ingest_workflow.PHOTO_EXTENSIONS),
                         self._raw_and_all()[1])


class WindowsPackagingContractTests(unittest.TestCase):
    """Exercise the same module staging helper used by release and PR builds."""

    def test_every_root_runtime_module_is_packaged(self):
        import runpy
        stage = runpy.run_path(str(ROOT / "scripts/windows/stage-python-modules.py"))["stage_modules"]
        with tempfile.TemporaryDirectory() as temporary:
            staged = stage(ROOT, Path(temporary))
            packaged = {path.name for path in staged}
            expected = {path.name for path in ROOT.glob("*.py")}
            self.assertEqual(packaged, expected)
            self.assertTrue({"render_scheduling.py", "camera_profile.py"} <= packaged)

    def test_optional_ai_package_is_packaged(self):
        script = (ROOT / "scripts" / "windows" / "build-release.ps1").read_text()
        self.assertIn("film_lab_ai", script)
