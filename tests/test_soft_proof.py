import unittest

import numpy as np

import soft_proof


class SoftProofTests(unittest.TestCase):
    def test_matte_target_marks_and_compresses_saturated_colors(self):
        image = np.array([[[1.0, 0.0, 0.0], [0.5, 0.5, 0.5]]], dtype=np.float32)
        proof, warning = soft_proof.apply(image, "matte")
        self.assertTrue(bool(warning[0, 0]))
        self.assertFalse(bool(warning[0, 1]))
        self.assertLess(float(proof[0, 0].max() - proof[0, 0].min()), 1.0)

    def test_paper_simulation_lifts_black_and_lowers_white(self):
        image = np.array([[[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]], dtype=np.float32)
        proof, _ = soft_proof.apply(image, "matte", simulate_paper=True)
        self.assertGreater(float(proof[0, 0].mean()), 0.0)
        self.assertLess(float(proof[0, 1].mean()), 1.0)

    def test_display_p3_contains_encoded_srgb_primaries(self):
        image = np.eye(3, dtype=np.float32).reshape(1, 3, 3)
        _, warning = soft_proof.apply(image, "display_p3")
        self.assertFalse(bool(warning.any()))


    def test_apply_icc_proof_with_srgb_is_near_identity(self):
        # Neutral ramp
        ramp = np.linspace(0.1, 0.9, 9, dtype=np.float32)[:, None, None]
        image = np.repeat(ramp, 3, axis=2)
        proof, warning = soft_proof.apply_icc_proof(image, "srgb")
        np.testing.assert_allclose(proof, image, atol=0.01)
        self.assertFalse(bool(warning.any()))

    def test_list_system_icc_profiles_returns_descriptions(self):
        profiles = soft_proof.list_system_icc_profiles()
        self.assertIsInstance(profiles, list)
        if profiles:
            first = profiles[0]
            self.assertIn("path", first)
            self.assertIn("name", first)
            self.assertTrue(len(first["name"]) > 0)

    def test_soft_proof_intents(self):
        image = np.array([[[0.8, 0.2, 0.1]]], dtype=np.float32)
        for intent in ("perceptual", "relative_colorimetric", "saturation", "absolute_colorimetric"):
            proof, _ = soft_proof.apply_icc_proof(image, "srgb", intent=intent)
            self.assertEqual(proof.shape, (1, 1, 3))


if __name__ == "__main__":
    unittest.main()
