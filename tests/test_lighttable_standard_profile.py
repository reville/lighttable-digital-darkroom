# SPDX-License-Identifier: GPL-3.0-only
"""The bundled LightTable Standard look is exactly what its generator states.

``profiles/build_lighttable_standard.py`` is the definition; the tracked
``.dcp`` is its output. This module regenerates the bytes and compares them,
then checks the stated targets hold in the file the reader sees.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import camera_profile  # noqa: E402
import film_pipeline as fp  # noqa: E402


def _generator():
    path = APP / "profiles" / "build_lighttable_standard.py"
    spec = importlib.util.spec_from_file_location("build_lighttable_standard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _srgb_encode(value):
    value = np.asarray(value, dtype=np.float64)
    return np.where(value <= 0.0031308, value * 12.92,
                    1.055 * np.power(np.maximum(value, 0.0), 1.0 / 2.4) - 0.055)


class LightTableStandardProfileTests(unittest.TestCase):
    def test_tracked_file_matches_the_generator_byte_for_byte(self):
        generator = _generator()
        self.assertEqual(camera_profile.BUNDLED_STANDARD_FILE.read_bytes(),
                         generator.profile_bytes())
        self.assertEqual(generator.OUTPUT, camera_profile.BUNDLED_STANDARD_FILE)

    def test_profile_carries_only_a_look(self):
        profile = camera_profile.read_profile(camera_profile.BUNDLED_STANDARD_FILE)
        self.assertEqual(profile["name"], camera_profile.BUNDLED_STANDARD_LABEL)
        self.assertIn("modelled", profile["copyright"].lower())
        for key in ("colorMatrix1", "colorMatrix2", "forwardMatrix1",
                    "forwardMatrix2", "lookTable", "hueSatMap2", "illuminant2",
                    "cameraModel"):
            self.assertIsNone(profile[key], key)
        self.assertEqual(profile["hueSatMapEncoding"], camera_profile.ENCODING_LINEAR)
        self.assertEqual(profile["unsupported"], [])
        self.assertFalse(profile["approximate"])
        summary = camera_profile.profile_summary(camera_profile.BUNDLED_STANDARD_FILE)
        self.assertTrue(summary["bundled"])
        self.assertFalse(summary["dualIlluminant"])

    def test_tone_curve_meets_its_stated_targets(self):
        generator = _generator()
        curve = camera_profile.read_profile(camera_profile.BUNDLED_STANDARD_FILE)["toneCurve"]
        self.assertEqual(curve.shape, (generator.TONE_SAMPLES, 2))
        self.assertTrue(np.all(np.diff(curve[:, 0]) > 0))
        self.assertTrue(np.all(np.diff(curve[:, 1]) >= 0))
        np.testing.assert_allclose(curve[0], [0.0, 0.0])
        np.testing.assert_allclose(curve[-1], [1.0, 1.0])
        for scene, display in generator.TONE_TARGETS:
            rendered = float(_srgb_encode(np.interp(scene, curve[:, 0], curve[:, 1])))
            self.assertAlmostEqual(rendered, display, delta=0.004, msg=f"target {scene}")
        # The shoulder rolls off: the slope at white is below the mid-tone slope.
        encoded_in = _srgb_encode(curve[:, 0])
        encoded_out = _srgb_encode(curve[:, 1])
        mid = np.searchsorted(curve[:, 0], 0.18)
        mid_slope = (encoded_out[mid + 4] - encoded_out[mid - 4]) / (encoded_in[mid + 4] - encoded_in[mid - 4])
        top_slope = (encoded_out[-1] - encoded_out[-9]) / (encoded_in[-1] - encoded_in[-9])
        self.assertGreater(mid_slope, 1.0)
        self.assertLess(top_slope, mid_slope)

    def test_hue_sat_map_is_modest_and_leaves_grey_alone(self):
        generator = _generator()
        profile = camera_profile.read_profile(camera_profile.BUNDLED_STANDARD_FILE)
        grid = profile["hueSatMap1"]
        self.assertEqual(profile["hueSatDims"], generator.HUE_SAT_DIMS)
        # Grey row: identity. Saturated row: no saturation lift.
        np.testing.assert_allclose(grid[:, :, 0, :],
                                   np.broadcast_to([0.0, 1.0, 1.0], grid[:, :, 0, :].shape))
        np.testing.assert_allclose(grid[:, :, 2, 1], 1.0)
        np.testing.assert_allclose(grid[..., 2], 1.0)
        self.assertLessEqual(float(np.abs(grid[..., 0]).max()), 4.0)
        self.assertLessEqual(float(grid[..., 1].max()), 1.08 + 1e-6)
        self.assertGreaterEqual(float(grid[..., 1].min()), 1.0)
        greys = np.linspace(0.0, 1.0, 9, dtype=np.float32)[:, None].repeat(3, axis=1)[None]
        applied = camera_profile.apply_profile(greys, profile)
        curved = camera_profile.apply_tone_curve(greys, profile["toneCurve"])
        np.testing.assert_allclose(applied, curved, atol=1e-6)
        self.assertTrue(np.allclose(applied[..., 0], applied[..., 1]))

    def test_saved_edits_keep_the_built_in_default(self):
        # The bundled look is the new-photo default through Develop Defaults,
        # not through DEFAULT_PARAMS: an older saved edit cleans to Built-in.
        self.assertEqual(fp.DEFAULT_PARAMS["camera_profile"], "")
        self.assertEqual(fp.clean_params({})["camera_profile"], "")
        self.assertEqual(fp.clean_params({"camera_profile": camera_profile.BUNDLED_STANDARD_NAME})["camera_profile"],
                         camera_profile.BUNDLED_STANDARD_NAME)


if __name__ == "__main__":
    unittest.main()
