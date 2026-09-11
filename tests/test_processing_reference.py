# SPDX-License-Identifier: GPL-3.0-only
"""Checks that the correctness gate rejects invalid output and wrong physics."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import processing_reference
from processing_reference import (cli_stages, compare_stage, difference_metrics, grain_variance,
                                  measured_curves, numpy_develop, target_linear)


class ProcessingReferenceTests(unittest.TestCase):
    def test_previous_dumps_cannot_hide_missing_current_output(self):
        import subprocess
        import tifffile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "engine/data").mkdir(parents=True)
            source = root / "source.tif"
            tifffile.imwrite(source, np.zeros((2, 3, 3), dtype=np.uint16), photometric="rgb")
            output = root / "run"
            output.mkdir()
            tifffile.imwrite(output / "output.tif", np.zeros((2, 3, 3), dtype=np.float32), photometric="rgb")
            for stage in processing_reference.STAGES:
                np.zeros((2, 3, 3)).tofile(output / f"{stage}.f64")
            response = subprocess.CompletedProcess([], 0, '{"id":1,"ok":true,"backend":"CPU (rayon)"}\n', '')
            def writes_final_but_omits_stage(*_args, **_kwargs):
                tifffile.imwrite(output / "output.tif", np.zeros((2, 3, 3), dtype=np.float32), photometric="rgb")
                return response
            fake_bridge = SimpleNamespace(rust_params_json=lambda _: {}, POSITIVE_STOCKS=set())
            with patch.object(processing_reference, "APP", root), \
                 patch.object(processing_reference, "_runtime", return_value=(fake_bridge, None, None)), \
                 patch.object(processing_reference.subprocess, "run", side_effect=writes_final_but_omits_stage):
                with self.assertRaisesRegex(RuntimeError, "missing required film_exposure diagnostic buffer"):
                    cli_stages(source, {"stock": "test", "paper": "test"}, output, binary=sys.executable)
            self.assertTrue((output / "output.tif").exists())
            self.assertFalse(any(output.glob("*.f64")))

    def test_target_contains_black_white_edges_and_identical_uint16_samples(self):
        target = target_linear()
        self.assertEqual(target.shape, (128, 192, 3))
        self.assertTrue(np.any(np.all(target == 0, axis=-1)))
        self.assertTrue(np.any(np.all(target == 1, axis=-1)))
        np.testing.assert_array_equal(target, np.rint(target * 65535) / 65535)
        self.assertGreater(np.max(np.abs(np.diff(target, axis=1))), .8)

    def test_bad_pixels_and_wrong_shape_cannot_pass(self):
        valid = np.zeros((2, 3, 3))
        for invalid in (np.full_like(valid, np.nan), np.full_like(valid, np.inf),
                        np.zeros((3, 2, 3)), np.zeros((0, 0, 3))):
            with self.subTest(shape=invalid.shape), self.assertRaises(ValueError):
                difference_metrics(valid, invalid)

    def test_pixel_local_regression_fails_even_when_mean_is_small(self):
        correct = np.zeros((100, 100, 3))
        damaged = correct.copy()
        damaged[50, 50, 1] = .2
        with tempfile.TemporaryDirectory() as directory:
            record = compare_stage("damaged", correct, damaged, directory,
                                   tolerance={"mean": .001, "p95": .001, "max": .01}, units="density")
            self.assertEqual(record["status"], "fail")
            self.assertLess(record["metrics"]["mean"], .001)
            self.assertEqual(record["metrics"]["max"], .2)
            self.assertTrue(Path(record["artifacts"]["actual"]).is_file())

    def test_curve_oracle_checks_foot_shoulders_channel_order_and_gamma(self):
        # Deliberately asymmetric measured curves: swapping channels, missing
        # base subtraction, or applying gamma to density all produce failures.
        curves = np.array([[.2, .4, .1], [1.2, 2.4, 3.1], [2.2, 4.4, 6.1]])
        exposures = np.array([[[-2, -2, -2], [-.25, -.25, -.25], [2, 2, 2]]])
        expected = np.array([[[0, 0, 0], [.5, 1, 1.5], [2, 4, 6]]])
        np.testing.assert_allclose(numpy_develop(exposures, [-1, 0, 1], curves, gamma=2), expected)
        self.assertFalse(np.allclose(numpy_develop(exposures, [-1, 0, 1], curves[:, ::-1], gamma=2), expected))

    def test_curve_oracle_rejects_unordered_or_missing_profile_data(self):
        for axis, curves in (([0, 0], np.ones((2, 3))),
                             ([0, 1], np.array([[np.nan] * 3, [1] * 3]))):
            with self.assertRaises(ValueError):
                numpy_develop(np.ones((1, 1, 3)), axis, curves)

    def test_development_time_uses_nearest_measured_family_not_column_zero(self):
        profile = {"info": {"channel_model": "bw"}, "data": {
            "log_exposure": [-1, 0, 1], "development_time": [4, 6.5, 12],
            "density_curves": [[.1, .2, .3], [.4, .8, 1.2], [.7, 1.4, 2.1]]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile))
            _, nominal = measured_curves(path)
            _, long = measured_curves(path, 11)
            np.testing.assert_array_equal(nominal[:, 0], [.2, .8, 1.4])
            np.testing.assert_array_equal(long, np.repeat(np.array([[.3], [1.2], [2.1]]), 3, axis=1))

    def test_grain_moment_oracle_agrees_with_independent_random_draws(self):
        # NumPy's generator samples the actual Poisson -> Binomial construction;
        # the oracle computes its moments algebraically and never calls an RNG.
        rng = np.random.default_rng(68191)
        density, maximum, particles, uniformity = .7, 2.2, 70, .93
        probability = density / maximum
        saturation = 1 - probability * uniformity * (1 - 1e-6)
        seeds = rng.poisson(particles / saturation, 200_000)
        samples = rng.binomial(seeds, probability) * maximum / particles * saturation
        expected = grain_variance(density, maximum, 0, 1, np.sqrt(particles), uniformity, 0)
        self.assertLess(abs(samples.var() / expected - 1), .015)
        self.assertLess(abs(samples.mean() - density), .001)
        blurred = grain_variance(density, maximum, 0, 1, np.sqrt(particles), uniformity, .65)
        self.assertLess(blurred, expected)
        self.assertGreater(blurred, 0)


if __name__ == "__main__":
    unittest.main()
