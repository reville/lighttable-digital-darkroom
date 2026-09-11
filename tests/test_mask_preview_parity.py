# SPDX-License-Identifier: GPL-3.0-only
"""Preview and export must apply the same mask mathematics.

The browser rasterises masks for the on-screen grade and edits.py rasterises
them for every server render and the delivered file. Where the two disagreed a
user saw one photograph and received another.
"""
from __future__ import annotations

import math
import unittest

import numpy as np

import edits


def linear_mask(start, end):
    return {
        "id": "m1", "name": "Gradient", "type": "linear", "enabled": True,
        "invert": False, "opacity": 1.0, "grade": {},
        "components": [{"id": "c1", "type": "linear", "combine": "add",
                        "invert": False, "start": list(start), "end": list(end)}],
    }


class LinearGradientDegeneracyTests(unittest.TestCase):
    """A gradient dragged shut has no direction and must grade nothing."""

    def test_a_collapsed_gradient_contributes_nothing(self):
        mask = edits.clean_masks([linear_mask((0.5, 0.5), (0.5, 0.5))])[0]
        weight = edits.raster_mask(mask, 128, 128)
        self.assertEqual(float(weight.max()), 0.0,
                         "a zero-length gradient must not grade half the frame")

    def test_a_very_short_gradient_renders_the_same_at_every_size(self):
        # Half a pixel apart on a 128 px preview raster, twenty-three pixels
        # apart at 6000 px. The old pixel-sized rule answered differently at
        # each size, so this showed no mask on screen while the export clamped
        # its denominator and graded half the frame. It is a legitimate hard
        # edge, so it must render, and render identically, at both sizes.
        span = 0.5 / 127
        mask = edits.clean_masks([linear_mask((0.5, 0.5), (0.5 + span, 0.5))])[0]
        coverage = []
        for edge in (128, 1024, 6000):
            weight = edits.raster_mask(mask, edge, edge)
            self.assertGreater(float(weight.max()), 0.9)
            coverage.append(float((weight > 0.5).mean()))
        for value in coverage[1:]:
            self.assertAlmostEqual(value, coverage[0], places=2)

    def test_the_threshold_is_normalized_not_pixel_sized(self):
        self.assertLess(edits.LINEAR_MIN_SPAN, 1e-3)
        self.assertGreater(edits.LINEAR_MIN_SPAN, 0.0)

    def test_a_real_gradient_is_resolution_independent(self):
        mask = edits.clean_masks([linear_mask((0.25, 0.5), (0.75, 0.5))])[0]
        coarse = edits.raster_mask(mask, 64, 64)
        fine = edits.raster_mask(mask, 512, 512)
        # Sample the same normalized positions in both rasters. The tolerance
        # covers the half-pixel each index rounds to, not a difference in the
        # ramp itself.
        for u in (0.1, 0.3, 0.5, 0.7, 0.9):
            with self.subTest(u=u):
                a = float(coarse[32, round(u * 63)])
                b = float(fine[256, round(u * 511)])
                self.assertAlmostEqual(a, b, delta=0.03)


class IntersectRefinementTests(unittest.TestCase):
    """Intersect takes the smaller weight; it never multiplies."""

    def test_intersect_component_takes_the_minimum(self):
        mask = edits.clean_masks([{
            "id": "m1", "name": "Mask", "type": "linear", "enabled": True,
            "invert": False, "opacity": 1.0, "grade": {},
            "components": [
                {"id": "a", "type": "linear", "combine": "add", "invert": False,
                 "start": [0.0, 0.5], "end": [1.0, 0.5]},
                {"id": "b", "type": "linear", "combine": "intersect",
                 "invert": False, "start": [0.0, 0.5], "end": [1.0, 0.5]},
            ],
        }])[0]
        weight = edits.raster_mask(mask, 64, 64)
        ramp = edits.raster_mask(edits.clean_masks(
            [linear_mask((0.0, 0.5), (1.0, 0.5))])[0], 64, 64)
        # min(x, x) is x. A multiplying implementation would give x * x.
        np.testing.assert_allclose(weight, np.minimum(ramp, ramp), atol=1e-6)
        self.assertGreater(float(np.abs(weight - ramp * ramp).max()), 0.05,
                           "the fixture must actually distinguish the two rules")


class RefinementBudgetTests(unittest.TestCase):
    """A full mask keeps the stroke the user just painted."""

    def test_refinements_survive_a_mask_already_at_the_component_cap(self):
        stroke = {"points": [[0.4, 0.4], [0.6, 0.6]], "size": 0.1,
                  "flow": 1.0, "feather": 0.5}
        components = [
            {"id": f"c{n}", "type": "linear",
             "combine": "add" if n == 0 else "add", "invert": False,
             "start": [0.0, 0.5], "end": [1.0, 0.5]}
            for n in range(edits.MAX_MASK_COMPONENTS)
        ]
        mask = edits.clean_masks([{
            "id": "m1", "name": "Mask", "type": "linear", "enabled": True,
            "invert": False, "opacity": 1.0, "grade": {},
            "components": components,
            "addStrokes": [stroke],
            "subtractStrokes": [stroke],
        }])[0]
        combines = [component["combine"] for component in mask["components"]]
        self.assertLessEqual(len(mask["components"]), edits.MAX_MASK_COMPONENTS)
        self.assertIn("subtract", combines,
                      "a fresh refinement must not be dropped without a word")
        self.assertEqual(
            sum(1 for component in mask["components"]
                if component["type"] == "brush"), 2)


if __name__ == "__main__":
    unittest.main()
