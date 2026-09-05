from __future__ import annotations

import unittest

import numpy as np

from bench import denoise_eval


class DenoiseEvaluationTests(unittest.TestCase):
    def test_noise_is_deterministic_and_metrics_reward_cleaner_pixels(self):
        clean = np.full((48, 64, 3), 0.4, dtype=np.float32)
        first = denoise_eval.add_poisson_gaussian(clean, 50, 0.01, 7)
        second = denoise_eval.add_poisson_gaussian(clean, 50, 0.01, 7)
        self.assertTrue(np.array_equal(first, second))
        noisy = denoise_eval.image_metrics(first, clean)
        halfway = denoise_eval.image_metrics((first + clean) / 2, clean)
        self.assertGreater(halfway["psnrDb"], noisy["psnrDb"])
        self.assertGreater(halfway["ssim"], noisy["ssim"])

    def test_flat_patch_delta_e_is_zero_for_identity(self):
        y, x = np.mgrid[:40, :60]
        image = np.stack((x / 59, y / 39, np.full_like(x, 0.3)), axis=2)
        self.assertAlmostEqual(
            denoise_eval.flat_patch_delta_e76(image, image), 0.0, places=7)

    def test_colour_gate_excludes_patches_clipped_by_synthetic_noise(self):
        reference = np.full((20, 40, 3), 0.5, dtype=np.float32)
        reference[:, :20] = 0.0
        candidate = reference.copy()
        candidate[:, :20] = 0.08
        self.assertAlmostEqual(denoise_eval.flat_patch_delta_e76(
            candidate, reference, divisions=(1, 2)), 0.0, places=7)


if __name__ == "__main__":
    unittest.main()
