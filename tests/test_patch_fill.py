# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import sys
import unittest

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import patch_fill  # noqa: E402


def stripes(height=96, width=128):
    rows, columns = np.mgrid[0:height, 0:width]
    return np.stack([
        0.2 + 0.6 * ((columns % 8) < 4),
        0.3 + 0.4 * ((rows % 10) < 5),
        np.full((height, width), 0.5),
    ], axis=-1).astype(np.float32)


class PatchFillTests(unittest.TestCase):
    def test_repeating_texture_is_rebuilt_from_known_pixels(self):
        truth = stripes()
        hole = np.zeros(truth.shape[:2], dtype=bool)
        hole[30:66, 50:80] = True
        damaged = truth.copy()
        damaged[hole] = 0.0
        filled = patch_fill.fill(damaged, hole, seed=3)
        self.assertLess(float(np.abs(filled[hole] - truth[hole]).mean()), 0.02)
        np.testing.assert_array_equal(filled[~hole], damaged[~hole])

    def test_same_seed_renders_identical_pixels(self):
        truth = stripes()
        hole = np.zeros(truth.shape[:2], dtype=bool)
        hole[20:40, 20:90] = True
        first = patch_fill.fill(truth, hole, seed=11)
        second = patch_fill.fill(truth, hole, seed=11)
        np.testing.assert_array_equal(first, second)
        self.assertTrue(np.isfinite(first).all())

    def test_degenerate_holes_return_finite_copies(self):
        image = stripes(24, 24)
        empty = np.zeros(image.shape[:2], dtype=bool)
        np.testing.assert_array_equal(patch_fill.fill(image, empty), image)
        full = np.ones(image.shape[:2], dtype=bool)
        np.testing.assert_array_equal(patch_fill.fill(image, full), image)
        # A tiny frame has no legal source patch; the nearest known pixel is used.
        tiny = np.zeros((9, 9, 3), dtype=np.float32)
        tiny[:, :4] = 1.0
        hole = np.zeros((9, 9), dtype=bool)
        hole[3:6, 5:8] = True
        result = patch_fill.fill(tiny, hole)
        self.assertTrue(np.isfinite(result).all())


if __name__ == "__main__":
    unittest.main()
