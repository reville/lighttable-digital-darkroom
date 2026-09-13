# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile

import color_pipeline
from calibration.measure_pair import metrics


class ExportPrecisionTests(unittest.TestCase):
    def test_float_resize_stays_float32_and_preserves_range(self):
        image = np.linspace(0.0, 1.0, 24 * 36 * 3, dtype=np.float32).reshape(
            24, 36, 3)
        resized = color_pipeline.resize_float_width(image, 12)
        self.assertEqual(resized.shape, (8, 12, 3))
        self.assertEqual(resized.dtype, np.float32)
        self.assertGreaterEqual(float(resized.min()), 0.0)
        self.assertLessEqual(float(resized.max()), 1.0)

    def test_tiff_export_is_rgb16_and_embeds_icc_profile(self):
        image = np.array([[[0.500123, 0.25, 0.75]]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.tif"
            self.assertEqual(
                color_pipeline.save_export_image(
                    image, path, fmt="tif", output_space="srgb"),
                (1, 1),
            )
            rendered = tifffile.imread(path)
            self.assertEqual(rendered.dtype, np.uint16)
            self.assertEqual(rendered.shape, (1, 1, 3))
            self.assertNotEqual(int(rendered[0, 0, 0]) % 257, 0)
            with tifffile.TiffFile(path) as document:
                self.assertIn(34675, document.pages[0].tags)

    def test_external_tiff_can_be_encoded_as_rgb8(self):
        image = np.array([[[0.1, 0.5, 0.9]]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external.tif"
            color_pipeline.save_export_image(
                image, path, fmt="tif", output_space="srgb", bit_depth=8)
            rendered = tifffile.imread(path)
            self.assertEqual(rendered.dtype, np.uint8)
            self.assertEqual(rendered.shape, (1, 1, 3))

    def test_heif_refuses_when_platform_encoder_is_unavailable(self):
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(color_pipeline.sys, "platform", "linux")):
            with self.assertRaisesRegex(RuntimeError, "macOS ImageIO helper"):
                color_pipeline.save_export_image(
                    np.zeros((2, 2, 3), dtype=np.float32),
                    Path(directory) / "result.heic", fmt="heif")

    def test_heif_stages_metadata_before_platform_encode(self):
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(color_pipeline.sys, "platform", "darwin"),
              mock.patch.object(color_pipeline.os, "access", return_value=True),
              mock.patch.object(color_pipeline.platform_image,
                                "write_metadata", return_value=True) as write,
              mock.patch.object(color_pipeline.subprocess, "run") as run):
            destination = Path(directory) / "result.heic"

            def encode(command, **_):
                destination.write_bytes(b"heif")
                return mock.Mock(returncode=0, stderr="", stdout="")

            run.side_effect = encode
            color_pipeline.save_export_image(
                np.zeros((2, 2, 3), dtype=np.float32), destination,
                fmt="heif", metadata_source="capture.dng",
                metadata_policy="copyright",
                metadata_fields={"creator": "Nicholas"})
            staged = write.call_args.args[0]
            self.assertEqual(Path(staged).suffix, ".tif")
            write.assert_called_once_with(
                staged, "capture.dng", "copyright", {"creator": "Nicholas"}, warnings=None)
            self.assertEqual(Path(run.call_args.args[0][2]), staged)

    def test_output_space_conversion_preserves_neutral(self):
        image = np.full((2, 2, 3), 0.4, dtype=np.float32)
        converted = color_pipeline.convert_output_space(image, "display_p3")
        np.testing.assert_allclose(converted[..., 0], converted[..., 1], atol=2e-4)
        np.testing.assert_allclose(converted[..., 1], converted[..., 2], atol=2e-4)

    def test_unknown_output_space_falls_back_to_srgb_consistently(self):
        self.assertEqual(color_pipeline.normalise_output_space("unknown"), "srgb")

    def test_pair_metrics_convert_declared_wide_gamut_input_to_srgb(self):
        image = np.array([
            [[0.25, 0.35, 0.45], [0.55, 0.45, 0.35]],
            [[0.20, 0.20, 0.20], [0.75, 0.75, 0.75]],
        ], dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.tif"
            reference = Path(directory) / "reference.tif"
            color_pipeline.save_export_image(
                image, candidate, fmt="tif", output_space="srgb")
            color_pipeline.save_export_image(
                image, reference, fmt="tif", output_space="prophoto")
            report = metrics(candidate, reference, "srgb", "prophoto")
            self.assertLess(report["deltaE2000Mean"], 0.02)


class CaptureColourTests(unittest.TestCase):
    def test_raw_cache_identity_includes_capture_white_balance(self):
        as_shot = color_pipeline.raw_decode_fingerprint({"wb_mode": "as_shot"})
        daylight = color_pipeline.raw_decode_fingerprint({"wb_mode": "daylight"})
        custom = color_pipeline.raw_decode_fingerprint({
            "wb_mode": "custom", "wb_temperature": 3200, "wb_tint": 0.1,
        })
        self.assertEqual(as_shot, color_pipeline.raw_decode_fingerprint({}))
        self.assertEqual(len({as_shot, daylight, custom}), 3)

    def test_raw_cache_identity_includes_capture_quality(self):
        baseline = color_pipeline.raw_decode_fingerprint({})
        variants = {
            color_pipeline.raw_decode_fingerprint({"raw_profile": "detail"}),
            color_pipeline.raw_decode_fingerprint({
                "raw_highlight_recovery": "blend"}),
            color_pipeline.raw_decode_fingerprint({"raw_sensor_denoise": "full"}),
        }
        self.assertNotIn(baseline, variants)
        self.assertEqual(len(variants), 3)

    def test_raw_cache_identity_includes_the_develop_profile(self):
        standard = color_pipeline.raw_decode_fingerprint(
            {"developProfile": "standard"})
        linear = color_pipeline.raw_decode_fingerprint(
            {"developProfile": "linear"})
        soft = color_pipeline.raw_decode_fingerprint(
            {"developProfile": "soft"})

        self.assertNotEqual(standard, linear)
        self.assertNotEqual(standard, soft)
        self.assertNotEqual(linear, soft)
        # An absent or unreadable value renders as the default profile, so it
        # must not earn a third cache identity.
        self.assertEqual(standard, color_pipeline.raw_decode_fingerprint({}))
        self.assertEqual(standard, color_pipeline.raw_decode_fingerprint(
            {"developProfile": "nonsense"}))

    def test_learned_denoise_changes_the_decode_fingerprint(self):
        baseline = color_pipeline.raw_decode_fingerprint({})
        enabled = color_pipeline.raw_decode_fingerprint({
            "learned_denoise": True, "learned_denoise_strength": 0.6})
        stronger = color_pipeline.raw_decode_fingerprint({
            "learned_denoise": True, "learned_denoise_strength": 0.9})
        self.assertNotEqual(baseline, enabled)
    def test_white_balance_presets_and_high_temperature_range(self):
        modes = {"as_shot", "auto", "daylight", "cloudy", "shade", "tungsten", "fluorescent", "flash"}
        fps = {mode: color_pipeline.raw_decode_fingerprint({"wb_mode": mode}) for mode in modes}
        self.assertEqual(len(fps), len(modes))
        xy_50k = color_pipeline._temperature_xy(50000.0)
        self.assertTrue(np.all(np.isfinite(xy_50k)))
        self.assertGreater(xy_50k[0], 0.2)
        dummy = np.ones((2, 2, 3), dtype=np.float32) * 0.5
        balanced = color_pipeline.apply_custom_raw_white_balance(dummy, 50000.0, 0.0)
        self.assertEqual(balanced.shape, (2, 2, 3))
        self.assertTrue(np.all(np.isfinite(balanced)))

    def test_decode_runs_learned_runner_once_for_one_tile(self):
        import rawpy

        class Sizes:
            width = 80

        class Raw:
            sizes = Sizes()
            daylight_whitebalance = [1.0, 1.0, 1.0, 1.0]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def postprocess(self, **_):
                return np.full((70, 80, 3), 16384, dtype=np.uint16)

        runner = mock.Mock(side_effect=lambda tile, *_: tile)
        with mock.patch("raw_decode_runtime.open_raw") as opened:
            opened.return_value.__enter__.return_value = (Raw(), rawpy)
            first = color_pipeline.decode_raw(
                "frame.dng", {"learned_denoise": True,
                              "learned_denoise_strength": 0.6},
                learned_denoise_runner=runner)
        self.assertEqual(first.shape, (70, 80, 3))
        runner.assert_called_once()

    def test_raw_postprocess_quality_options_are_sensor_stage(self):
        import rawpy

        options = color_pipeline.raw_postprocess_options({
            "raw_profile": "detail",
            "raw_highlight_recovery": "blend",
            "raw_sensor_denoise": "light",
        })
        self.assertEqual(options["demosaic_algorithm"],
                         rawpy.DemosaicAlgorithm.DCB)
        self.assertEqual(options["highlight_mode"], rawpy.HighlightMode.Blend)
        self.assertEqual(options["fbdd_noise_reduction"],
                         rawpy.FBDDNoiseReductionMode.Light)
        self.assertTrue(options["no_auto_bright"])

    def test_custom_white_balance_is_applied_before_film_as_float(self):
        image = np.full((2, 2, 3), 0.25, dtype=np.float32)
        balanced = color_pipeline.apply_custom_raw_white_balance(
            image, 3200, 0.0)
        self.assertEqual(balanced.dtype, np.float32)
        self.assertFalse(np.allclose(balanced[..., 0], balanced[..., 2]))


def _neutral_render_before_develop_profiles(image: np.ndarray) -> np.ndarray:
    """The Film-off render exactly as it stood before Phase 4.1.

    Kept verbatim so the ``linear`` profile can be proved unchanged rather
    than merely asserted to be different from ``standard``.
    """
    import colour

    linear = color_pipeline.as_float_rgb(image)
    prophoto = colour.RGB_COLOURSPACES["ProPhoto RGB"]
    srgb = colour.RGB_COLOURSPACES["sRGB"]
    encoded = colour.RGB_to_RGB(
        linear, prophoto, srgb,
        chromatic_adaptation_transform="Bradford",
        apply_cctf_decoding=False, apply_cctf_encoding=True,
    )
    white = float(np.percentile(np.maximum(encoded, 0.0), 99.5))
    if np.isfinite(white) and white > 0:
        encoded = encoded * min(4.0, 0.96 / white)
    return np.clip(encoded, 0.0, 1.0).astype(np.float32)


def _srgb_code_value(linear: float) -> float:
    """Encode one scene-linear value the way the display render will."""
    if linear <= 0.0031308:
        return linear * 12.92
    return 1.055 * linear ** (1.0 / 2.4) - 0.055


class StandardDevelopProfileTests(unittest.TestCase):
    """Phase 4.1: the fixed base curve used when the film profile is off."""

    NEUTRAL_PATCHES = (0.02, 0.05, 0.09, 0.18, 0.36, 0.72, 1.0)

    def _patch_scene(self) -> np.ndarray:
        return np.concatenate(
            [np.full((1, 8, 3), value, dtype=np.float32)
             for value in self.NEUTRAL_PATCHES], axis=1)

    def test_linear_profile_is_byte_for_byte_the_previous_render(self):
        image = (np.random.default_rng(7).random((17, 23, 3),
                                                 dtype=np.float32) ** 2.0)
        expected = _neutral_render_before_develop_profiles(image)

        for rendered in (
            color_pipeline.linear_prophoto_to_display_srgb(
                image, {"developProfile": "linear"}),
            color_pipeline.linear_prophoto_to_display_srgb(
                image, develop_profile="linear"),
        ):
            self.assertEqual(rendered.dtype, np.float32)
            self.assertEqual(rendered.tobytes(), expected.tobytes())

        # The keyword wins over the dict, and everything else is Standard.
        self.assertEqual(
            color_pipeline.linear_prophoto_to_display_srgb(
                image, {"developProfile": "standard"},
                develop_profile="linear").tobytes(),
            expected.tobytes())
        for params in ({}, None, {"developProfile": "nonsense"}):
            self.assertFalse(np.array_equal(
                color_pipeline.linear_prophoto_to_display_srgb(image, params),
                expected))

    def test_standard_base_curve_is_monotonic_and_stays_in_range(self):
        curve = color_pipeline.standard_base_curve()
        domain = color_pipeline.standard_base_curve_domain()

        self.assertEqual(curve.shape, (256,))
        self.assertEqual(domain.shape, (256,))
        self.assertTrue(np.all(np.diff(curve) >= 0.0))
        self.assertTrue(np.all(np.diff(domain) > 0.0))
        # The documented domain is the whole 0..1 scene-linear range.
        self.assertEqual(float(domain[0]), 0.0)
        self.assertEqual(float(domain[-1]), 1.0)
        self.assertEqual(float(curve[0]), 0.0)
        self.assertLessEqual(float(curve[-1]), 1.0)
        self.assertGreaterEqual(float(curve.min()), 0.0)
        self.assertLessEqual(float(curve.max()), 1.0)

    def test_standard_base_curve_lifts_middle_grey_by_the_documented_amount(self):
        for curve in (color_pipeline.standard_base_curve(),
                      color_pipeline.soft_base_curve()):
            domain = color_pipeline.standard_base_curve_domain()
            grey = float(np.interp(0.18, domain, curve))
            display = _srgb_code_value(grey)
            # Both profiles place a grey card at the same lifted code value,
            # so switching between them changes contrast, not exposure.
            self.assertAlmostEqual(display, color_pipeline.STANDARD_GREY_DISPLAY,
                                   delta=0.01)
            self.assertGreater(grey, 0.18 * 1.3)
            self.assertLess(grey, 0.18 * 1.45)

    def test_standard_curve_lifts_mid_tones_and_rolls_off_highlights(self):
        curve = color_pipeline.standard_base_curve()
        domain = color_pipeline.standard_base_curve_domain()

        useful = (domain > 0.03) & (domain < 0.95)
        self.assertTrue(np.all(curve[useful] >= domain[useful]))
        # Continuous through the pivot: no kink in the slope at middle grey.
        slope = np.diff(curve) / np.diff(domain)
        pivot = int(np.searchsorted(domain, 0.18))
        self.assertLess(abs(float(slope[pivot + 1] / slope[pivot - 2]) - 1.0), 0.08)
        # A shoulder, not a clip: the last step into white stays a step.
        self.assertLess(float(curve[-1] - curve[-2]),
                        float(domain[-1] - domain[-2]))
        self.assertGreater(float(curve[-1] - curve[-2]), 0.0)

    def test_standard_profile_adds_contrast_without_new_clipping(self):
        scene = self._patch_scene()
        linear = color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"developProfile": "linear"})
        standard = color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"developProfile": "standard"})

        def patch(image, index):
            return float(image[0, index * 8 + 4, 0])

        for index, value in enumerate(self.NEUTRAL_PATCHES):
            # Every patch renders brighter than the plain encode, the grey
            # card by a visible margin, and white still has headroom.
            self.assertGreater(patch(standard, index), patch(linear, index),
                               msg=str(value))
            if value == 0.18:
                self.assertGreater(patch(standard, index) - patch(linear, index), 0.05)
        values = [patch(standard, index) for index in range(len(self.NEUTRAL_PATCHES))]
        self.assertEqual(values, sorted(values))
        self.assertGreaterEqual(float(standard.min()), 0.0)
        self.assertLess(float(standard.max()), 1.0)

    def test_standard_profile_no_longer_darkens_dim_scenes(self):
        # A dim scene that lives below middle grey. The fixed-grey curve used
        # to push all of it darker than the Linear render; the lifted curve
        # must leave the Standard render brighter.
        scene = (np.random.default_rng(3).random((24, 32, 3), dtype=np.float32) ** 3.0) * 0.4
        linear = color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"developProfile": "linear"})
        standard = color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"developProfile": "standard"})
        self.assertGreater(float(np.median(standard)), float(np.median(linear)))
        # Independent random channels make an adversarially saturated scene.
        # The lift pushes a little more of it over the sRGB gamut edge than the
        # plain encode does; on photographs the difference is a tenth of a
        # percent, and even here it must stay small.
        self.assertLess(float((standard >= 1.0).mean()), 0.03)

    def test_soft_base_curve_is_monotonic_and_holds_middle_grey(self):
        curve = color_pipeline.soft_base_curve()
        domain = color_pipeline.standard_base_curve_domain()

        self.assertEqual(curve.shape, (256,))
        self.assertTrue(np.all(np.diff(curve) >= 0.0))
        self.assertEqual(float(curve[0]), 0.0)
        self.assertLessEqual(float(curve[-1]), 1.0)

        grey = float(np.interp(0.18, domain, curve))
        display = _srgb_code_value(grey)
        self.assertAlmostEqual(display, color_pipeline.STANDARD_GREY_DISPLAY, delta=0.01)
        self.assertAlmostEqual(grey, 0.243, delta=0.004)

    def test_soft_profile_preserves_more_highlight_headroom_than_standard(self):
        std_curve = color_pipeline.standard_base_curve()
        soft_curve = color_pipeline.soft_base_curve()
        domain = color_pipeline.standard_base_curve_domain()

        # Above middle grey up to near-white, soft curve compresses highlights less aggressively
        # (lower output value than standard), leaving more headroom before clipping.
        upper = (domain > 0.25) & (domain < 0.95)
        self.assertTrue(np.all(soft_curve[upper] < std_curve[upper]))
        # But still above linear domain
        self.assertTrue(np.all(soft_curve[upper] > domain[upper]))

    def test_white_normalisation_is_shared_and_the_curves_shoulder_above_it(self):
        dim = np.repeat(
            np.linspace(0.0, 0.55, 240, dtype=np.float32)[None, :, None],
            3, axis=2)

        rendered = color_pipeline.linear_prophoto_to_display_srgb(
            dim, {"developProfile": "linear"})
        self.assertAlmostEqual(float(np.percentile(rendered, 99.5)), 0.96, delta=0.001)
        # The curved profiles expose to the same 0.96 first and then let the
        # shoulder place the brightest useful values just under white, so
        # exposure is shared and the profiles differ only in tone.
        for profile in ("standard", "soft"):
            rendered = color_pipeline.linear_prophoto_to_display_srgb(
                dim, {"developProfile": profile})
            top = float(np.percentile(rendered, 99.5))
            self.assertGreater(top, 0.96, msg=profile)
            self.assertLess(top, 0.995, msg=profile)


def _previous_resize_float_to_size(image, size):
    """The single-threaded resize the threaded one must reproduce."""
    from PIL import Image
    source = color_pipeline.as_float_rgb(image)
    channels = [
        np.asarray(Image.fromarray(source[..., channel], mode="F").resize(
            size, Image.Resampling.LANCZOS, reducing_gap=3.0), dtype=np.float32)
        for channel in range(3)]
    return np.clip(np.stack(channels, axis=2), 0.0, 1.0).astype(np.float32, copy=False)


@unittest.skipIf(color_pipeline._develop_kernels() is None, "needs numba")
class DevelopKernelTests(unittest.TestCase):
    """The parallel Develop must be bit-identical to the colour-science render."""

    PROFILES = ("linear", "standard", "soft")
    SPACES = ("srgb", "display_p3", "prophoto")

    def setUp(self):
        self.kernels = color_pipeline._develop_kernels()

    def assertSameBits(self, actual, expected, msg=None):
        self.assertEqual(actual.dtype, expected.dtype, msg)
        self.assertEqual(actual.shape, expected.shape, msg)
        self.assertEqual(actual.tobytes(), expected.tobytes(), msg)

    def _inputs(self):
        rng = np.random.default_rng(29)
        codes = rng.integers(0, 65536, (40, 56, 3), dtype=np.uint16)
        codes[:4] = 0
        codes[4:6] = 65535
        rgba = np.concatenate(
            [codes, np.full((40, 56, 1), 65535, dtype=np.uint16)], axis=2)
        return {
            "uint16": codes,
            "uint16 with alpha": rgba,
            "uint8": rng.integers(0, 256, (40, 56, 3), dtype=np.uint8),
            # Out-of-range floats exercise the input clip; squared values put
            # most of the frame in the shadows, like a linear decode.
            "float32": (rng.random((40, 56, 3), dtype=np.float32) * 1.5 - 0.2) ** 2,
            "float64": codes / 65535.0,
            "dim uint16": (codes // 64).astype(np.uint16),
        }

    def test_every_profile_and_output_space_matches_the_reference(self):
        for label, image in self._inputs().items():
            for profile in self.PROFILES:
                for space in self.SPACES:
                    msg = f"{label} {profile} {space}"
                    params = {"developProfile": profile}
                    expected = color_pipeline._linear_prophoto_to_display_reference(
                        image, params, profile, space)
                    # Called directly so a kernel error fails instead of
                    # silently taking the reference path.
                    self.assertSameBits(color_pipeline._develop_with_kernels(
                        self.kernels, image, params, profile, space), expected, msg)
                    self.assertSameBits(color_pipeline.linear_prophoto_to_display(
                        image, params, output_space=space), expected, msg)

    def test_wide_gamut_export_conversion_matches_colour_science(self):
        import colour
        rng = np.random.default_rng(17)
        # Large enough for the kernel path; extended values reach both
        # transfer-function branches and the output clip.
        frames = (rng.random((800, 700, 3), dtype=np.float32) * 1.3 - 0.15,
                  rng.random((800, 700, 3)) * 1.3 - 0.15)
        for frame in frames:
            for space in ("display_p3", "prophoto"):
                expected = np.clip(colour.RGB_to_RGB(
                    np.asarray(frame, dtype=np.float64), "sRGB",
                    color_pipeline.COLOUR_SPACE_NAMES[space],
                    apply_cctf_decoding=True, apply_cctf_encoding=True),
                    0.0, 1.0).astype(np.float32)
                converted = color_pipeline._convert_from_srgb_with_kernels(
                    frame, space)
                self.assertIsNotNone(converted, space)
                self.assertSameBits(converted, expected, f"{frame.dtype} {space}")
                self.assertSameBits(color_pipeline.convert_output_space(
                    frame, space), expected, f"{frame.dtype} {space}")

    def test_black_frame_skips_normalisation_identically(self):
        black = np.zeros((8, 8, 3), dtype=np.uint16)
        for profile in self.PROFILES:
            for space in self.SPACES:
                self.assertSameBits(
                    color_pipeline._develop_with_kernels(
                        self.kernels, black, {}, profile, space),
                    color_pipeline._linear_prophoto_to_display_reference(
                        black, {}, profile, space), f"{profile} {space}")

    def test_kernel_failure_renders_through_the_reference(self):
        image = self._inputs()["uint16"]
        expected = color_pipeline._linear_prophoto_to_display_reference(
            image, {}, "standard", "srgb")
        with mock.patch.object(color_pipeline, "_develop_with_kernels",
                               side_effect=RuntimeError("kernel")):
            self.assertSameBits(
                color_pipeline.linear_prophoto_to_display_srgb(image, {}), expected)

    def test_warm_up_compiles_every_kernel_a_raw_develop_uses(self):
        names = ("encode_codes", "deliver", "count_at_least", "gather_at_least")
        color_pipeline.warm_develop_jit()
        warmed = {name: len(getattr(self.kernels, name).signatures) for name in names}
        for name in names:
            self.assertTrue(warmed[name], name)
        # A 16-bit frame large enough for the candidate percentile must find
        # every specialization already compiled, so warming covered it.
        codes = np.random.default_rng(2).integers(
            0, 65536, (620, 600, 3), dtype=np.uint16)
        for profile in self.PROFILES:
            for space in ("srgb", "display_p3"):
                color_pipeline._develop_with_kernels(
                    self.kernels, codes, {}, profile, space)
        for name in names:
            self.assertEqual(len(getattr(self.kernels, name).signatures),
                             warmed[name], name)

    def test_white_point_gathers_candidates_and_matches_numpy(self):
        rng = np.random.default_rng(5)
        shape = (180, 240, 3)
        spread = rng.random(shape) * 1.4 - 0.2
        ties = np.round(rng.random(shape), 2)
        clipped = np.where(rng.random(shape) < 0.8, 1.0, rng.random(shape))
        signed_zero = np.where(rng.random(shape) < 0.5, -0.0, spread)
        cases = {"spread": spread, "ties": ties, "clipped": clipped,
                 "signed zero": signed_zero, "black": np.zeros(shape),
                 "negative": -np.abs(spread)}
        for label, encoded in cases.items():
            encoded = np.ascontiguousarray(encoded)
            expected = float(np.percentile(np.maximum(encoded, 0.0), 99.5))
            with mock.patch.object(color_pipeline.np, "percentile",
                                   side_effect=AssertionError("numpy fallback")):
                white = color_pipeline._develop_white_point(
                    encoded, self.kernels, min_values=0, sample_stride=7)
            self.assertEqual(np.float64(white).tobytes(),
                             np.float64(expected).tobytes(), label)

    def test_white_point_falls_back_to_numpy_when_the_sample_misleads(self):
        size = 180 * 240 * 3
        flat = np.full(size, 0.5)
        sampled = np.arange(size) % 7 == 0
        # Just over 1% of the sampled values are the brightest in the frame,
        # so the sample's threshold keeps far fewer than 0.5% of all values.
        bright = np.flatnonzero(sampled)[::90]
        flat[sampled] = 0.0
        flat[bright] = 1.0
        encoded = flat.reshape(180, 240, 3)
        expected = float(np.percentile(np.maximum(encoded, 0.0), 99.5))
        with mock.patch.object(color_pipeline.np, "percentile",
                               wraps=np.percentile) as percentile:
            white = color_pipeline._develop_white_point(
                encoded, self.kernels, min_values=0, sample_stride=7)
        percentile.assert_called_once()
        self.assertEqual(white, expected)

        with_nan = np.ascontiguousarray(encoded.copy())
        with_nan[3, 4, 1] = np.nan
        self.assertTrue(np.isnan(color_pipeline._develop_white_point(
            with_nan, self.kernels, min_values=0, sample_stride=7)))


class ThreadedPixelOperationTests(unittest.TestCase):
    def test_threaded_resize_matches_the_single_threaded_resize(self):
        rng = np.random.default_rng(3)
        for image in (rng.integers(0, 65536, (300, 420, 3), dtype=np.uint16),
                      rng.random((300, 420, 4), dtype=np.float32) * 1.2 - 0.1):
            for size in ((150, 107), (420, 300), (840, 600)):
                expected = _previous_resize_float_to_size(image, size)
                resized = color_pipeline.resize_float_to_size(image, size)
                self.assertEqual(resized.dtype, expected.dtype)
                self.assertEqual(resized.tobytes(), expected.tobytes(), size)

    def test_band_quantisation_matches_the_whole_frame_expression(self):
        rng = np.random.default_rng(8)
        for image in (rng.random((1210, 1203, 3), dtype=np.float32),
                      rng.random((1210, 1203, 3)) * 1.1 - 0.05,
                      rng.random((9, 7, 3), dtype=np.float32)):
            expected = (image * 65535.0 + 0.5).astype(np.uint16)
            self.assertEqual(color_pipeline.to_uint16(image).tobytes(),
                             expected.tobytes())


if __name__ == "__main__":
    unittest.main()
