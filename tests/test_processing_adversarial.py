# SPDX-License-Identifier: GPL-3.0-only
"""Semantic processing invariants outside the fixed renderer parity recipes."""
import base64
import unittest

import numpy as np

import edits
import grade


class MaskSemanticTests(unittest.TestCase):
    def test_flat_mask_inversion_is_applied_once_when_migrated(self):
        for component in (
            {"type": "radial", "center": [.5, .5], "radius": .3, "feather": .4},
            {"type": "linear", "start": [.1, .4], "end": [.8, .6]},
            {"type": "brush", "strokes": [
                {"points": [[.3, .4], [.7, .6]], "size": .2}]},
            {"type": "subject", "bitmap": {
                "width": 2, "height": 2,
                "data": base64.b64encode(bytes([0, 64, 128, 255])).decode()}},
        ):
            with self.subTest(kind=component["type"]):
                normal = edits.raster_mask(component, 33, 47)
                inverted = edits.raster_mask({**component, "invert": True}, 33, 47)
                np.testing.assert_allclose(inverted, 1 - normal, atol=1e-7)
                canonical = {"components": [component], "invert": True}
                np.testing.assert_array_equal(
                    inverted, edits.raster_mask(canonical, 33, 47))
                cleaned = edits.clean_masks([{**component, "invert": True}])
                self.assertEqual(cleaned, edits.clean_masks(cleaned))

    def test_explicit_component_and_mask_inversion_remain_independent(self):
        component = {"type": "radial", "radius": .3, "feather": .5}
        normal = edits.raster_mask(component, 33, 47)
        twice = edits.raster_mask({
            "components": [{**component, "invert": True}], "invert": True,
        }, 33, 47)
        np.testing.assert_allclose(twice, normal, atol=1e-7)

    def test_local_detail_matches_full_image_grade_then_mask(self):
        # A mask limits which pixels change, not which source neighbors the
        # detail kernel can see. Compare the optimization with this full-frame
        # reference at interior edges, image edges and single-pixel masks.
        rng = np.random.default_rng(20260908)
        recipes = ({"texture": 1}, {"clarity": 1},
                   {"exposure": .3, "texture": -.8, "clarity": .4})
        for height, width in ((32, 48), (47, 33), (1, 17), (19, 1)):
            image = rng.uniform(.1, .9, (height, width, 3)).astype(np.float32)
            for bounds in ((height // 4, max(1, 3 * height // 4),
                            width // 4, max(1, 3 * width // 4)),
                           (0, max(1, height // 2), 0, max(1, width // 2)),
                           (height // 2, height // 2 + 1, width // 2, width // 2 + 1)):
                bitmap = np.zeros(image.shape[:2], dtype=np.uint8)
                y0, y1, x0, x1 = bounds
                bitmap[y0:y1, x0:x1] = 255
                for recipe in recipes:
                    with self.subTest(shape=image.shape, bounds=bounds, grade=recipe):
                        mask = {"type": "subject", "bitmap": {
                            "width": width, "height": height,
                            "data": base64.b64encode(bitmap).decode()}, "grade": recipe}
                        weight = (bitmap.astype(np.float32) / 255)[..., None]
                        expected = image * (1 - weight) + grade.apply(image, recipe) * weight
                        actual = edits.apply_masks(image, [mask])
                        np.testing.assert_allclose(actual, expected, atol=1e-6)
                        np.testing.assert_array_equal(actual[bitmap == 0], image[bitmap == 0])


class DiscreteGeometryTests(unittest.TestCase):
    def test_flips_preserve_every_source_pixel_at_uneven_dimensions(self):
        rng = np.random.default_rng(20260908)
        for height, width in ((395, 165), (700, 965), (1, 17), (19, 1)):
            image = rng.uniform(.1, .9, (height, width, 3)).astype(np.float32)
            original = image.copy()
            for horizontal, vertical in ((True, False), (False, True), (True, True)):
                with self.subTest(shape=image.shape, horizontal=horizontal, vertical=vertical):
                    expected = image[::(-1 if vertical else 1), ::(-1 if horizontal else 1)]
                    actual = edits.apply_manual_optics(image, {
                        "flipHorizontal": horizontal, "flipVertical": vertical})
                    np.testing.assert_array_equal(actual, expected)
                    np.testing.assert_array_equal(image, original)

    def test_optical_vignette_does_not_create_black_edges_or_modify_source(self):
        image = np.ones((395, 165, 3), dtype=np.float32)
        for horizontal in (False, True):
            with self.subTest(horizontal=horizontal):
                actual = edits.apply_manual_optics(image, {
                    "flipHorizontal": horizontal, "vignette": -.1})
                # Its bounded radial multiplier is at least 1 - .1*.8*1.5.
                self.assertGreaterEqual(float(actual.min()), .879999)
                np.testing.assert_array_equal(image, np.ones_like(image))


if __name__ == "__main__":
    unittest.main()
