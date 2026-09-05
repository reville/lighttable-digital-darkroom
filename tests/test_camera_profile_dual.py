"""Tests for dual-illuminant camera profile interpolation."""
from __future__ import annotations

import unittest
import numpy as np

import camera_profile


class DualIlluminantTests(unittest.TestCase):
    def test_illuminant_temperature_mapping(self):
        self.assertEqual(camera_profile.illuminant_temperature(17), 2856.0) # Std Light A
        self.assertEqual(camera_profile.illuminant_temperature(21), 6504.0) # D65
        self.assertEqual(camera_profile.illuminant_temperature(1), 5500.0)  # Daylight
        self.assertEqual(camera_profile.illuminant_temperature(3), 3200.0)  # Tungsten
        self.assertEqual(camera_profile.illuminant_temperature(None), 5500.0)
        self.assertEqual(camera_profile.illuminant_temperature(999), 5500.0)

    def test_interpolate_hue_sat_maps_at_endpoints(self):
        # Table 1: shifts red by 10 deg, scales sat by 1.0, val by 1.0
        dims = (1, 1, 1)
        map1 = np.array([[[[10.0, 1.0, 1.0]]]], dtype=np.float32)
        # Table 2: shifts red by 30 deg, scales sat by 1.2, val by 0.9
        map2 = np.array([[[[30.0, 1.2, 0.9]]]], dtype=np.float32)

        ill1 = 17 # 2856 K
        ill2 = 21 # 6504 K

        # At or below T1 -> exactly map1
        interp_low = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, 2800.0)
        np.testing.assert_allclose(interp_low, map1, atol=1e-5)

        interp_exact_t1 = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, 2856.0)
        np.testing.assert_allclose(interp_exact_t1, map1, atol=1e-5)

        # At or above T2 -> exactly map2
        interp_high = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, 7000.0)
        np.testing.assert_allclose(interp_high, map2, atol=1e-5)

        interp_exact_t2 = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, 6504.0)
        np.testing.assert_allclose(interp_exact_t2, map2, atol=1e-5)

    def test_interpolate_hue_sat_maps_midway_in_inverse_temperature(self):
        dims = (1, 1, 1)
        map1 = np.array([[[[10.0, 1.0, 1.0]]]], dtype=np.float32)
        map2 = np.array([[[[20.0, 2.0, 0.5]]]], dtype=np.float32)

        ill1 = 17 # 2856 K
        ill2 = 21 # 6504 K

        # Midpoint in 1/T space
        inv_t1 = 1.0 / 2856.0
        inv_t2 = 1.0 / 6504.0
        mid_inv_t = 0.5 * (inv_t1 + inv_t2)
        mid_t = 1.0 / mid_inv_t

        interp_mid = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, mid_t)
        # Hue should be 15.0, Sat 1.5, Val 0.75
        self.assertAlmostEqual(float(interp_mid[0, 0, 0, 0]), 15.0, places=4)
        self.assertAlmostEqual(float(interp_mid[0, 0, 0, 1]), 1.5, places=4)
        self.assertAlmostEqual(float(interp_mid[0, 0, 0, 2]), 0.75, places=4)

    def test_shortest_arc_hue_interpolation(self):
        # Map 1: +5 deg
        # Map 2: +355 deg (-5 deg)
        map1 = np.array([[[[5.0, 1.0, 1.0]]]], dtype=np.float32)
        map2 = np.array([[[[355.0, 1.0, 1.0]]]], dtype=np.float32)

        ill1 = 17
        ill2 = 21

        inv_t1 = 1.0 / 2856.0
        inv_t2 = 1.0 / 6504.0
        mid_t = 1.0 / (0.5 * (inv_t1 + inv_t2))

        interp = camera_profile.interpolate_hue_sat_maps(map1, map2, ill1, ill2, mid_t)
        # Midway between +5 and -5 deg should be 0 deg (or 360 deg)
        hue = float(interp[0, 0, 0, 0])
        self.assertTrue(abs(hue) < 1e-3 or abs(hue - 360.0) < 1e-3)

    def test_apply_profile_uses_temperature_when_dual_illuminants_present(self):
        profile = {
            "name": "Test Dual",
            "hueSatDims": (1, 1, 1),
            "hueSatMap1": np.array([[[[0.0, 1.0, 1.0]]]], dtype=np.float32),
            "hueSatMap2": np.array([[[[60.0, 1.0, 1.0]]]], dtype=np.float32),
            "illuminant1": 17,
            "illuminant2": 21,
        }
        pixels = np.array([[[1.0, 0.0, 0.0]]], dtype=np.float32) # Pure Red

        # Low temp -> 0 deg shift (stays pure red)
        rendered_low = camera_profile.apply_profile(pixels, profile, temperature=2856.0)
        np.testing.assert_allclose(rendered_low, pixels, atol=1e-3)

        # High temp -> 60 deg shift (pure red shifts to yellow)
        rendered_high = camera_profile.apply_profile(pixels, profile, temperature=6504.0)
        # Red shifted 60 deg in HSV is yellow [1, 1, 0]
        np.testing.assert_allclose(rendered_high, [[[1.0, 1.0, 0.0]]], atol=1e-3)


if __name__ == "__main__":
    unittest.main()
