import unittest
from unittest import mock
import base64
import io

import numpy as np

import edits


class LocalEditTests(unittest.TestCase):
    def setUp(self):
        y, x = np.mgrid[0:80, 0:120].astype(np.float32)
        self.image = np.stack((x / 119, y / 79, np.full_like(x, 0.35)), axis=2)

    def test_mask_schema_is_bounded_and_normalized(self):
        raw = [{
            "id": f"m-{index}", "type": "radial", "opacity": 7,
            "lumaLow": 0.9, "lumaHigh": 0.2,
            "grade": {"exposure": 99},
        } for index in range(edits.MAX_MASKS + 1)]
        masks = edits.clean_masks(raw)
        self.assertEqual(len(masks), edits.MAX_MASKS)
        self.assertEqual(masks[0]["opacity"], 1)
        self.assertLessEqual(masks[0]["lumaLow"], masks[0]["lumaHigh"])
        self.assertEqual(masks[0]["grade"]["exposure"], 3)

    def test_mask_points_are_bounded_across_the_image(self):
        strokes = [{"points": [[0.5, 0.5]] * edits.MAX_POINTS}
                   for _ in range(edits.MAX_STROKES)]
        masks = edits.clean_masks([
            {"type": "brush", "strokes": strokes}
            for _ in range(edits.MAX_MASKS)
        ])
        total = sum(len(stroke["points"])
                    for mask in masks
                    for component in mask["components"]
                    for stroke in component.get("strokes", []))
        self.assertEqual(total, edits.MAX_TOTAL_MASK_POINTS)

    def test_radial_and_linear_masks_have_expected_direction(self):
        radial = edits.raster_mask({
            "type": "radial", "center": [0.5, 0.5], "radius": 0.25,
            "feather": 0.5,
        }, 80, 120)
        self.assertGreater(radial[40, 60], 0.95)
        self.assertLess(radial[0, 0], 0.01)
        linear = edits.raster_mask({
            "type": "linear", "start": [0.2, 0.5], "end": [0.8, 0.5],
        }, 80, 120)
        self.assertLess(linear[40, 10], 0.01)
        self.assertGreater(linear[40, 110], 0.99)

    def test_local_adjustment_changes_only_selected_region(self):
        out = edits.apply_masks(self.image, [{
            "type": "radial", "center": [0.5, 0.5], "radius": 0.18,
            "feather": 0.2, "grade": {"exposure": 1.0},
        }])
        centre_delta = np.abs(out[40, 60] - self.image[40, 60]).mean()
        corner_delta = np.abs(out[0, 0] - self.image[0, 0]).mean()
        self.assertGreater(centre_delta, 0.05)
        self.assertLess(corner_delta, 1e-6)

    def test_color_range_uses_point_color_weight_and_composes_with_luminance(self):
        colors = np.array([[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                            [0.25, 0.0, 0.0]]], dtype=np.float32)
        weight = edits._color_weight(colors, 0, 24, 1)
        hue, saturation, _ = edits.grade._rgb_to_hsv(colors)
        difference = np.abs(((hue + 180.0) % 360.0) - 180.0)
        expected = (1.0 - edits._smoothstep(24 * 0.45, 24, difference))
        expected *= saturation
        np.testing.assert_allclose(weight, expected, atol=1e-6)
        out = edits.apply_masks(colors, [{
            "type": "subject", "bitmap": {
                "width": 3, "height": 1,
                "data": base64.b64encode(bytes([255, 255, 255])).decode(),
            },
            "colorHue": 0, "colorRange": 24, "colorAmount": 1,
            "lumaLow": 0.15, "lumaHigh": 1,
            "grade": {"exposure": -1},
        }])
        self.assertGreater(float(np.abs(out[0, 0] - colors[0, 0]).mean()), 0.05)
        np.testing.assert_allclose(out[0, 1], colors[0, 1], atol=1e-6)
        np.testing.assert_allclose(out[0, 2], colors[0, 2], atol=1e-6)

    def test_png_bitmap_and_local_detail_affect_only_masked_region(self):
        bitmap = np.zeros((32, 32), dtype=np.uint8)
        bitmap[:, :16] = 255
        buffer = io.BytesIO()
        from PIL import Image
        Image.fromarray(bitmap, "L").save(buffer, "PNG")
        mask = {
            "type": "face-skin",
            "bitmap": {"width": 32, "height": 32, "encoding": "png",
                       "data": base64.b64encode(buffer.getvalue()).decode()},
            "grade": {"texture": -0.7, "clarity": -0.4},
        }
        output = edits.apply_masks(self.image, [mask])
        self.assertGreater(float(np.abs(output[:, :50] - self.image[:, :50]).mean()),
                           1e-5)
        np.testing.assert_allclose(output[:, 70:], self.image[:, 70:], atol=1e-6)

    def test_depth_bitmap_remains_continuous_and_rangeable(self):
        values = bytes([0, 64, 128, 192, 255])
        mask = edits.raster_mask({
            "type": "depth", "depthLow": 0.45, "depthHigh": 0.8,
            "bitmap": {"width": 5, "height": 1,
                       "data": base64.b64encode(values).decode()},
        }, 1, 5)
        self.assertLess(float(mask[0, 1]), 0.05)
        self.assertGreater(float(mask[0, 2]), 0.7)
        self.assertGreater(float(mask[0, 3]), 0.7)
        self.assertLess(float(mask[0, 4]), 0.05)

    def test_mask_grade_work_is_bounded_to_the_active_region(self):
        with mock.patch.object(edits.grade, "apply",
                               wraps=edits.grade.apply) as apply_grade:
            edits.apply_masks(self.image, [{
                "type": "radial", "center": [0.5, 0.5], "radius": 0.1,
                "feather": 0.1, "grade": {"exposure": 1.0},
            }])
        graded = apply_grade.call_args.args[0]
        self.assertLess(graded.shape[0], self.image.shape[0] // 2)
        self.assertLess(graded.shape[1], self.image.shape[1] // 2)

    def test_mask_components_add_subtract_and_intersect(self):
        mask = edits.raster_mask({
            "components": [
                {"type": "radial", "center": [0.35, 0.5], "radius": 0.3,
                 "feather": 0, "combine": "add"},
                {"type": "radial", "center": [0.65, 0.5], "radius": 0.3,
                 "feather": 0, "combine": "add"},
                {"type": "radial", "center": [0.5, 0.5], "radius": 0.08,
                 "feather": 0, "combine": "subtract"},
            ],
        }, 100, 100)
        self.assertGreater(mask[50, 30], 0.9)
        self.assertGreater(mask[50, 70], 0.9)
        self.assertLess(mask[50, 50], 0.1)

        intersected = edits.raster_mask({
            "components": [
                {"type": "radial", "center": [0.4, 0.5], "radius": 0.35,
                 "feather": 0},
                {"type": "linear", "start": [0.5, 0], "end": [0.75, 0],
                 "combine": "intersect"},
            ],
        }, 100, 100)
        self.assertLess(intersected[50, 40], 0.1)
        self.assertGreater(intersected[50, 62], 0.4)

    def test_browser_brush_refinements_migrate_into_export_components(self):
        raw = {
            "type": "radial", "center": [0.3, 0.5], "radius": 0.12,
            "feather": 0,
            "addStrokes": [{"size": 0.12, "feather": 0, "flow": 1,
                            "points": [[0.8, 0.5]]}],
            "subtractStrokes": [{"size": 0.12, "feather": 0, "flow": 1,
                                 "points": [[0.3, 0.5]]}],
        }
        cleaned = edits.clean_masks([raw])[0]
        self.assertEqual(
            [component["combine"] for component in cleaned["components"]],
            ["add", "add", "subtract"],
        )
        mask = edits.raster_mask(raw, 100, 100)
        self.assertLess(mask[50, 30], 0.1)
        self.assertGreater(mask[50, 79], 0.9)

    def test_clone_spot_moves_source_pixels_and_stays_finite(self):
        image = np.zeros((80, 120, 3), dtype=np.float32)
        image[32:48, 84:100] = 1
        out = edits.apply_heals(image, [{
            "mode": "clone", "target": [0.25, 0.5], "source": [0.77, 0.5],
            "radius": 0.1, "feather": 0, "opacity": 1,
        }])
        self.assertGreater(float(out[40, 30].mean()), 0.9)
        self.assertTrue(np.isfinite(out).all())

    def test_automatic_remove_inpaints_from_the_boundary(self):
        image = self.image.copy()
        image[34:47, 54:67] = 0
        out = edits.apply_heals(image, [{
            "mode": "remove", "target": [0.5, 0.5], "radius": 0.11,
            "feather": 0.25, "opacity": 1,
        }])
        original_error = np.abs(image[40, 60] - self.image[40, 60]).mean()
        repaired_error = np.abs(out[40, 60] - self.image[40, 60]).mean()
        self.assertLess(repaired_error, original_error * 0.35)

    def test_manual_optics_identity_and_perspective_transform(self):
        identity = edits.apply_manual_optics(self.image, {})
        np.testing.assert_array_equal(identity, self.image)
        transformed = edits.apply_manual_optics(
            self.image, {"vertical": 0.3, "horizontal": -0.2, "scale": 1.15})
        self.assertEqual(transformed.shape, self.image.shape)
        self.assertTrue(np.isfinite(transformed).all())
        self.assertGreater(float(np.abs(transformed - self.image).mean()), 0.01)

    def test_manual_optics_flips_match_pixel_geometry(self):
        horizontal = edits.apply_manual_optics(
            self.image, {"flipHorizontal": True})
        vertical = edits.apply_manual_optics(
            self.image, {"flipVertical": True})
        np.testing.assert_allclose(horizontal, self.image[:, ::-1], atol=1e-6)
        np.testing.assert_allclose(vertical, self.image[::-1], atol=1e-6)
        self.assertFalse(edits.optics_is_identity({"flipHorizontal": True}))

    def test_unambiguous_successor_resolves_compatible_profile(self):
        class Camera:
            maker = "Camera Maker"
            model = "Fixed Series V"
            crop_factor = 1.5
            score = 100

        class Lens:
            maker = "Lens Maker"
            model = "Fixed Lens"
            min_focal = max_focal = 23
            score = 100
            calib_distortion = [1]
            calib_vignetting = [1]
            calib_tca = [1]

        class Database:
            def find_cameras(self, **options):
                return [Camera()] if options.get("loose_search") else []

            def find_lenses(self, *_args, **_options):
                return [Lens()]

        with mock.patch.object(edits, "_lens_database", return_value=Database()):
            profile = edits.lens_profile_for({
                "Make": "Camera Maker", "Model": "Fixed Series VI",
                "FocalLength": "23 mm", "FNumber": "4.0",
            })
        self.assertIsNotNone(profile)
        self.assertTrue(profile["aliasedCamera"])
        self.assertTrue(profile["hasDistortion"])


if __name__ == "__main__":
    unittest.main()
