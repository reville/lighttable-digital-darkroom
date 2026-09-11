# SPDX-License-Identifier: GPL-3.0-only
"""The capture stage: white balance, highlight recovery, demosaic, denoise.

These controls run in the RAW decoder, before the film model and before any
display-referred grade. A rendered TIFF cannot reach them, so the rest of the
processing gates leave them completely untested. `film_semantics` marks them
`needs-raw` and points here.

The fixtures are synthetic DNGs built by `make_raw_fixtures.py`, so the suite
runs anywhere without shipping a camera original. Coverage of real camera
formats stays with the RAW journey layers in docs/journey-testing.md; what is
checked here is that each capture control does what its name says.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

FIXTURES = Path(__file__).parent / "fixtures" / "raw"
BAYER = FIXTURES / "synthetic-bayer-rggb.dng"
LINEAR = FIXTURES / "synthetic-linear.dng"
# Regions of the synthetic scene, in decoded pixel coordinates.
CLIPPED = (slice(8, 20), slice(32, 48))
FLAT = (slice(30, 42), slice(10, 38))


def _rawpy():
    try:
        import rawpy
    except ImportError:  # pragma: no cover - environment without rawpy
        return None
    return rawpy


@unittest.skipUnless(_rawpy() and BAYER.is_file(),
                     "needs rawpy and the synthetic DNG fixtures")
class RawCaptureControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import color_pipeline
        cls.pipeline = color_pipeline

    def decode(self, params, path=BAYER):
        return self.pipeline.decode_raw(path, params).astype(np.float64)

    def warmth(self, image):
        """Red against blue: rises as the rendering gets warmer."""
        return float(image[..., 0].mean() / max(image[..., 2].mean(), 1.0))

    def test_both_fixtures_decode_to_the_expected_shape(self):
        for path in (BAYER, LINEAR):
            with self.subTest(fixture=path.name):
                decoded = self.decode({}, path)
                self.assertEqual(decoded.shape, (48, 64, 3))
                self.assertTrue(np.isfinite(decoded).all())
                self.assertGreater(decoded.max(), decoded.min(),
                                   "the decode collapsed to a single value")

    def test_a_mosaic_and_a_linear_dng_agree_on_the_same_scene(self):
        """Demosaicing must reconstruct roughly the scene the mosaic sampled."""
        mosaic = self.decode({"wb_mode": "as_shot"}, BAYER)
        linear = self.decode({"wb_mode": "as_shot"}, LINEAR)
        # Interpolation cannot be exact, but the two must agree on the whole
        # frame's colour balance. A swapped CFA pattern would not.
        self.assertAlmostEqual(self.warmth(mosaic), self.warmth(linear),
                               delta=0.25)

    def test_white_balance_presets_are_ordered_by_colour_temperature(self):
        """A warmer preset must render warmer than a cooler one."""
        ordered = ["tungsten", "fluorescent", "daylight", "cloudy", "shade"]
        warmth = [self.warmth(self.decode({"wb_mode": mode})) for mode in ordered]
        self.assertEqual(warmth, sorted(warmth),
                         f"presets out of order: {dict(zip(ordered, warmth))}")

    def test_custom_white_balance_temperature_warms_the_render(self):
        warmth = [self.warmth(self.decode(
            {"wb_mode": "custom", "wb_temperature": kelvin, "wb_tint": 0.0}))
            for kelvin in (2500.0, 4000.0, 5500.0, 7500.0, 9000.0)]
        self.assertEqual(warmth, sorted(warmth),
                         f"temperature did not warm monotonically: {warmth}")

    def test_custom_white_balance_tint_moves_green_against_magenta(self):
        def green(value):
            image = self.decode({"wb_mode": "custom", "wb_temperature": 5500.0,
                                 "wb_tint": value})
            return float(image[..., 1].mean()
                         / max((image[..., 0].mean() + image[..., 2].mean()) / 2, 1.0))
        readings = [green(value) for value in (-1.0, -0.5, 0.0, 0.5, 1.0)]
        # Positive tint is magenta: the green ratio must fall, not merely
        # change monotonically in either direction.
        self.assertEqual(readings, sorted(readings, reverse=True),
                         f"positive tint must lower the green ratio: {readings}")

    def test_as_shot_differs_from_auto_and_from_a_preset(self):
        """Each mode must be a real choice, not the same decode relabelled."""
        modes = {mode: self.decode({"wb_mode": mode})
                 for mode in ("as_shot", "auto", "daylight", "tungsten")}
        warmth = {mode: round(self.warmth(image), 4)
                  for mode, image in modes.items()}
        self.assertEqual(len(set(warmth.values())), len(warmth),
                         f"white balance modes are not distinct: {warmth}")

    def test_highlight_recovery_modes_treat_the_clipped_block_differently(self):
        means = {mode: self.decode({"raw_highlight_recovery": mode})[CLIPPED].mean()
                 for mode in ("off", "blend", "reconstruct")}
        self.assertEqual(len(set(round(value) for value in means.values())), 3,
                         f"recovery modes are indistinguishable: {means}")
        self.assertGreater(means["off"], means["blend"],
                           "clipping should leave the block brighter than a blend")

    def test_sensor_denoise_quiets_the_flat_field(self):
        variance = [self.decode({"raw_sensor_denoise": mode})[FLAT].var()
                    for mode in ("off", "light", "full")]
        self.assertEqual(variance, sorted(variance, reverse=True),
                         f"denoise did not reduce noise monotonically: {variance}")

    def test_raw_profiles_select_different_demosaics(self):
        camera = self.decode({"raw_profile": "camera"})
        for profile in ("detail", "smooth"):
            with self.subTest(profile=profile):
                other = self.decode({"raw_profile": profile})
                self.assertGreater(np.abs(other - camera).max(), 8,
                                   f"{profile} decoded identically to camera")

    def test_develop_profile_separates_only_on_a_raw_source(self):
        """The film-off rendering intent, which no TIFF fixture can reach."""
        scene = self.decode({}) / 65535.0
        rendered = {profile: np.asarray(
            self.pipeline.linear_prophoto_to_display_srgb(
                scene, {"developProfile": profile}), dtype=np.float64)
            for profile in ("linear", "standard", "soft")}
        for profile in ("standard", "soft"):
            with self.subTest(profile=profile):
                self.assertGreater(
                    np.abs(rendered[profile] - rendered["linear"]).max(), 1e-3,
                    f"{profile} rendered identically to linear")

    def test_decode_is_reproducible(self):
        """Two decodes of the same file and parameters must be identical."""
        first = self.decode({"wb_mode": "daylight", "raw_profile": "detail"})
        second = self.decode({"wb_mode": "daylight", "raw_profile": "detail"})
        self.assertEqual(float(np.abs(first - second).max()), 0.0)


@unittest.skipUnless(_rawpy(), "needs rawpy")
class RawFixtures(unittest.TestCase):
    def test_fixtures_are_reproducible_from_the_generator(self):
        """The committed DNGs must match what the script writes today."""
        import hashlib
        import shutil
        import tempfile
        import make_raw_fixtures

        committed = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(FIXTURES.glob("*.dng"))}
        self.assertTrue(committed, "no DNG fixtures are committed")
        with tempfile.TemporaryDirectory(prefix="lighttable-raw-fixtures-") as temp:
            original = make_raw_fixtures.FIXTURES
            make_raw_fixtures.FIXTURES = Path(temp)
            try:
                make_raw_fixtures.build()
                rebuilt = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(Path(temp).glob("*.dng"))}
            finally:
                make_raw_fixtures.FIXTURES = original
                shutil.rmtree(temp, ignore_errors=True)
        self.assertEqual(committed, rebuilt,
                         "the committed fixtures no longer match "
                         "tests/make_raw_fixtures.py; rerun it and commit")

    def test_fixtures_stay_small(self):
        total = sum(path.stat().st_size for path in FIXTURES.glob("*.dng"))
        self.assertLess(total, 256 * 1024,
                        "RAW fixtures should stay well under a quarter megabyte")


if __name__ == "__main__":
    unittest.main()
