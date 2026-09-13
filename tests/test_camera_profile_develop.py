# SPDX-License-Identifier: GPL-3.0-only
"""The optional camera profile in the Film-off develop.

The profile files themselves are the user's own Adobe Camera Raw or Lightroom
installation; the application only resolves a bare file name inside the
configured folder. These tests build a small .dcp in a temporary folder with
the same writer the profile reader's tests use.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

import camera_profile
import color_pipeline
import film_pipeline as fp
try:
    from test_camera_profile import write_profile
except ImportError:  # invoked as tests.<module> rather than by discovery
    from tests.test_camera_profile import write_profile


def _scene() -> np.ndarray:
    rng = np.random.default_rng(11)
    return (rng.random((12, 16, 3), dtype=np.float32) ** 2.0) * 0.8


# SHA-256 prefixes of the Built-in develop of ``_scene()`` (float) and its
# uint16 codes, recorded from the release before camera profiles moved to
# the DNG order and the bundled look arrived. A photo that saved no profile,
# or saved "Built-in", must keep rendering these bytes.
BUILT_IN_DIGESTS = {
    ("float", "standard", "srgb"): "96eead0535531cd8",
    ("float", "standard", "display_p3"): "0dad7fedf49b2348",
    ("float", "soft", "srgb"): "e2636c0cb747503b",
    ("float", "soft", "display_p3"): "118f6902b3b8f43c",
    ("float", "linear", "srgb"): "4aa052b67dc50d3e",
    ("float", "linear", "display_p3"): "2ca04e75639fef8a",
    ("uint16", "standard", "srgb"): "7cdc4537336c9142",
    ("uint16", "standard", "display_p3"): "db19fef430fae6da",
    ("uint16", "soft", "srgb"): "fed8dc61e4200449",
    ("uint16", "soft", "display_p3"): "1203e98234d703be",
    ("uint16", "linear", "srgb"): "554c36d40d0988e4",
    ("uint16", "linear", "display_p3"): "97829399a1f53647",
}


class CameraProfileDevelopTests(unittest.TestCase):
    def setUp(self):
        self._resolver = color_pipeline.CAMERA_PROFILE_RESOLVER
        color_pipeline._read_camera_profile.cache_clear()

    def tearDown(self):
        color_pipeline.CAMERA_PROFILE_RESOLVER = self._resolver
        color_pipeline._read_camera_profile.cache_clear()

    def test_parameter_is_a_bare_file_name_or_nothing(self):
        for value, expected in (
            ("Nikon D7100 Camera Standard.dcp", "Nikon D7100 Camera Standard.dcp"),
            ("  Nikon  D7100   Adobe Standard.DCP ", "Nikon D7100 Adobe Standard.DCP"),
            ("../escape.dcp", ""), ("sub/dir.dcp", ""), ("C:\\\\x.dcp", ""),
            (".hidden.dcp", ""), ("not-a-profile.txt", ""), ("", ""), (None, ""),
        ):
            self.assertEqual(fp.clean_params({"camera_profile": value})["camera_profile"],
                             expected, repr(value))
        self.assertIn("camera_profile", fp.RAW_DEVELOP_KEYS)
        self.assertEqual(fp.DEFAULT_PARAMS["camera_profile"], "")

    def test_unresolved_profile_renders_the_built_in_develop(self):
        scene = _scene()
        built_in = color_pipeline.linear_prophoto_to_display_srgb(scene, {})
        color_pipeline.CAMERA_PROFILE_RESOLVER = lambda name: None
        rendered = color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"camera_profile": "Missing Camera Adobe Standard.dcp"})
        self.assertEqual(rendered.tobytes(), built_in.tobytes())
        self.assertEqual(color_pipeline.raw_decode_fingerprint({}),
                         color_pipeline.raw_decode_fingerprint(
                             {"camera_profile": "Missing Camera Adobe Standard.dcp"}))

    def test_parallel_develop_matches_the_reference_with_a_profile(self):
        kernels = color_pipeline._develop_kernels()
        if kernels is None:
            self.skipTest("needs numba")
        codes = (_scene() * 65535.0).astype(np.uint16)
        with tempfile.TemporaryDirectory() as folder:
            curved = write_profile(Path(folder) / "Test Camera Standard.dcp",
                                   tone_curve=[[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]])
            plain = write_profile(Path(folder) / "Test Camera Adobe Standard.dcp")
            index = {curved.name: curved, plain.name: plain}
            color_pipeline.CAMERA_PROFILE_RESOLVER = index.get
            for path in (curved, plain):
                params = fp.clean_params({"camera_profile": path.name})
                for image in (codes, _scene()):
                    for space in ("srgb", "display_p3"):
                        self.assertEqual(
                            color_pipeline._develop_with_kernels(
                                kernels, image, params, "standard", space).tobytes(),
                            color_pipeline._linear_prophoto_to_display_reference(
                                image, params, "standard", space).tobytes(),
                            f"{path.name} {image.dtype} {space}")

    def test_profile_tone_curve_replaces_the_built_in_curve(self):
        scene = _scene()
        with tempfile.TemporaryDirectory() as folder:
            path = write_profile(Path(folder) / "Test Camera Standard.dcp",
                                 tone_curve=[[0.0, 0.0], [0.25, 0.4], [0.5, 0.75], [1.0, 1.0]])
            index = {path.name: path}
            color_pipeline.CAMERA_PROFILE_RESOLVER = index.get
            params = fp.clean_params({"camera_profile": path.name})
            profiled = color_pipeline.linear_prophoto_to_display_srgb(scene, params)
            built_in = color_pipeline.linear_prophoto_to_display_srgb(scene, {})
            self.assertFalse(np.array_equal(profiled, built_in))
            # The profile owns tone: a strongly lifting curve renders brighter
            # mid-tones than the built-in Standard curve does.
            self.assertGreater(float(np.median(profiled)), float(np.median(built_in)))
            # Linear stays the scan-matching path and ignores the profile.
            linear = color_pipeline.linear_prophoto_to_display_srgb(
                scene, dict(params, developProfile="linear"))
            self.assertEqual(linear.tobytes(), color_pipeline.linear_prophoto_to_display_srgb(
                scene, {"developProfile": "linear"}).tobytes())
            # A different profile file means a different neutral cache entry.
            self.assertNotEqual(color_pipeline.raw_decode_fingerprint(params),
                                color_pipeline.raw_decode_fingerprint({}))
            before = color_pipeline.raw_decode_fingerprint(params)
            write_profile(path, tone_curve=[[0.0, 0.0], [1.0, 1.0]])
            path.touch()
            self.assertNotEqual(before, color_pipeline.raw_decode_fingerprint(params))

    def test_profile_without_a_curve_keeps_the_standard_curve(self):
        scene = _scene()
        with tempfile.TemporaryDirectory() as folder:
            path = write_profile(Path(folder) / "Test Camera Adobe Standard.dcp",
                                 tone_curve=None)
            color_pipeline.CAMERA_PROFILE_RESOLVER = {path.name: path}.get
            params = fp.clean_params({"camera_profile": path.name})
            image, has_curve = color_pipeline.apply_camera_profile(
                color_pipeline.as_float_rgb(scene), params)
            self.assertFalse(has_curve)
            self.assertEqual(image.shape, scene.shape)
            self.assertEqual(image.dtype, np.float32)

    def test_built_in_develop_renders_the_recorded_bytes(self):
        scene = _scene()
        codes = (scene * 65535.0).astype(np.uint16)
        for image, label in ((scene, "float"), (codes, "uint16")):
            for develop in ("standard", "soft", "linear"):
                for space in ("srgb", "display_p3"):
                    for params in ({"developProfile": develop},
                                   fp.clean_params({"developProfile": develop, "camera_profile": ""})):
                        out = color_pipeline.linear_prophoto_to_display(
                            image, params, output_space=space)
                        self.assertEqual(
                            hashlib.sha256(out.tobytes()).hexdigest()[:16],
                            BUILT_IN_DIGESTS[(label, develop, space)],
                            f"{label} {develop} {space}")

    def test_bundled_standard_resolves_without_a_folder_and_changes_the_develop(self):
        scene = _scene()
        color_pipeline.CAMERA_PROFILE_RESOLVER = None
        params = fp.clean_params({"camera_profile": camera_profile.BUNDLED_STANDARD_NAME})
        self.assertEqual(color_pipeline.camera_profile_path(params),
                         camera_profile.BUNDLED_STANDARD_FILE)
        rendered = color_pipeline.linear_prophoto_to_display_srgb(scene, params)
        built_in = color_pipeline.linear_prophoto_to_display_srgb(scene, {})
        self.assertFalse(np.array_equal(rendered, built_in))
        self.assertNotEqual(color_pipeline.raw_decode_fingerprint(params),
                            color_pipeline.raw_decode_fingerprint({}))
        # Linear ignores the look, as it does every profile.
        linear = color_pipeline.linear_prophoto_to_display_srgb(
            scene, dict(params, developProfile="linear"))
        self.assertEqual(linear.tobytes(), color_pipeline.linear_prophoto_to_display_srgb(
            scene, {"developProfile": "linear"}).tobytes())
        kernels = color_pipeline._develop_kernels()
        if kernels is None:
            self.skipTest("needs numba")
        for image in ((scene * 65535.0).astype(np.uint16), scene):
            for space in ("srgb", "display_p3"):
                self.assertEqual(
                    color_pipeline._develop_with_kernels(
                        kernels, image, params, "standard", space).tobytes(),
                    color_pipeline._linear_prophoto_to_display_reference(
                        image, params, "standard", space).tobytes())

    def test_hue_sat_map_is_applied_before_the_develop_exposure(self):
        # A map that halves value only for bright pixels: applied before the
        # exposure gain it sees the un-normalised scene, after it the lifted
        # one. The Develop must show the former.
        from camera_profile_write import grid_bytes
        scene = _scene() * 0.25  # dim, so the exposure gain is well above 1
        with tempfile.TemporaryDirectory() as folder:
            path = write_profile(Path(folder) / "Test Camera Standard.dcp",
                                 hue_sat_dims=(1, 1, 3),
                                 hue_sat_map=grid_bytes(1, 1, 3, lambda h, s, v: (0.0, 1.0, 0.5 if v == 2 else 1.0)),
                                 tone_curve=[[0.0, 0.0], [1.0, 1.0]])
            color_pipeline.CAMERA_PROFILE_RESOLVER = {path.name: path}.get
            params = fp.clean_params({"camera_profile": path.name})
            profile = camera_profile.read_profile(path)
            linear = color_pipeline.as_float_rgb(scene)
            captured = {}

            def expose(image):
                captured["input"] = image.copy()
                return image

            color_pipeline.apply_camera_profile(linear, params, expose=expose)
            expected = camera_profile.apply_hue_sat_map(
                linear, profile["hueSatMap1"], profile["hueSatDims"])
            np.testing.assert_allclose(captured["input"], expected, atol=1e-6)
            self.assertFalse(np.allclose(captured["input"], linear))

    def test_capture_temperature_reaches_the_dual_illuminant_tables(self):
        from camera_profile_write import grid_bytes
        scene = _scene()
        with tempfile.TemporaryDirectory() as folder:
            path = write_profile(
                Path(folder) / "Test Camera Dual.dcp",
                hue_sat_dims=(1, 1, 1),
                hue_sat_map=grid_bytes(1, 1, 1, lambda h, s, v: (0.0, 1.0, 1.0)),
                hue_sat_map_2=grid_bytes(1, 1, 1, lambda h, s, v: (90.0, 1.0, 1.0)),
                illuminant1=17, illuminant2=21, tone_curve=[[0.0, 0.0], [1.0, 1.0]])
            color_pipeline.CAMERA_PROFILE_RESOLVER = {path.name: path}.get
            params = fp.clean_params({"camera_profile": path.name})
            warm = color_pipeline.linear_prophoto_to_display_srgb(
                scene, params, capture_temperature=2856.0)
            cool = color_pipeline.linear_prophoto_to_display_srgb(
                scene, params, capture_temperature=6504.0)
            default = color_pipeline.linear_prophoto_to_display_srgb(scene, params)
            self.assertFalse(np.array_equal(warm, cool))
            self.assertFalse(np.array_equal(default, warm))
            self.assertFalse(np.array_equal(default, cool))
            kernels = color_pipeline._develop_kernels()
            if kernels is not None:
                self.assertEqual(
                    color_pipeline._develop_with_kernels(
                        kernels, scene, params, "standard", "srgb",
                        capture_temperature=2856.0).tobytes(),
                    warm.tobytes())


if __name__ == "__main__":
    unittest.main()
