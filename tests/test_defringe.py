# SPDX-License-Identifier: GPL-3.0-only
"""Defringe removes false colour beside hard edges and nothing else."""
import unittest

import numpy as np

import edits


def fringe_target(fringe_rgb, width=96, height=48, column=48, band=3):
    """A dark-to-bright vertical edge with a coloured strip on its bright side."""
    image = np.full((height, width, 3), 0.15, dtype=np.float32)
    image[:, column:] = 0.85
    image[:, column:column + band] = fringe_rgb
    # A flat patch of the same colour far from any edge must survive.
    image[8:24, 8:24] = fringe_rgb
    return image


def chroma(image):
    return image.max(axis=2) - image.min(axis=2)


PURPLE = (0.55, 0.15, 0.7)
GREEN = (0.2, 0.7, 0.25)


class DefringeTests(unittest.TestCase):
    def test_purple_amount_drains_the_fringe_but_not_flat_colour(self):
        image = fringe_target(PURPLE)
        out = edits.apply_defringe(image, {"defringePurple": 1.0})
        strip = (slice(None), slice(48, 51))
        self.assertLess(chroma(out[strip]).mean(), 0.25 * chroma(image[strip]).mean())
        # The patch outline is itself a hard edge, so only its interior is
        # promised to survive; a fringe is never wider than that margin.
        np.testing.assert_allclose(out[11:21, 11:21], image[11:21, 11:21], atol=1e-6)
        np.testing.assert_allclose(out[:, 60:], image[:, 60:], atol=1e-6)
        # Brightness is kept: the strip takes its neighbours' colour at its own luma.
        luma = lambda block: (0.2126 * block[..., 0] + 0.7152 * block[..., 1] + 0.0722 * block[..., 2])
        self.assertAlmostEqual(float(luma(out[strip]).mean()), float(luma(image[strip]).mean()), delta=0.03)

    def test_amount_scales_and_zero_is_identity(self):
        image = fringe_target(PURPLE)
        strip = (slice(None), slice(48, 51))
        half = edits.apply_defringe(image, {"defringePurple": 0.5})
        full = edits.apply_defringe(image, {"defringePurple": 1.0})
        self.assertIs(edits.apply_defringe(image, {"defringePurple": 0.0}), image)
        self.assertGreater(chroma(half[strip]).mean(), chroma(full[strip]).mean())
        self.assertLess(chroma(half[strip]).mean(), chroma(image[strip]).mean())

    def test_each_hue_range_only_touches_its_own_fringe(self):
        purple = fringe_target(PURPLE)
        green = fringe_target(GREEN)
        np.testing.assert_allclose(edits.apply_defringe(green, {"defringePurple": 1.0}), green, atol=1e-6)
        np.testing.assert_allclose(edits.apply_defringe(purple, {"defringeGreen": 1.0}), purple, atol=1e-6)
        out = edits.apply_defringe(green, {"defringeGreen": 1.0})
        self.assertLess(chroma(out[:, 48:51]).mean(), 0.25 * chroma(green[:, 48:51]).mean())

    def test_hue_range_wraps_through_red(self):
        red_fringe = fringe_target((0.8, 0.15, 0.2))
        strip = (slice(None), slice(48, 51))
        inside = edits.apply_defringe(red_fringe, {
            "defringePurple": 1.0, "defringePurpleHueStart": 300.0, "defringePurpleHueEnd": 30.0})
        outside = edits.apply_defringe(red_fringe, {
            "defringePurple": 1.0, "defringePurpleHueStart": 250.0, "defringePurpleHueEnd": 330.0})
        self.assertLess(chroma(inside[strip]).mean(), 0.25 * chroma(red_fringe[strip]).mean())
        np.testing.assert_allclose(outside, red_fringe, atol=1e-6)

    def test_defringe_runs_inside_the_shared_base_stage(self):
        image = fringe_target(PURPLE)
        optics = {"defringePurple": 1.0, "flipHorizontal": True}
        staged = edits.apply_base(image, optics)
        expected = edits.apply_manual_optics(edits.apply_defringe(image, optics), optics)
        np.testing.assert_allclose(staged, expected, atol=1e-6)
        alpha = np.concatenate([image, np.full(image.shape[:2] + (1,), 0.5, np.float32)], axis=2)
        with_alpha = edits.apply_defringe(alpha, {"defringePurple": 1.0})
        self.assertEqual(with_alpha.shape[2], 4)
        np.testing.assert_allclose(with_alpha[..., 3], 0.5)

    def test_schema_cleaning_and_bake_gates(self):
        cleaned = edits.clean_optics({"defringePurple": 7, "defringeGreen": -1,
                                      "defringePurpleHueStart": 400, "defringeGreenHueEnd": "x",
                                      "profileChromatic": 0})
        self.assertEqual(cleaned["defringePurple"], 1.0)
        self.assertEqual(cleaned["defringeGreen"], 0.0)
        self.assertEqual(cleaned["defringePurpleHueStart"], 360.0)
        self.assertEqual(cleaned["defringeGreenHueEnd"], edits.OPTICS_DEFAULTS["defringeGreenHueEnd"])
        self.assertFalse(cleaned["profileChromatic"])
        self.assertTrue(edits.clean_optics(None)["profileChromatic"])
        self.assertFalse(edits.defringe_is_active(None))
        self.assertTrue(edits.defringe_is_active({"defringeGreen": 0.2}))
        # A hue range with no amount changes nothing, so it is still identity.
        self.assertTrue(edits.optics_is_identity({"defringePurpleHueStart": 10}))
        self.assertFalse(edits.optics_is_identity({"defringeGreen": 0.3}))
        self.assertFalse(edits.optics_requires_bake({"distortion": 0.4}))
        self.assertTrue(edits.optics_requires_bake({"defringePurple": 0.4}))
        self.assertTrue(edits.optics_requires_bake({"profileEnabled": True}))
        for key, (low, high) in edits.OPTICS_RANGES.items():
            self.assertIn(key, edits.OPTICS_DEFAULTS)
            self.assertTrue(low <= edits.OPTICS_DEFAULTS[key] <= high, key)


if __name__ == "__main__":
    unittest.main()
