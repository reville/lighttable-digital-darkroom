"""Numeric parity checks; opt-in GPU cases exercise the actual bundled worker."""
import os
import unittest
from unittest import mock

import numpy as np
from scipy import ndimage
from skimage import transform

import merge_acceleration as gpu
import merge_workflow as merge


class MergeFallbackTests(unittest.TestCase):
    def test_panorama_projective_horizon_retains_cpu_warp(self):
        image = np.zeros((64, 64, 3), dtype=np.float32)
        for denominator in ([1, 0, -32], [0, 0, 1e-10]):
            inverse = np.array([[1, 0, 0], [0, 1, 0], denominator], dtype=np.float64)
            with mock.patch.object(gpu, "_disabled", False), \
                    mock.patch.object(gpu, "_MIN_PIXELS", 0), \
                    mock.patch.object(gpu, "_compute") as compute, \
                    mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "gpu"}):
                self.assertIsNone(gpu.panorama_warp(image, np.linalg.inv(inverse), (64, 64)))
                compute.assert_not_called()

    def test_missing_gpu_retains_cpu_merge_and_stops_retrying(self):
        frame = np.random.default_rng(4).random((48, 52, 3), dtype=np.float32)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "cpu"}):
            expected = merge.hdr_merge([frame, frame])
        with mock.patch.object(gpu, "_disabled", False), \
                mock.patch.object(gpu, "_MIN_PIXELS", 0), \
                mock.patch("gpu_compute.compute", side_effect=RuntimeError("no device")) as call, \
                mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "gpu"}):
            actual = merge.hdr_merge([frame, frame])
            np.testing.assert_array_equal(actual, expected)
            self.assertEqual(call.call_count, 1)


@unittest.skipUnless(os.environ.get("LIGHTTABLE_TEST_GPU") == "1", "requires actual GPU worker")
class MergeGpuParityTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(gpu, "_MIN_PIXELS", 0),
                        mock.patch.object(gpu, "_TILE_PIXELS", 2500),
                        mock.patch.object(gpu, "_disabled", False),
                        mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "gpu"})]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in reversed(self.patches)])
        self.rng = np.random.default_rng(12)
        self.image = self.rng.random((89, 97, 3), dtype=np.float32)

    def test_hdr_shift_matches_subpixel_boundaries(self):
        for shift in [(0.75, -1.2), (-2.4, 3.1), (0, 1)]:
            actual = gpu.shift(self.image, shift)
            self.assertIsNotNone(actual)
            expected = ndimage.shift(self.image, (*shift, 0), order=1,
                                     mode="constant", prefilter=False)
            np.testing.assert_allclose(actual[..., :3], expected, atol=8e-6, rtol=0)
            expected_mask = ndimage.shift(np.ones(self.image.shape[:2]), shift,
                                          order=0, mode="constant", prefilter=False)
            np.testing.assert_array_equal(actual[..., 3], expected_mask)

    def test_hdr_fusion_even_and_odd_brackets(self):
        for count in [2, 3, 9]:
            images = [np.clip(self.image * (0.6 + i * 0.12), 0, 1) for i in range(count)]
            with mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "cpu"}):
                stack, valid = merge._align_exposures(images)
                expected = merge.hdr_merge(images)
            actual = gpu.hdr_fuse(stack, valid)
            self.assertIsNotNone(actual)
            np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=0)

    def test_panorama_combined_warp_preserves_original_feather(self):
        for matrix, shape in [(np.eye(3), (89, 97)),
                (np.array([[1.003, 0.007, -24.35], [-0.005, .991, -15.6],
                           [0.00002, -0.00001, 1]]), (39, 47)),
                (np.array([[1, 0, 200], [0, 1, -3.4], [0, 0, 1]]), (51, 63))]:
            mapping = transform.ProjectiveTransform(matrix)
            color = transform.warp(self.image, inverse_map=mapping.inverse,
                                   output_shape=shape, order=1, preserve_range=True)
            weight = transform.warp(merge._source_feather(self.image.shape[:2]),
                                    inverse_map=mapping.inverse, output_shape=shape,
                                    order=1, preserve_range=True)
            actual = gpu.panorama_warp(self.image, matrix, shape)
            self.assertIsNotNone(actual)
            np.testing.assert_allclose(actual[..., 3], weight, atol=1e-5, rtol=1e-6)
            np.testing.assert_allclose(actual[..., :3], color * weight[..., None],
                                       atol=0.0003, rtol=1e-5)

    def test_sharpness_and_pyramid_filters_across_tile_boundaries(self):
        with mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "cpu"}):
            expected = merge._sharpness(self.image)
        actual = gpu.sharpness(self.image)
        self.assertIsNotNone(actual)
        np.testing.assert_allclose(actual, expected, atol=2e-7, rtol=0)
        for value in [self.image, self.image[..., 0]]:
            expected = ndimage.gaussian_filter(value, (1, 1, 0) if value.ndim == 3 else 1)[::2, ::2]
            actual = gpu.pyramid_down(value)
            self.assertIsNotNone(actual)
            np.testing.assert_allclose(actual, expected, atol=3e-7, rtol=0)

    def test_resize_odd_shapes_and_reflected_borders(self):
        for value in [self.image, self.image[..., 0], self.image[:1, :2]]:
            shape = (value.shape[0] * 2 - 1, value.shape[1] * 2 - 1, *value.shape[2:])
            expected = transform.resize(value, shape, order=1, preserve_range=True,
                                        anti_aliasing=False)
            actual = gpu.resize(value, shape)
            self.assertIsNotNone(actual)
            np.testing.assert_allclose(actual, expected, atol=8e-6, rtol=0)

    def test_complete_focus_blend_matches_cpu(self):
        blurred = ndimage.gaussian_filter(self.image, (2, 2, 0))
        images = []
        for index in range(3):
            frame = blurred.copy()
            frame[index*25:index*25+39] = self.image[index*25:index*25+39]
            images.append(frame)
        # Hold the CPU registration fixed so this checks GPU pixel operations,
        # independently of RANSAC's stochastic feature sampling.
        fixed_alignment = ([np.eye(3)]*3, (slice(None), slice(None)))
        with mock.patch.object(merge, "_focus_alignment", return_value=fixed_alignment):
            with mock.patch.dict(os.environ, {"LIGHTTABLE_MERGE_ACCELERATION": "cpu"}):
                expected = merge.focus_merge(images)
            actual = merge.focus_merge(images)
        self.assertFalse(gpu._disabled)
        np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=0)


if __name__ == "__main__":
    unittest.main()
