from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import edits
import server


class NativeCorrectedPreviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cache = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.cache / "render").mkdir()
        (self.cache / "edit").mkdir()
        self.stack.enter_context(mock.patch.object(server, "CACHE", self.cache))
        self.stack.enter_context(mock.patch.object(server, "file_key", return_value="source"))
        self.stack.enter_context(mock.patch.object(server, "exif_for", return_value={}))
        self.stack.enter_context(mock.patch.object(server.edits, "lens_profile_for", return_value=None))
        self.stack.enter_context(mock.patch.object(server, "prune_render_cache_throttled"))
        self.pixels = (np.arange(48 * 32 * 3).reshape(32, 48, 3) % 256).astype(np.uint8)
        self.base_key = "a" * 32
        server.write_native_surface(self.cache / "render" / f"{self.base_key}.rgba", self.pixels)
        self.result = {
            "key": self.base_key, "cached": True, "refining": True,
            "native": {"url": f"/api/render/native?key={self.base_key}"},
        }
        self.optics = {"profileEnabled": True, "flipHorizontal": True}

    def render(self, optics=None, heals=None):
        return server.apply_preview_edits(
            self.result, "frame.jpg", 48, {},
            self.optics if optics is None else optics, heals, native=True)

    def test_corrected_native_pixels_match_reference_without_jpeg_roundtrip(self):
        expected = edits.apply_base(self.pixels.astype(np.float32) / 255.0,
                                    self.optics, [], None)
        with mock.patch.object(server, "jpeg_bytes", side_effect=AssertionError("JPEG on critical path")):
            result = self.render()
        self.assertTrue(result["baseEditsBaked"])
        self.assertTrue(result["refining"])
        self.assertFalse(result["edit_cached"])
        self.assertNotIn("img", result)
        self.assertNotEqual(result["key"], self.base_key)
        rgba, _ = server.read_native_surface(self.cache / "render" / f"{result['key']}.rgba")
        np.testing.assert_array_equal(rgba[..., :3], (np.clip(expected, 0, 1) * 255 + 0.5).astype(np.uint8))
        self.assertIn(result["key"], result["helper"])
        helper = server.browser_helper_path(result["key"])
        server.ensure_jpeg_surface(helper, self.cache / "render" / f"{result['key']}.rgba", 24)
        with Image.open(helper) as image:
            self.assertEqual(image.size, (24, 16))

    def test_cached_corrected_base_skips_corrections_and_invalidates_on_edit(self):
        first = self.render()
        with mock.patch.object(server.edits, "apply_base", side_effect=AssertionError("cache missed")):
            cached = self.render()
        self.assertEqual(cached["key"], first["key"])
        self.assertTrue(cached["edit_cached"])
        changed = self.render(dict(self.optics, flipVertical=True))
        self.assertNotEqual(changed["key"], first["key"])

    def test_truncated_corrected_surface_is_regenerated(self):
        first = self.render()
        surface = self.cache / "render" / f"{first['key']}.rgba"
        surface.write_bytes(surface.read_bytes()[:20])
        regenerated = self.render()
        self.assertFalse(regenerated["edit_cached"])
        server.read_native_surface(surface)

    def test_live_supported_edits_stay_unbaked(self):
        response = self.render({"distortion": 0.2}, [{"enabled": False}] * 16)
        self.assertFalse(response["baseEditsBaked"])
        self.assertEqual(response["native"], self.result["native"])
        self.assertFalse(server.native_base_edits_required({}, [{"enabled": False}] * 17))
        self.assertTrue(server.native_base_edits_required({}, [{}] * 17))

    def test_each_retouch_mode_uses_ordered_export_pixels(self):
        for mode in ("clone", "heal", "remove"):
            with self.subTest(mode=mode):
                heals = [{"mode": mode, "target": [.35, .5], "source": [.7, .5],
                          "radius": .14, "feather": .4}]
                result = self.render({}, heals)
                self.assertTrue(result["baseEditsBaked"])
                rgba, _ = server.read_native_surface(self.cache / "render" / f"{result['key']}.rgba")
                expected = edits.apply_base(self.pixels.astype(np.float32) / 255, {}, heals)
                np.testing.assert_array_equal(rgba[..., :3], np.rint(expected * 255).astype(np.uint8))

    def test_local_tones_and_curves_use_exact_ordered_preview_pixels(self):
        import grade
        from processing_edit_cases import full_mask
        for values in ({"whites": .4}, {"blacks": -.3},
                       {"curveL": (np.linspace(0, 1, 256) ** .7).tolist()}):
            with self.subTest(controls=list(values)):
                masks = [full_mask({"exposure": -.25}), full_mask(values)]
                result = server.apply_preview_edits(self.result, "frame.jpg", 48, {},
                    {}, [], native=True, grade_values={"exposure": .3}, masks=masks)
                self.assertTrue(result["gradeEditsBaked"])
                rgba, _ = server.read_native_surface(self.cache / "render" / f"{result['key']}.rgba")
                expected = edits.apply_masks(grade.apply(self.pixels.astype(np.float32) / 255,
                    {"exposure": .3}), masks)
                np.testing.assert_array_equal(rgba[..., :3], np.rint(expected * 255).astype(np.uint8))

    def test_local_detail_bakes_grade_and_invalidates_on_prior_mask_or_grade(self):
        from processing_edit_cases import full_mask
        masks = [full_mask({"exposure": 1}), full_mask({"texture": 1})]
        def render(values, local_masks=masks):
            return server.apply_preview_edits(self.result, "frame.jpg", 48, {},
                native=True, grade_values=values, masks=local_masks)
        first = render({"exposure": .4})
        self.assertTrue(first["gradeEditsBaked"])
        with mock.patch.object(server.edits, "apply_masks", side_effect=AssertionError("cache missed")):
            self.assertEqual(render({"exposure": .4})["key"], first["key"])
        self.assertNotEqual(render({"exposure": .7})["key"], first["key"])
        self.assertNotEqual(render({"exposure": .4}, masks[::-1])["key"], first["key"])
        self.assertFalse(render({}, [full_mask({"texture": 0})]).get("gradeEditsBaked", False))

    def test_more_than_sixteen_heals_use_the_complete_reference_result(self):
        heals = [{"mode": "clone", "target": [0.7, 0.5],
                  "source": [0.2, 0.5], "radius": 0.2}] * 17
        expected = edits.apply_base(self.pixels.astype(np.float32) / 255.0,
                                    {}, heals, None)
        result = self.render({}, heals)
        rgba, _ = server.read_native_surface(self.cache / "render" / f"{result['key']}.rgba")
        self.assertTrue(result["baseEditsBaked"])
        np.testing.assert_array_equal(rgba[..., :3], (np.clip(expected, 0, 1) * 255 + 0.5).astype(np.uint8))
        self.assertFalse(np.array_equal(rgba[..., :3], self.pixels))

    def test_develop_image_base_is_baked_for_native_and_lossless_browser(self):
        encoded = io.BytesIO()
        Image.fromarray(self.pixels).save(encoded, "JPEG")
        with mock.patch.object(server, "_preview_source_bytes", return_value=encoded.getvalue()):
            self.result = {"key": "neutral-base", "img": "/api/orig?name=frame.jpg", "refining": False}
            native = self.render()
            browser = server.apply_preview_edits(self.result, "frame.jpg", 48, {}, self.optics, [])
        self.assertTrue(native["baseEditsBaked"])
        self.assertEqual(native["native"]["format"], "rgba8")
        self.assertEqual(browser["key"], native["key"])
        self.assertTrue(browser["img"].startswith("/api/render/png?key="))

    def test_cancelled_render_does_not_decode_or_correct(self):
        self.result = {"cancelled": True}
        with mock.patch.object(server.edits, "apply_base", side_effect=AssertionError("stale work")):
            self.assertEqual(self.render(), {"cancelled": True})


if __name__ == "__main__":
    unittest.main()
