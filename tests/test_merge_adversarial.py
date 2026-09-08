"""Merge invariants for low-information frames and recoverable bad inputs."""
import unittest

import numpy as np

import merge_workflow as merge


class HDRBoundaryTests(unittest.TestCase):
    def test_identical_featureless_frames_preserve_every_pixel(self):
        for shape in ((64, 96, 3), (17, 11, 3)):
            for value in (.2, .5, 1.0):
                with self.subTest(shape=shape, value=value):
                    image = np.full(shape, value, dtype=np.float32)
                    original = image.copy()
                    result = merge.hdr_merge([image, image])
                    np.testing.assert_allclose(result, image, atol=1e-6)
                    np.testing.assert_array_equal(image, original)

    def test_featureless_exposure_brackets_remain_spatially_uniform(self):
        frames = [np.full((64, 96, 3), value, dtype=np.float32)
                  for value in (.2, .5, .8)]
        result = merge.hdr_merge(frames)
        self.assertGreater(float(result.min()), .2)
        self.assertLess(float(result.max()), .8)
        np.testing.assert_allclose(result, np.full_like(result, result[0, 0, 0]), atol=1e-6)

    def test_dimension_failure_does_not_poison_a_later_merge(self):
        image = np.full((64, 96, 3), .5, dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "matching dimensions"):
            merge.hdr_merge([image, image[:, :-1]])
        np.testing.assert_allclose(merge.hdr_merge([image, image]), image, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
