"""Auto-straighten and guided upright.

Every frame here is drawn with numpy so the suite carries no fixture files.
The synthetic tolerances the roadmap asks for are stated on each test: a
tilted horizon is recovered within 0.2 degrees and a known keystone within
0.05 of its ``vertical`` value.
"""
import math
import unittest

import numpy as np

import edits
import geometry_auto


HEIGHT, WIDTH = 480, 720


def _pixels(points):
    """Normalised -1..1 centred coordinates -> pixels, as edits.py measures."""
    half = max(min(WIDTH, HEIGHT) / 2.0, 1.0)
    return [(float(x) * half + (WIDTH - 1) / 2.0,
             float(y) * half + (HEIGHT - 1) / 2.0) for x, y in points]


def _render(lines, thickness=2.0):
    """Draw normalised segments as pale strokes on a dark frame."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float64)
    canvas = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
    for start, end in lines:
        (x0, y0), (x1, y1) = _pixels((start, end))
        dx, dy = x1 - x0, y1 - y0
        squared = dx * dx + dy * dy or 1.0
        along = np.clip(((xx - x0) * dx + (yy - y0) * dy) / squared, 0.0, 1.0)
        distance = np.hypot(xx - (x0 + along * dx), yy - (y0 + along * dy))
        canvas = np.maximum(canvas, np.clip(1.0 - distance / thickness, 0, 1))
    return np.repeat(canvas[..., None], 3, axis=2).astype(np.float32)


def _grid(optics=None, tilt=0.0):
    """A grid of straight lines, optionally pushed through the optics model.

    ``tilt`` rotates the content directly (degrees, positive running down to
    the right); ``optics`` warps it with the same chain that
    ``edits.apply_manual_optics`` samples through, so applying that very
    optics patch is what straightens the frame again.
    """
    cosine, sine = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
    lines = []
    for offset in (-0.7, -0.35, 0.0, 0.35, 0.7):
        for start, end in (((-0.85, offset), (0.85, offset)),
                           ((offset, -0.85), (offset, 0.85))):
            points = [(cosine * x - sine * y, sine * x + cosine * y)
                      for x, y in (start, end)]
            if optics:
                mapped = geometry_auto._source_from_output(points, optics)
                points = [tuple(mapped[0]), tuple(mapped[1])]
            lines.append(tuple(points))
    return _render(lines)


def _cluster_angles(image, axis):
    """(length-weighted mean, length-weighted std) of one cluster, degrees."""
    segments = geometry_auto._as_array(geometry_auto.detect_lines(image))
    horizontal, vertical = geometry_auto._cluster(segments)
    chosen = horizontal if axis == "horizontal" else vertical
    if len(chosen) < 2:
        return None
    angles = geometry_auto._axis_angles(chosen, axis)
    weights = geometry_auto._lengths(chosen)
    mean = float((angles * weights).sum() / weights.sum())
    spread = math.sqrt(geometry_auto._weighted_variance(angles, weights))
    return (mean, spread)


class DetectionTests(unittest.TestCase):
    def test_detect_lines_returns_normalised_segments(self):
        lines = geometry_auto.detect_lines(_grid(tilt=4.0))
        self.assertGreater(len(lines), 4)
        limit_x = WIDTH / min(WIDTH, HEIGHT)
        limit_y = HEIGHT / min(WIDTH, HEIGHT)
        for start, end in lines:
            for x, y in (start, end):
                self.assertLessEqual(abs(x), limit_x + 1e-6)
                self.assertLessEqual(abs(y), limit_y + 1e-6)

    def test_detection_is_repeatable(self):
        image = _grid(tilt=4.0)
        self.assertEqual(geometry_auto.detect_lines(image),
                         geometry_auto.detect_lines(image))

    def test_downscaling_keeps_the_angle(self):
        big = np.repeat(np.repeat(_grid(tilt=6.0), 2, axis=0), 2, axis=1)
        result = geometry_auto.analyze(big, mode="level")
        self.assertAlmostEqual(result["optics"]["rotate"], 6.0, delta=0.2)


class LevelTests(unittest.TestCase):
    def test_tilted_grid_recovers_its_angle_within_0_2_degrees(self):
        for tilt in (3.5, -2.75, 8.0):
            with self.subTest(tilt=tilt):
                result = geometry_auto.analyze(_grid(tilt=tilt), mode="level")
                self.assertAlmostEqual(result["optics"]["rotate"], tilt,
                                       delta=0.2)
                self.assertGreater(result["confidence"], 0.5)
                self.assertGreaterEqual(result["lines"], 2)
                self.assertEqual(result["notes"], [])

    def test_the_patch_actually_levels_the_frame(self):
        image = _grid(tilt=5.0)
        before = _cluster_angles(image, "horizontal")
        patch = geometry_auto.analyze(image, mode="level")["optics"]
        after = _cluster_angles(edits.apply_manual_optics(image, patch),
                                "horizontal")
        self.assertAlmostEqual(before[0], 5.0, delta=0.2)
        self.assertAlmostEqual(after[0], 0.0, delta=0.2)

    def test_level_falls_back_to_the_vertical_lines(self):
        tilt = 4.0
        cosine = math.cos(math.radians(tilt))
        sine = math.sin(math.radians(tilt))
        lines = [tuple((cosine * x - sine * y, sine * x + cosine * y)
                       for x, y in ((offset, -0.85), (offset, 0.85)))
                 for offset in (-0.7, -0.35, 0.0, 0.35, 0.7)]
        result = geometry_auto.analyze(_render(lines), mode="level")
        self.assertAlmostEqual(result["optics"]["rotate"], tilt, delta=0.2)
        self.assertTrue(any("vertical" in note for note in result["notes"]),
                        result["notes"])

    def test_rotation_beyond_the_clamp_is_capped_and_reported(self):
        result = geometry_auto.analyze(_grid(tilt=19.0), mode="level")
        self.assertEqual(result["optics"]["rotate"], geometry_auto.ROTATE_LIMIT)
        self.assertTrue(any("15" in note for note in result["notes"]),
                        result["notes"])


class KeystoneTests(unittest.TestCase):
    def test_known_vertical_keystone_recovers_within_0_05(self):
        for value in (0.35, -0.4):
            with self.subTest(vertical=value):
                image = _grid({"vertical": value})
                result = geometry_auto.analyze(image, mode="vertical")
                self.assertAlmostEqual(result["optics"]["vertical"], value,
                                       delta=0.05)

    def test_the_patch_makes_the_verticals_parallel(self):
        image = _grid({"vertical": 0.45})
        before = _cluster_angles(image, "vertical")
        patch = geometry_auto.analyze(image, mode="vertical")["optics"]
        after = _cluster_angles(edits.apply_manual_optics(image, patch),
                                "vertical")
        self.assertGreater(before[1], 3.0)
        self.assertLess(after[1], 1.0)

    def test_full_mode_solves_rotation_and_both_keystones(self):
        truth = {"rotate": 4.0, "vertical": 0.3, "horizontal": -0.25}
        image = _grid(truth)
        result = geometry_auto.analyze(image, mode="full")
        patch = result["optics"]
        self.assertAlmostEqual(patch["rotate"], 4.0, delta=0.5)
        self.assertAlmostEqual(patch["vertical"], 0.3, delta=0.05)
        self.assertAlmostEqual(patch["horizontal"], -0.25, delta=0.05)
        straightened = edits.apply_manual_optics(image, patch)
        for axis in ("horizontal", "vertical"):
            before = _cluster_angles(image, axis)
            after = _cluster_angles(straightened, axis)
            self.assertGreater(before[1], 2.0, axis)
            self.assertLess(after[1], 1.0, axis)


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.blank = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)

    def test_two_converging_guides_produce_a_keystone(self):
        guides = [
            {"kind": "vertical", "points": [[-0.5, -0.8], [-0.62, 0.8]]},
            {"kind": "vertical", "points": [[0.5, -0.8], [0.62, 0.8]]},
        ]
        result = geometry_auto.analyze(self.blank, mode="vertical",
                                       guides=guides)
        # The pair converges towards the top, so the top must be stretched.
        self.assertGreater(result["optics"]["vertical"], 0.05)
        self.assertEqual(result["lines"], 2)
        self.assertTrue(result["notes"])

    def test_parallel_guides_ask_for_no_keystone(self):
        guides = [
            {"kind": "vertical", "points": [[-0.5, -0.8], [-0.5, 0.8]]},
            {"kind": "vertical", "points": [[0.5, -0.8], [0.5, 0.8]]},
        ]
        result = geometry_auto.analyze(self.blank, mode="vertical",
                                       guides=guides)
        self.assertAlmostEqual(result["optics"]["vertical"], 0.0, delta=0.01)

    def test_a_single_guide_still_sets_the_rotation(self):
        guides = [{"kind": "horizontal", "points": [[-0.8, -0.1],
                                                    [0.8, 0.1]]}]
        result = geometry_auto.analyze(self.blank, mode="level", guides=guides)
        expected = math.degrees(math.atan2(0.2, 1.6))
        self.assertAlmostEqual(result["optics"]["rotate"], expected, delta=0.1)

    def test_guides_replace_detection(self):
        guides = [{"kind": "horizontal", "points": [[-0.8, 0.0],
                                                    [0.8, 0.0]]}]
        result = geometry_auto.analyze(_grid(tilt=8.0), mode="level",
                                       guides=guides)
        self.assertAlmostEqual(result["optics"]["rotate"], 0.0, delta=0.05)
        self.assertEqual(result["lines"], 1)

    def test_malformed_guides_are_ignored(self):
        guides = [{"kind": "vertical", "points": [[0.0, 0.0]]},
                  {"kind": "diagonal", "points": [[0, 0], [1, 1]]},
                  {"points": [[0, 0], [1, 1]]}, "nonsense",
                  {"kind": "vertical", "points": [[float("nan"), 0], [1, 1]]}]
        result = geometry_auto.analyze(self.blank, mode="level", guides=guides)
        self.assertEqual(result["optics"], {})
        self.assertEqual(result["confidence"], 0.0)


class DegenerateInputTests(unittest.TestCase):
    def test_blank_frame_returns_an_empty_patch_at_zero_confidence(self):
        result = geometry_auto.analyze(np.zeros((HEIGHT, WIDTH, 3),
                                                dtype=np.float32))
        self.assertEqual(result["optics"], {})
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["lines"], 0)
        self.assertTrue(result["notes"])

    def test_odd_inputs_never_raise(self):
        cases = [
            np.full((HEIGHT, WIDTH, 3), 0.5, dtype=np.float32),
            np.zeros((4, 4, 3), dtype=np.float32),
            np.zeros((HEIGHT, WIDTH), dtype=np.float32),
            (np.random.default_rng(7).random((80, 80, 3)) * 255).astype(
                np.uint8),
            np.full((60, 60, 3), np.nan, dtype=np.float32),
            np.zeros((0, 0, 3), dtype=np.float32),
            None,
        ]
        for index, image in enumerate(cases):
            with self.subTest(case=index):
                result = geometry_auto.analyze(image, mode="full")
                self.assertIn("optics", result)
                self.assertGreaterEqual(result["confidence"], 0.0)

    def test_an_unknown_mode_falls_back_to_full(self):
        image = _grid(tilt=4.0)
        self.assertEqual(geometry_auto.analyze(image, mode="sideways"),
                         geometry_auto.analyze(image, mode="full"))


class ScaleTests(unittest.TestCase):
    def test_identity_needs_no_crop_and_a_rotation_does(self):
        self.assertEqual(geometry_auto.solve_scale({}), 1.0)
        self.assertEqual(geometry_auto.solve_scale(dict(edits.OPTICS_DEFAULTS)),
                         1.0)
        self.assertGreater(geometry_auto.solve_scale({"rotate": 10.0}), 1.0)

    def test_scale_stays_inside_the_edits_clamp(self):
        extreme = geometry_auto.solve_scale(
            {"rotate": 15.0, "vertical": 1.0, "horizontal": 1.0}, aspect=2.0)
        self.assertLessEqual(extreme, 1.6)
        self.assertGreaterEqual(extreme, 1.0)

    def test_the_solved_scale_leaves_no_empty_corner(self):
        white = np.ones((HEIGHT, WIDTH, 3), dtype=np.float32)
        for patch in ({"rotate": 7.0}, {"vertical": 0.6},
                      {"rotate": -5.0, "vertical": 0.3, "horizontal": -0.4}):
            with self.subTest(patch=tuple(sorted(patch.items()))):
                patch = dict(patch)
                patch["scale"] = geometry_auto.solve_scale(
                    patch, aspect=WIDTH / HEIGHT)
                warped = edits.apply_manual_optics(white, patch)
                self.assertGreater(float(warped.min()), 0.99)


class PatchContractTests(unittest.TestCase):
    def test_every_mode_returns_a_clean_bounded_optics_patch(self):
        image = _grid({"rotate": 4.0, "vertical": 0.3, "horizontal": -0.2})
        for mode in geometry_auto.MODES:
            with self.subTest(mode=mode):
                result = geometry_auto.analyze(image, mode=mode)
                patch = result["optics"]
                self.assertTrue(set(patch) <= set(edits.OPTICS_DEFAULTS),
                                sorted(patch))
                self.assertIn("scale", patch)
                self.assertGreaterEqual(patch["scale"], 1.0)
                self.assertLessEqual(patch["scale"], 1.6)
                self.assertLessEqual(abs(patch.get("rotate", 0.0)), 15.0)
                self.assertLessEqual(abs(patch.get("vertical", 0.0)), 1.0)
                self.assertLessEqual(abs(patch.get("horizontal", 0.0)), 1.0)
                self.assertGreaterEqual(result["confidence"], 0.0)
                self.assertLessEqual(result["confidence"], 1.0)
                self.assertIsInstance(result["lines"], int)
                for note in result["notes"]:
                    self.assertIsInstance(note, str)

    def test_the_patch_survives_edits_clean_optics_unchanged(self):
        patch = geometry_auto.analyze(_grid(tilt=6.0), mode="full")["optics"]
        cleaned = edits.clean_optics(patch)
        for key, value in patch.items():
            self.assertAlmostEqual(cleaned[key], value, places=5, msg=key)

    def test_the_two_model_maps_are_inverses(self):
        optics = {"rotate": 6.0, "vertical": 0.4, "horizontal": -0.3,
                  "distortion": 0.5, "scale": 1.2}
        points = np.array([[0.0, 0.0], [1.2, 0.9], [-1.1, 0.5], [0.3, -0.8]])
        source = geometry_auto._source_from_output(points, optics)
        back = geometry_auto._output_from_source(source, optics)
        np.testing.assert_allclose(back, points, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
