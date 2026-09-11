# SPDX-License-Identifier: GPL-3.0-only
"""Reference sanity checks; real worker cases run in the export gate."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from processing_export import (analytical_exposure, reference_geometry,
                               reference_resize, srgb_decode, srgb_encode)


class ProcessingExportTests(unittest.TestCase):
    def test_transfer_function_anchors_and_exposure_are_linear_light(self):
        np.testing.assert_allclose(srgb_encode([0, .0031308, .18, 1]),
                                   [0, .040449936, .4613561295, 1], atol=1e-9)
        np.testing.assert_allclose(srgb_decode(srgb_encode([0, .001, .18, .75, 1])),
                                   [0, .001, .18, .75, 1], atol=1e-12)
        expected = srgb_encode(.36)
        self.assertAlmostEqual(float(analytical_exposure(srgb_encode(.18), 1)), expected)
        self.assertGreater(abs(expected - srgb_encode(.18) * 2), .1)

    def test_lanczos_keeps_constant_colors_and_rotated_crop_location(self):
        constant = np.full((24, 36, 3), [.11, .33, .77])
        resized = reference_resize(constant, 12)
        self.assertEqual(resized.shape, (8, 12, 3))
        np.testing.assert_allclose(resized, np.broadcast_to([.11, .33, .77], resized.shape), atol=1e-12)
        image = np.arange(6 * 8 * 3).reshape(6, 8, 3) / 144
        result = reference_geometry(image, rotate=90,
                                    crop={"x": 0, "y": 0, "w": .5, "h": .5})
        np.testing.assert_array_equal(result, np.rot90(image, 3)[:4, :3])


if __name__ == "__main__":
    unittest.main()
