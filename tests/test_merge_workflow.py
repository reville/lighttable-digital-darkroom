import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import ndimage
from skimage.feature import match_descriptors
from skimage import metrics, transform

import merge_workflow as merge


class MergeWorkflowTests(unittest.TestCase):
    def test_hdr_merge_aligns_brackets_and_recovers_tones(self):
        y, x = np.mgrid[0:80, 0:100].astype(np.float32)
        base = np.stack([x / 99, y / 79, (x + y) / 178], axis=2)
        dark = np.clip(ndimage.shift(base * 0.45, (1.2, -1.0, 0), order=1), 0, 1)
        bright = np.clip(ndimage.shift(base * 1.7, (-0.8, 1.1, 0), order=1), 0, 1)
        result = merge.hdr_merge([dark, base, bright])
        self.assertEqual(result.shape, base.shape)
        self.assertTrue(np.isfinite(result).all())
        self.assertGreater(float(result[60, 75].mean()), float(dark[60, 75].mean()))
        self.assertLess(float(result[60, 75].mean()), float(bright[60, 75].mean()))

    def test_panorama_stitches_overlapping_views(self):
        rng = np.random.default_rng(42)
        scene = rng.random((90, 220, 3), dtype=np.float32)
        scene = ndimage.gaussian_filter(scene, (1.0, 1.0, 0))
        left, right = scene[:, :150], scene[:, 70:220]
        result = merge.panorama_merge([left, right])
        self.assertGreater(result.shape[1], 190)
        self.assertGreaterEqual(result.shape[0], 85)
        self.assertTrue(np.isfinite(result).all())

    def test_panorama_preserves_overlap_and_streams_reloadable_sources(self):
        rng = np.random.default_rng(7)
        scene = ndimage.gaussian_filter(
            rng.random((128, 256, 3), dtype=np.float32), (1.0, 1.0, 0))
        images = [scene[:, :192], scene[:, 64:]]
        loads = [0, 0]

        def loader(index):
            def load():
                loads[index] += 1
                return images[index].copy()
            return load

        progress = []
        result = merge.panorama_merge(
            [loader(0), loader(1)], progress=lambda *value: progress.append(value),
            tile_edge=64)
        self.assertGreater(metrics.peak_signal_noise_ratio(
            scene, result, data_range=1.0), 60.0)
        self.assertEqual(loads, [2, 2])
        self.assertIn("aligning", {item[0] for item in progress})
        self.assertIn("blending", {item[0] for item in progress})

    def test_descriptor_matching_is_bounded_and_matches_reference(self):
        rng = np.random.default_rng(11)
        first = rng.random((37, 16), dtype=np.float32)
        second = np.vstack((first + rng.normal(0, 0.001, first.shape),
                            rng.random((13, 16)))).astype(np.float32)
        expected = match_descriptors(
            first, second, metric="euclidean", max_ratio=0.78,
            cross_check=True)
        actual = merge._match_descriptors_bounded(
            first, second, max_ratio=0.78, block_size=7)
        np.testing.assert_array_equal(actual, expected)

    def test_feature_budget_keeps_spatial_coverage(self):
        y, x = np.mgrid[:10, :20]
        keypoints = np.column_stack((y.ravel(), x.ravel()))
        sigmas = np.linspace(1.0, 3.0, len(keypoints))
        selected = merge._bounded_feature_indices(
            keypoints, sigmas, (10, 20), 40)
        self.assertEqual(len(selected), 40)
        self.assertGreater(len(np.unique(keypoints[selected, 0])), 5)
        self.assertGreater(np.ptp(keypoints[selected, 1]), 15)

    def test_focus_stack_recovers_sharp_bands(self):
        height, width = 192, 224
        y, x = np.mgrid[:height, :width].astype(np.float32)
        checker = ((x // 8 + y // 8) % 2) * 0.10
        sharp = np.stack([
            0.2 + 0.55 * x / (width - 1) + checker,
            0.15 + 0.55 * y / (height - 1) + checker,
            0.2 + 0.3 * (x + y) / (width + height - 2) + checker,
        ], axis=2).astype(np.float32)
        blurred = ndimage.gaussian_filter(sharp, (3.0, 3.0, 0))
        frames = []
        scales = (0.9925, 0.9975, 1.0025, 1.0075)
        center_x, center_y = (width - 1) / 2, (height - 1) / 2
        for index, scale in enumerate(scales):
            frame = blurred.copy()
            y0 = index * 48
            frame[max(0, y0 - 8):min(height, y0 + 64)] = sharp[
                max(0, y0 - 8):min(height, y0 + 64)]
            breathing = transform.ProjectiveTransform(np.array([
                [scale, 0.0, center_x * (1.0 - scale)],
                [0.0, scale, center_y * (1.0 - scale)],
                [0.0, 0.0, 1.0],
            ]))
            frames.append(transform.warp(
                frame, inverse_map=breathing.inverse,
                preserve_range=True).astype(np.float32))
        result = merge.focus_merge(frames)
        reference = transform.ProjectiveTransform(np.array([
            [scales[2], 0.0, center_x * (1.0 - scales[2])],
            [0.0, scales[2], center_y * (1.0 - scales[2])],
            [0.0, 0.0, 1.0],
        ]))
        expected = transform.warp(
            sharp, inverse_map=reference.inverse,
            preserve_range=True).astype(np.float32)
        height_delta = expected.shape[0] - result.shape[0]
        width_delta = expected.shape[1] - result.shape[1]
        expected = expected[
            height_delta // 2:height_delta // 2 + result.shape[0],
            width_delta // 2:width_delta // 2 + result.shape[1]]
        self.assertGreater(metrics.peak_signal_noise_ratio(
            expected, result, data_range=1.0), 32.0)
        result_energy = float(np.mean(np.abs(ndimage.laplace(result))))
        sharp_energy = float(np.mean(np.abs(ndimage.laplace(expected))))
        self.assertGreater(result_energy, sharp_energy * 0.95)
        self.assertTrue(np.isfinite(result).all())

    def test_focus_stack_accepts_rails_and_refuses_the_sixty_first_frame(self):
        frame = np.zeros((12, 12, 3), dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "2 to 60"):
            merge.focus_merge([frame] * 61)
        alignment = []
        result = merge.focus_merge([frame] * 30, alignment=alignment.append)
        self.assertEqual(result.shape, frame.shape)
        self.assertEqual(alignment[-1], 0)

    def test_focus_stack_streams_reloadable_sources(self):
        # A flat frame forces the no-feature fallback. It must reuse the
        # reduced alignment image instead of reading each full source again.
        frame = np.full((128, 144, 3), 0.4, dtype=np.float32)
        loads = [0, 0]

        def loader(index):
            def load():
                loads[index] += 1
                return frame.copy()
            return load

        progress = []
        result = merge.focus_merge(
            [loader(0), loader(1)],
            progress=lambda *value: progress.append(value))
        self.assertEqual(result.shape, frame.shape)
        self.assertEqual(loads, [3, 3])
        self.assertEqual(
            {item[0] for item in progress},
            {"aligning", "analyzing", "blending"})

    def test_output_names_are_safe_and_collision_safe(self):
        self.assertEqual(merge.safe_output_name("trip/final", "hdr"), "trip_final.tif")
        self.assertEqual(merge.safe_output_name("", "focus"), "Focus.tif")

    def test_an_orphan_manifest_reserves_its_matching_merge_name(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "HDR.tif"
            Path(str(output) + ".lighttable.json").touch()
            self.assertEqual(merge.collision_path(output).name, "HDR-2.tif")


if __name__ == "__main__":
    unittest.main()
