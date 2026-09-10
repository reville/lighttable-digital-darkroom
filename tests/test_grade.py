import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import numpy as np

import grade

ROOT = Path(__file__).resolve().parents[1]


class ParametricStateTests(unittest.TestCase):
    def test_controls_survive_cleaning_without_changing_authoritative_pixels(self):
        curve = [(x / 255) ** .8 for x in range(256)]
        state = grade.clean({'curveL': curve, 'parametricCurve': {'lights': 40}})
        self.assertEqual(state['parametricCurve']['lights'], 40)
        self.assertEqual(state['parametricCurve']['splitDL'], .5)
        self.assertEqual(state['curveL'], grade.clean({'curveL': curve})['curveL'])
        self.assertEqual(grade.clean(state), state)


class GradeEffectsTests(unittest.TestCase):
    def setUp(self):
        y, x = np.mgrid[0:32, 0:32].astype(np.float32)
        checker = ((x.astype(int) + y.astype(int)) % 2).astype(np.float32)
        self.image = np.stack([
            x / 31.0,
            y / 31.0,
            0.25 + checker * 0.5,
        ], axis=2)

    def test_identity_returns_the_input(self):
        out = grade.apply(self.image, {})
        np.testing.assert_array_equal(out, self.image)

    def test_vignette_defaults_reproduce_legacy_pixels(self):
        image = np.full((97, 129, 3), 0.4, dtype=np.float32)
        yy, xx = np.mgrid[:97, :129].astype(np.float32)
        radius = np.sqrt(((xx / 128 - .5) * 2) ** 2 +
                         ((yy / 96 - .5) * 2) ** 2) / 1.4142
        for amount in (-0.6, 0.6):
            expected = np.clip(image * np.clip(
                1 - amount * .9 * radius ** 2.2, 0, 2)[..., None], 0, 1)
            actual = grade.apply(image, {"vignette": amount})
            np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-7)
            np.testing.assert_array_equal(actual, grade.apply(image, {
                "vignette": amount, "vignetteSize": .5, "vignetteFeather": 1}))

    def test_vignette_size_and_feather_shape_the_transition(self):
        image = np.full((101, 101, 3), .5, dtype=np.float32)
        def render(size, feather):
            return grade.apply(image, {"vignette": .8,
                "vignetteSize": size, "vignetteFeather": feather})
        small, large = render(.2, .5), render(.6, .5)
        self.assertGreater(float(large[50, 85, 0]), float(small[50, 85, 0]))
        hard, soft = render(.3, 0), render(.3, 1)
        self.assertLess(float(soft[50, 85, 0]), float(hard[50, 85, 0]))
        for pixels in (small, large, hard, soft):
            np.testing.assert_array_equal(pixels[50, 50], image[50, 50])
            self.assertTrue(np.isfinite(pixels).all())
            self.assertGreaterEqual(float(pixels.min()), 0)
            self.assertLessEqual(float(pixels.max()), .5)

    def test_vignette_shape_without_amount_is_an_exact_identity(self):
        image = self.image * 2 - .5
        for size in (0, .5, 1):
            for feather in (0, .5, 1):
                settings = {"vignetteSize": size, "vignetteFeather": feather}
                self.assertTrue(grade.is_identity(settings))
                np.testing.assert_array_equal(grade.apply(image, settings), image)

    def test_vignette_shape_ranges_are_bounded(self):
        cleaned = grade.clean({"vignetteSize": -1, "vignetteFeather": 2})
        self.assertEqual(cleaned["vignetteSize"], 0)
        self.assertEqual(cleaned["vignetteFeather"], 1)

    def test_new_effects_are_cleaned_and_non_identity(self):
        cleaned = grade.clean({"texture": 0.4, "clarity": -0.3,
                               "dehaze": 0.25})
        self.assertEqual(cleaned["texture"], 0.4)
        self.assertEqual(cleaned["clarity"], -0.3)
        self.assertEqual(cleaned["dehaze"], 0.25)
        self.assertFalse(grade.is_identity(cleaned))

    def test_texture_changes_local_detail_without_leaving_gamut(self):
        out = grade.apply(self.image, {"texture": 0.7})
        self.assertGreater(float(np.abs(out - self.image).mean()), 0.001)
        self.assertGreaterEqual(float(out.min()), 0.0)
        self.assertLessEqual(float(out.max()), 1.0)

    def test_combined_effects_are_finite_and_shape_preserving(self):
        out = grade.apply(self.image, {
            "texture": 0.45, "clarity": 0.35, "dehaze": 0.2,
        })
        self.assertEqual(out.shape, self.image.shape)
        self.assertTrue(np.isfinite(out).all())
        self.assertGreaterEqual(float(out.min()), 0.0)
        self.assertLessEqual(float(out.max()), 1.0)

    @unittest.skipUnless(grade._HAS_NUMBA, "Numba is not installed")
    def test_numba_grade_stages_match_numpy_fallback(self):
        settings = {
            "exposure": -0.52, "highlights": -0.76, "shadows": -0.30,
            "whites": -0.10, "blacks": -0.07, "contrast": 0.43,
            "dehaze": 0.02, "temp": 0.53, "tint": -0.45,
            "saturation": 0.37, "vibrance": 0.74,
        }
        original = grade._HAS_NUMBA
        try:
            grade._HAS_NUMBA = False
            expected = grade.apply(self.image, settings)
            grade._HAS_NUMBA = True
            actual = grade.apply(self.image, settings)
        finally:
            grade._HAS_NUMBA = original
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5)

    def test_detail_defaults_remain_an_identity_even_with_nonzero_radius(self):
        cleaned = grade.clean({})
        self.assertEqual(cleaned["sharpenRadius"], 1.0)
        self.assertEqual(cleaned["sharpenDetail"], 0.25)
        self.assertTrue(grade.is_identity(cleaned))

    def test_sharpening_and_noise_reduction_are_bounded_and_effective(self):
        sharpened = grade.apply(self.image, {
            "sharpness": 0.7, "sharpenRadius": 1.4,
            "sharpenDetail": 0.6, "sharpenMasking": 0.25,
        })
        reduced = grade.apply(self.image, {
            "luminanceNoise": 0.6, "colorNoise": 0.7,
        })
        for output in (sharpened, reduced):
            self.assertEqual(output.shape, self.image.shape)
            self.assertTrue(np.isfinite(output).all())
            self.assertGreaterEqual(float(output.min()), 0.0)
            self.assertLessEqual(float(output.max()), 1.0)
            self.assertGreater(float(np.abs(output - self.image).mean()), 0.0001)

    def test_manual_chromatic_aberration_moves_only_selected_channels(self):
        red = grade.apply(self.image, {"chromaticAberrationRedCyan": 0.8})
        blue = grade.apply(self.image, {"chromaticAberrationBlueYellow": -0.8})
        np.testing.assert_allclose(red[..., 1], self.image[..., 1], atol=1e-6)
        np.testing.assert_allclose(red[..., 2], self.image[..., 2], atol=1e-6)
        np.testing.assert_allclose(blue[..., 0], self.image[..., 0], atol=1e-6)
        np.testing.assert_allclose(blue[..., 1], self.image[..., 1], atol=1e-6)
        self.assertGreater(float(np.abs(red[..., 0] - self.image[..., 0]).mean()), 0)
        self.assertGreater(float(np.abs(blue[..., 2] - self.image[..., 2]).mean()), 0)

    def test_point_color_targets_only_nearby_hues(self):
        image = np.array([[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]], dtype=np.float32)
        out = grade.apply(image, {"pointColor": [{
            "hue": 0, "range": 24, "hueShift": 90,
            "saturation": -0.3, "luminance": 0.2,
        }]})
        self.assertGreater(float(np.abs(out[0, 0] - image[0, 0]).mean()), 0.05)
        np.testing.assert_allclose(out[0, 1], image[0, 1], atol=1e-5)

    def test_point_color_uniformity_zero_is_byte_identical(self):
        point = {"hue": 20, "range": 40, "hueShift": 8,
                 "saturation": -0.1, "luminance": 0.05,
                 "refSaturation": 0.55, "refLuminance": 0.62}
        baseline = grade.apply(self.image, {"pointColor": [point]})
        explicit = grade.apply(self.image, {"pointColor": [{
            **point, "uniformHue": 0, "uniformSaturation": 0,
            "uniformLuminance": 0,
        }]})
        np.testing.assert_array_equal(explicit, baseline)

    def test_point_color_uniformity_reduces_hue_variance(self):
        hsv = np.array([[[5.0, 1.0, 0.6], [15.0, 1.0, 0.7],
                         [25.0, 1.0, 0.5]]], dtype=np.float32)
        image = grade._hsv_to_rgb(hsv[..., 0], hsv[..., 1], hsv[..., 2])
        output = grade.apply(image, {"pointColor": [{
            "hue": 15, "range": 40, "hueShift": 0,
            "saturation": 0, "luminance": 0,
            "refSaturation": 0.6, "refLuminance": 0.6,
            "uniformHue": 1, "uniformSaturation": 1,
            "uniformLuminance": 1,
        }]})
        hue, saturation, value = grade._rgb_to_hsv(output)
        self.assertLess(float(np.ptp(hue)), 1.0)
        self.assertLess(float(np.ptp(saturation)), 0.03)
        self.assertLess(float(np.ptp(value)), 0.03)

    def test_color_grading_has_independent_tonal_weights(self):
        ramp = np.array([[[0.08, 0.08, 0.08], [0.5, 0.5, 0.5],
                          [0.92, 0.92, 0.92]]], dtype=np.float32)
        out = grade.apply(ramp, {"colorGrading": {
            "shadows": {"hue": 220, "saturation": 0.8},
            "midtones": {"hue": 0, "saturation": 0},
            "highlights": {"hue": 35, "saturation": 0.8},
            "global": {"hue": 0, "saturation": 0},
            "balance": 0, "blending": 0.5,
        }})
        self.assertGreater(float(out[0, 0, 2]), float(out[0, 0, 0]))
        self.assertGreater(float(out[0, 2, 0]), float(out[0, 2, 2]))
        self.assertLess(float(np.abs(out[0, 1] - ramp[0, 1]).mean()), 0.02)

    def test_advanced_color_schema_is_bounded(self):
        cleaned = grade.clean({
            "pointColor": [{"hue": 721, "range": 1000,
                            "hueShift": -999, "saturation": 4,
                            "luminance": -4}] * 20,
            "colorGrading": {"shadows": {"hue": -30,
                "saturation": 2, "luminance": -2}},
        })
        self.assertEqual(len(cleaned["pointColor"]), 8)
        self.assertEqual(cleaned["pointColor"][0]["hue"], 1)
        self.assertEqual(cleaned["pointColor"][0]["range"], 90)
        self.assertEqual(cleaned["colorGrading"]["shadows"]["saturation"], 1)

    def test_monochrome_produces_equal_rgb_channels(self):
        out = grade.apply(self.image, {"monochrome": 1.0})
        np.testing.assert_allclose(out[..., 0], out[..., 1], rtol=0, atol=1e-6)
        np.testing.assert_allclose(out[..., 1], out[..., 2], rtol=0, atol=1e-6)

    def test_monochrome_with_mixer_shifts_color_luminance(self):
        blue_pixel = np.array([[[0.1, 0.2, 0.9]]], dtype=np.float32)
        base = grade.apply(blue_pixel, {"monochrome": 1.0})
        boosted = grade.apply(blue_pixel, {
            "monochrome": 1.0,
            "hsl": {"blue": {"h": 0, "s": 0, "l": 0.8}},
        })
        darkened = grade.apply(blue_pixel, {
            "monochrome": 1.0,
            "hsl": {"blue": {"h": 0, "s": 0, "l": -0.8}},
        })
        self.assertGreater(float(boosted[0, 0, 0]), float(base[0, 0, 0]))
        self.assertLess(float(darkened[0, 0, 0]), float(base[0, 0, 0]))


class ParallelKernelSafetyTests(unittest.TestCase):
    def test_concurrent_grade_calls_do_not_abort_the_process(self):
        """numba's workqueue threading layer aborts the whole process when two
        threads enter a parallel kernel at once. Export workers and request
        handler threads both grade, so entry must be serialised. The check runs
        in a subprocess because an abort would take the test runner with it."""
        if not grade._HAS_NUMBA:
            self.skipTest("numba is unavailable")
        script = textwrap.dedent(f"""
            import sys, threading
            import numpy as np
            sys.path.insert(0, {str(ROOT)!r})
            import grade
            img = np.random.default_rng(0).random((1200, 1800, 3)).astype(np.float32)
            g = {{"exposure": 0.5, "contrast": 0.2, "highlights": -0.3}}
            grade.apply(img, g)
            def work():
                for _ in range(4):
                    grade.apply(img, g)
            threads = [threading.Thread(target=work) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            print("survived")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=300, env=dict(os.environ, NUMBA_NUM_THREADS="4"))
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertIn("survived", result.stdout)


if __name__ == "__main__":
    unittest.main()
