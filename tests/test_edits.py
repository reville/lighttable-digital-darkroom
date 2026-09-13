# SPDX-License-Identifier: GPL-3.0-only
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
        self.assertEqual(masks[0]["grade"]["exposure"], 5.0)

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

    def test_banded_manual_optics_match_the_single_pass_render(self):
        import math
        from scipy.ndimage import map_coordinates

        def single_pass(image, optics):
            optics = edits.clean_optics(optics)
            height, width = image.shape[:2]
            yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
            half = max(min(width, height) / 2.0, 1.0)
            nx = (xx - (width - 1) / 2.0) / half / optics["scale"]
            ny = (yy - (height - 1) / 2.0) / half / optics["scale"]
            if optics["flipHorizontal"]:
                nx = -nx
            if optics["flipVertical"]:
                ny = -ny
            rotation = math.radians(optics["rotate"])
            cosine, sine = math.cos(rotation), math.sin(rotation)
            rx = cosine * nx - sine * ny
            ry = sine * nx + cosine * ny
            px = rx * (1.0 + optics["vertical"] * 0.45 * ry)
            py = ry * (1.0 + optics["horizontal"] * 0.45 * rx)
            radius2 = px * px + py * py
            factor = 1.0 + optics["distortion"] * 0.18 * radius2
            if any((optics["distortion"], optics["vertical"], optics["horizontal"],
                    rotation, optics["scale"] - 1.0)):
                warped = np.stack([map_coordinates(
                    image[..., channel],
                    [py * factor * half + (height - 1) / 2.0,
                     px * factor * half + (width - 1) / 2.0],
                    order=1, mode="constant", cval=0.0) for channel in range(3)], axis=2)
            else:
                warped = image[::(-1 if optics["flipVertical"] else 1),
                               ::(-1 if optics["flipHorizontal"] else 1)].copy()
            if optics["vignette"]:
                warped *= (1.0 + optics["vignette"] * 0.8
                           * np.clip(radius2 / 2.0, 0.0, 1.5))[..., None]
            return np.clip(warped, 0.0, 1.0).astype(np.float32)

        rng = np.random.default_rng(12)
        rgb = rng.random((331, 257, 3), dtype=np.float32)
        rgba = rng.random((97, 64, 4), dtype=np.float32)
        for image, optics in (
                (rgb, {"distortion": 0.4, "vertical": 0.3, "horizontal": -0.2,
                       "rotate": 7.5, "scale": 1.1, "vignette": -0.4}),
                (rgb, {"vignette": 0.6}),
                (rgb, {"flipHorizontal": True, "vignette": 0.3}),
                (rgba, {"flipVertical": True, "flipHorizontal": True}),
                (rgba, {"rotate": -3.0})):
            expected = single_pass(image, optics)
            rendered = edits.apply_manual_optics(image, optics)
            self.assertEqual(rendered.shape, expected.shape, optics)
            self.assertEqual(rendered.tobytes(), expected.tobytes(), optics)

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


def _full_selection(values):
    return {"type": "subject", "opacity": 1.0, "grade": values,
            "bitmap": {"width": 1, "height": 1, "data": base64.b64encode(b"\xff").decode()}}


class RetouchAndLocalEffectTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.clean = np.clip(0.45 + rng.normal(0, 0.02, (300, 400, 3)), 0, 1).astype(np.float32)

    def test_new_retouch_fields_are_bounded_and_kept_per_mode(self):
        dust, stroke, clone = edits.clean_heals([
            {"mode": "dust", "sensitivity": 9, "size": -1, "points": [[0, 0], [1, 1]]},
            {"mode": "remove", "fill": "texture", "points": [[0.1, 0.2], [2, -1], "bad"]},
            {"mode": "clone", "fill": "patch", "points": [[0, 0], [1, 1]]},
        ])
        self.assertEqual((dust["sensitivity"], dust["size"]), (1.0, edits.DUST_SIZE_RANGE[0]))
        self.assertNotIn("points", dust)
        self.assertEqual(stroke["fill"], "smooth")
        self.assertEqual(stroke["points"], [[0.1, 0.2], [1.0, 0.0]])
        self.assertNotIn("fill", clone)
        self.assertNotIn("points", clone)
        legacy = edits.clean_heals([{"mode": "remove"}])[0]
        self.assertEqual(legacy["fill"], "smooth")
        self.assertNotIn("points", legacy)

    def test_dust_removal_repairs_marks_and_leaves_clean_grain(self):
        dusty = self.clean.copy()
        rng = np.random.default_rng(4)
        for y, x in rng.integers(20, 280, (25, 2)):
            dusty[y - 2:y + 3, x - 2:x + 3] = 0.98
        dusty[60:240, 200:202] = 0.05
        spot = {"mode": "dust", "sensitivity": 0.5, "size": 0.01}
        repaired = edits.apply_heals(dusty, [spot])
        before = float(np.abs(dusty - self.clean).mean())
        after = float(np.abs(repaired - self.clean).mean())
        self.assertLess(after, before * 0.2)
        defects, _ = edits.dust_defects(self.clean, 0.5, 0.01)
        self.assertEqual(int(defects.sum()), 0)
        np.testing.assert_allclose(edits.apply_heals(self.clean, [spot]), self.clean, atol=1e-6)

    def test_dust_sensitivity_and_size_select_more_marks(self):
        dusty = self.clean.copy()
        dusty[100:104, 100:104] = 0.7
        dusty[150:160, 250:260] = 0.95
        counts = [int(edits.dust_defects(dusty, value, 0.01)[0].sum()) for value in (0.0, 1.0)]
        self.assertLessEqual(counts[0], counts[1])
        sizes = [int(edits.dust_defects(dusty, 0.5, value)[0].sum()) for value in (0.004, 0.02)]
        self.assertLess(sizes[0], sizes[1])

    def test_stroke_remove_covers_the_whole_path_with_either_fill(self):
        damaged = self.clean.copy()
        damaged[140:146, 40:360] = 0.0
        stroke = {"id": "stroke", "mode": "remove", "radius": 0.02, "feather": 0.2,
                  "points": [[0.1, 0.475], [0.9, 0.475]]}
        region = (slice(140, 146), slice(50, 350))
        for fill in ("patch", "smooth"):
            repaired = edits.apply_heals(damaged, [dict(stroke, fill=fill)])
            error = float(np.abs(repaired[region] - self.clean[region]).mean())
            self.assertLess(error, 0.05, fill)
        untouched = (slice(0, 100), slice(0, 400))
        repaired = edits.apply_heals(damaged, [dict(stroke, fill="patch")])
        np.testing.assert_array_equal(repaired[untouched], damaged[untouched])

    def test_patch_fill_is_seeded_by_the_correction_identity(self):
        damaged = self.clean.copy()
        damaged[100:130, 150:200] = 1.0
        spot = {"id": "a", "mode": "remove", "fill": "patch", "target": [0.44, 0.38],
                "radius": 0.08, "feather": 0.3}
        first = edits.apply_heals(damaged, [spot])
        second = edits.apply_heals(damaged, [spot])
        np.testing.assert_array_equal(first, second)
        self.assertLess(float(first[115, 175].mean()), 0.7)

    def test_lens_blur_softens_only_the_selection_and_grows_with_amount(self):
        checker = np.zeros((120, 160, 3), dtype=np.float32)
        checker[(np.indices((120, 160)).sum(axis=0) % 2) == 0] = 1.0
        half = {"type": "radial", "center": [0.25, 0.5], "radius": 0.3, "feather": 0.0,
                "grade": {"blur": 0.5}}
        blurred = edits.apply_masks(checker, [half])
        detail = lambda image: float(np.abs(np.diff(image[..., 0], axis=1)).mean())
        self.assertLess(detail(blurred[40:80, 20:60]), 0.2)
        np.testing.assert_array_equal(blurred[:, 130:], checker[:, 130:])
        readings = [detail(edits.apply_masks(checker, [_full_selection({"blur": value})]))
                    for value in (0.05, 0.3, 1.0)]
        self.assertGreater(readings[0], readings[1])
        self.assertGreaterEqual(readings[1], readings[2])
        self.assertEqual(edits.clean_local_grade({"blur": 4})["blur"], 1.0)

    def test_luminosity_curve_matches_rgb_curve_luminance_without_its_colour_shift(self):
        ramp = np.tile(np.linspace(0, 1, 200, dtype=np.float32)[None, :, None], (20, 1, 3))
        ramp[..., 0] *= 0.4
        curve = (np.linspace(0, 1, 256) ** 0.5).tolist()
        rgb = edits.apply_masks(ramp, [_full_selection({"curveL": curve})])
        luminosity = edits.apply_masks(ramp, [_full_selection(
            {"curveL": curve, "curveLuminosity": True})])
        import grade
        middle = (slice(None), slice(30, 170))
        np.testing.assert_allclose((luminosity @ grade.LUMA)[middle],
                                   (rgb @ grade.LUMA)[middle], atol=2e-3)
        source_spread = (ramp[..., 1] - ramp[..., 0])[middle]
        self.assertLess(float(np.abs((luminosity[..., 1] - luminosity[..., 0])[middle]
                                     - source_spread).mean()),
                        float(np.abs((rgb[..., 1] - rgb[..., 0])[middle] - source_spread).mean()))
        self.assertTrue(edits.clean_local_grade({"curveLuminosity": True})["curveLuminosity"])
        self.assertNotIn("curveLuminosity", edits.clean_local_grade({"curveLuminosity": "yes"}))


if __name__ == "__main__":
    unittest.main()
