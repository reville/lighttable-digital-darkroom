# SPDX-License-Identifier: GPL-3.0-only
"""Camera-to-scene colour from a profile's matrices, per the DNG specification.

The pure maths is checked against constructions whose answers are known by
design: a "camera" whose matrices are ProPhoto's own must produce an identity
transform, a forward matrix must land the balanced neutral on equal RGB, the
illuminant weights must agree with the hue/sat table blend, and the capture
temperature iteration must recover the illuminant a neutral was built from.
The end-to-end check decodes the synthetic DNG fixture with a profile that
carries the fixture's own ColorMatrix1 and compares against LibRaw's own
conversion from the same matrix.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import camera_calibration as cal  # noqa: E402
import camera_profile  # noqa: E402
from camera_profile_write import write_profile  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "raw" / "synthetic-bayer-rggb.dng"

# ProPhoto as the "camera": XYZ D50 -> camera is the inverse of the ROMM
# matrix, so a balanced decode through it must be an identity transform.
PROPHOTO_AS_CAMERA = np.linalg.inv(cal.PROPHOTO_TO_XYZ_D50)
D65_XYZ = np.array([0.95047, 1.0, 1.08883])
A_XYZ = np.array([1.09850, 1.0, 0.35585])


def _rawpy():
    try:
        import rawpy
    except ImportError:  # pragma: no cover - environment without rawpy
        return None
    return rawpy


class IlluminantBlendTests(unittest.TestCase):
    def test_weights_match_the_hue_sat_table_blend(self):
        for temperature in (2000.0, 2856.0, 3500.0, 4800.0, 6504.0, 9000.0):
            weight = cal.illuminant_weight(17, 21, temperature)
            table = camera_profile.interpolate_hue_sat_maps(
                np.array([[[[0.0, 1.0, 1.0]]]]), np.array([[[[0.0, 2.0, 1.0]]]]),
                17, 21, temperature)
            self.assertAlmostEqual(weight * 1.0 + (1.0 - weight) * 2.0,
                                   float(table[0, 0, 0, 1]), places=5)
        self.assertEqual(cal.illuminant_weight(17, 21, 2856.0), 1.0)
        self.assertEqual(cal.illuminant_weight(17, 21, 6504.0), 0.0)
        # Tag order does not matter: calibration 1 is still calibration 1.
        self.assertEqual(cal.illuminant_weight(21, 17, 6504.0), 1.0)
        self.assertEqual(cal.illuminant_weight(21, 17, 2856.0), 0.0)
        self.assertEqual(cal.illuminant_weight(21, 21, 4000.0), 1.0)

    def test_matrices_blend_entrywise_in_reciprocal_temperature(self):
        m1 = np.eye(3)
        m2 = np.full((3, 3), 2.0)
        mid = 1.0 / (0.5 * (1.0 / 2856.0 + 1.0 / 6504.0))
        np.testing.assert_allclose(cal.interpolate_matrices(m1, m2, 17, 21, mid),
                                   0.5 * m1 + 0.5 * m2, atol=1e-9)
        np.testing.assert_allclose(cal.interpolate_matrices(m1, None, 17, None, mid), m1)
        np.testing.assert_allclose(cal.interpolate_matrices(m1, m2, 17, 21, 1500.0), m1)
        np.testing.assert_allclose(cal.interpolate_matrices(m1, m2, 17, 21, 12000.0), m2)


class TemperatureTests(unittest.TestCase):
    def test_mccamy_recovers_the_standard_illuminants(self):
        self.assertAlmostEqual(cal.xy_to_cct(*cal.xyz_to_xy(D65_XYZ)), 6504.0, delta=15.0)
        self.assertAlmostEqual(cal.xy_to_cct(*cal.xyz_to_xy(A_XYZ)), 2856.0, delta=15.0)
        self.assertEqual(cal.xy_to_cct(0.3320, 0.1858), cal.MAX_TEMPERATURE)

    def test_neutral_from_multipliers(self):
        np.testing.assert_allclose(
            cal.neutral_from_multipliers([1.9230769, 1.0, 1.4705882, 0.0]),
            [0.52, 1.0, 0.68], atol=1e-6)
        np.testing.assert_allclose(
            cal.neutral_from_multipliers([0.0, 0.0, 0.0, 0.0], [2.0, 1.0, 4.0]),
            [0.5, 1.0, 0.25], atol=1e-9)
        np.testing.assert_allclose(cal.neutral_from_multipliers(None, None), [1.0, 1.0, 1.0])
        np.testing.assert_allclose(cal.neutral_from_multipliers([float("nan"), 1, 1]), [1.0, 1.0, 1.0])

    def test_capture_temperature_iteration_recovers_the_illuminant(self):
        # Two calibrations of the same "camera": an exact one at A and one
        # deliberately skewed at D65, so the blended matrix really depends on
        # the temperature being solved for.
        skewed = PROPHOTO_AS_CAMERA @ np.diag([1.08, 1.0, 0.92])
        profile = {"colorMatrix1": PROPHOTO_AS_CAMERA, "colorMatrix2": skewed,
                   "illuminant1": 17, "illuminant2": 21}
        for white, expected in ((A_XYZ, 2856.0), (D65_XYZ, 6504.0)):
            # The neutral a camera would report for this white is the white
            # taken through the calibration that applies at its temperature.
            matrix = cal.interpolate_matrices(PROPHOTO_AS_CAMERA, skewed, 17, 21, expected)
            neutral = matrix @ white
            neutral = neutral / neutral[1]
            estimate = cal.estimate_capture_temperature(profile, neutral)
            self.assertAlmostEqual(estimate, expected, delta=25.0)
        self.assertEqual(cal.estimate_capture_temperature({}, [1, 1, 1]),
                         cal.DEFAULT_CAPTURE_TEMPERATURE)
        with self.assertRaises(cal.CalibrationError):
            cal.estimate_capture_temperature(profile, [1.0, 0.0, 1.0])

    def test_needs_capture_temperature_only_for_dual_illuminant_profiles(self):
        self.assertFalse(cal.needs_capture_temperature(None))
        self.assertFalse(cal.needs_capture_temperature({"colorMatrix1": np.eye(3), "illuminant1": 21}))
        self.assertTrue(cal.needs_capture_temperature(
            {"hueSatMap1": 1, "hueSatMap2": 1, "illuminant1": 17, "illuminant2": 21}))
        self.assertTrue(cal.needs_capture_temperature(
            {"colorMatrix1": np.eye(3), "colorMatrix2": np.eye(3), "illuminant1": 17, "illuminant2": 21}))

    def test_temperature_memory_is_bounded(self):
        cal.forget_capture_temperatures()
        for index in range(cal._TEMPERATURES_LIMIT + 10):
            cal.remember_capture_temperature(("id", index), 5000.0 + index)
        self.assertIsNone(cal.recall_capture_temperature(("id", 0)))
        self.assertEqual(cal.recall_capture_temperature(("id", cal._TEMPERATURES_LIMIT + 9)),
                         5000.0 + cal._TEMPERATURES_LIMIT + 9)
        cal.forget_capture_temperatures()


class CameraToProPhotoTests(unittest.TestCase):
    def test_forward_matrix_path_is_identity_for_a_prophoto_camera(self):
        profile = {"colorMatrix1": PROPHOTO_AS_CAMERA,
                   "forwardMatrix1": cal.PROPHOTO_TO_XYZ_D50, "illuminant1": 23}
        matrix = cal.camera_to_prophoto(profile, [0.6, 1.0, 0.8], 5000.0)
        np.testing.assert_allclose(matrix, np.eye(3), atol=1e-6)
        np.testing.assert_allclose(matrix @ np.ones(3), np.ones(3), atol=1e-9)

    def test_colour_matrix_path_adapts_the_scene_white_to_d50(self):
        profile = {"colorMatrix1": PROPHOTO_AS_CAMERA, "illuminant1": 21}
        # A D50 neutral needs no adaptation: identity.
        neutral = PROPHOTO_AS_CAMERA @ cal.D50_XYZ
        matrix = cal.camera_to_prophoto(profile, neutral / neutral[1], 5003.0)
        np.testing.assert_allclose(matrix, np.eye(3), atol=1e-9)
        # A tungsten neutral: the balanced neutral still lands on equal RGB
        # and a saturated colour is adapted rather than merely scaled.
        neutral = PROPHOTO_AS_CAMERA @ A_XYZ
        matrix = cal.camera_to_prophoto(profile, neutral / neutral[1], 2856.0)
        np.testing.assert_allclose(matrix @ np.ones(3), np.ones(3), atol=1e-9)
        self.assertGreater(float(np.abs(matrix - np.eye(3)).max()), 0.01)

    def test_dual_illuminant_matrices_blend_before_use(self):
        warm = PROPHOTO_AS_CAMERA
        cool = PROPHOTO_AS_CAMERA @ np.diag([1.1, 1.0, 0.9])
        profile = {"colorMatrix1": warm, "colorMatrix2": cool, "illuminant1": 17, "illuminant2": 21,
                   "forwardMatrix1": cal.PROPHOTO_TO_XYZ_D50,
                   "forwardMatrix2": cal.PROPHOTO_TO_XYZ_D50 @ np.diag([0.9, 1.0, 1.1])}
        at_warm = cal.camera_to_prophoto(profile, [1.0, 1.0, 1.0], 2856.0)
        at_cool = cal.camera_to_prophoto(profile, [1.0, 1.0, 1.0], 6504.0)
        self.assertFalse(np.allclose(at_warm, at_cool))
        np.testing.assert_allclose(at_warm, np.eye(3), atol=1e-6)

    def test_rejects_unusable_profiles(self):
        with self.assertRaises(cal.CalibrationError):
            cal.camera_to_prophoto({}, [1, 1, 1], 5000.0)
        with self.assertRaises(cal.CalibrationError):
            cal.camera_to_prophoto({"colorMatrix1": np.zeros((3, 3))}, [1, 1, 1], 5000.0)
        with self.assertRaises(cal.CalibrationError):
            cal.camera_to_prophoto({"colorMatrix1": PROPHOTO_AS_CAMERA}, [1, -1, 1], 5000.0)
        self.assertFalse(cal.has_calibration(None))
        self.assertFalse(cal.has_calibration({"forwardMatrix1": np.eye(3)}))

    def test_convert_camera_native_keeps_dtype_and_clips(self):
        matrix = np.array([[1.5, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -0.5]])
        codes = np.array([[[60000, 30000, 30000]]], dtype=np.uint16)
        out = cal.convert_camera_native(codes, matrix)
        self.assertEqual(out.dtype, np.uint16)
        np.testing.assert_array_equal(out, [[[65535, 30000, 0]]])
        floats = np.array([[[0.5, 0.5, 0.5]]], dtype=np.float32)
        out = cal.convert_camera_native(floats, matrix)
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_allclose(out, [[[0.75, 0.5, 0.0]]], atol=1e-6)
        with self.assertRaises(cal.CalibrationError):
            cal.convert_camera_native(np.zeros((2, 2, 4)), matrix)


@unittest.skipUnless(_rawpy() and FIXTURE.is_file(),
                     "needs rawpy and the synthetic DNG fixture")
class FixtureDecodeTests(unittest.TestCase):
    """The decode path with a profile carrying the fixture's own matrix."""

    def setUp(self):
        import color_pipeline
        self.pipeline = color_pipeline
        self._resolver = color_pipeline.CAMERA_PROFILE_RESOLVER
        color_pipeline._read_camera_profile.cache_clear()
        cal.forget_capture_temperatures()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def tearDown(self):
        self.pipeline.CAMERA_PROFILE_RESOLVER = self._resolver
        self.pipeline._read_camera_profile.cache_clear()
        cal.forget_capture_temperatures()

    def _profile(self, name, **kwargs):
        import make_raw_fixtures
        path = write_profile(Path(self.folder.name) / name,
                             colour_matrix=make_raw_fixtures.COLOR_MATRIX,
                             illuminant1=21, **kwargs)
        self.pipeline.CAMERA_PROFILE_RESOLVER = {path.name: path}.get
        import film_pipeline as fp
        return fp.clean_params({"camera_profile": path.name})

    def test_profile_matrix_replaces_the_decoders_and_agrees_with_it(self):
        import make_raw_fixtures
        params = self._profile("Synthetic Sensor Standard.dcp")
        theirs = self.pipeline.decode_raw(FIXTURE, {}).astype(np.float64) / 65535.0
        ours = self.pipeline.decode_raw(FIXTURE, params).astype(np.float64) / 65535.0
        self.assertEqual(ours.shape, theirs.shape)
        self.assertFalse(np.array_equal(ours, theirs))
        # LibRaw builds its conversion from the same ColorMatrix1 (without a
        # D50 adaptation and with its own row normalisation), so the two
        # renderings agree closely without being identical: measured mean
        # difference 0.018 and correlation 0.998 on this fixture.
        difference = np.abs(ours - theirs)
        self.assertLess(float(difference.mean()), 0.03)
        self.assertGreater(float(np.corrcoef(ours.ravel(), theirs.ravel())[0, 1]), 0.995)
        # Both balance the as-shot neutral to equal RGB, so the channel means
        # of the whole frame keep the same ordering and proportions.
        ratio = ours.reshape(-1, 3).mean(axis=0) / theirs.reshape(-1, 3).mean(axis=0)
        self.assertLess(float(np.abs(ratio - 1.0).max()), 0.08)
        # The camera-native demosaic is a distinct cache entry from LibRaw's
        # ProPhoto one: both decodes stay reproducible from cache.
        np.testing.assert_array_equal(
            self.pipeline.decode_raw(FIXTURE, params), (ours * 65535.0).round().astype(np.uint16))
        np.testing.assert_array_equal(
            self.pipeline.decode_raw(FIXTURE, {}), (theirs * 65535.0).round().astype(np.uint16))
        self.assertIsNotNone(make_raw_fixtures.AS_SHOT_NEUTRAL)

    def test_capture_temperature_is_remembered_for_the_develop(self):
        import make_raw_fixtures
        params = self._profile("Synthetic Sensor Dual.dcp",
                               colour_matrix_2=make_raw_fixtures.COLOR_MATRIX,
                               illuminant2=17)
        self.assertIsNone(self.pipeline.capture_temperature_for(FIXTURE, {}))
        self.pipeline.decode_raw(FIXTURE, params)
        remembered = self.pipeline.capture_temperature_for(FIXTURE, params)
        self.assertIsNotNone(remembered)
        profile = self.pipeline.camera_profile_for(params)
        expected = cal.estimate_capture_temperature(
            profile, cal.neutral_from_multipliers([1.0 / n for n in make_raw_fixtures.AS_SHOT_NEUTRAL]))
        self.assertAlmostEqual(remembered, expected, delta=1.0)
        # Forgotten by the decode cache, it is re-estimated from the header.
        cal.forget_capture_temperatures()
        self.assertAlmostEqual(self.pipeline.capture_temperature_for(FIXTURE, params),
                               expected, delta=1.0)
        # Daylight-based modes balance by the daylight multipliers instead.
        daylight = self.pipeline.capture_temperature_for(FIXTURE, dict(params, wb_mode="daylight"))
        self.assertIsNotNone(daylight)
        self.assertNotAlmostEqual(daylight, expected, delta=1.0)

    def test_a_profile_without_matrices_leaves_the_decode_alone(self):
        import film_pipeline as fp
        path = write_profile(Path(self.folder.name) / "Look Only.dcp",
                             tone_curve=[[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]])
        self.pipeline.CAMERA_PROFILE_RESOLVER = {path.name: path}.get
        params = fp.clean_params({"camera_profile": path.name})
        np.testing.assert_array_equal(self.pipeline.decode_raw(FIXTURE, params),
                                      self.pipeline.decode_raw(FIXTURE, {}))
        bundled = fp.clean_params({"camera_profile": camera_profile.BUNDLED_STANDARD_NAME})
        np.testing.assert_array_equal(self.pipeline.decode_raw(FIXTURE, bundled),
                                      self.pipeline.decode_raw(FIXTURE, {}))


if __name__ == "__main__":
    unittest.main()
