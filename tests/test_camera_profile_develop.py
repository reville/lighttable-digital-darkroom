# SPDX-License-Identifier: GPL-3.0-only
"""The optional camera profile in the Film-off develop.

The profile files themselves are the user's own Adobe Camera Raw or Lightroom
installation; the application only resolves a bare file name inside the
configured folder. These tests build a small .dcp in a temporary folder with
the same writer the profile reader's tests use.
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

import color_pipeline
import film_pipeline as fp
try:
    from test_camera_profile import write_profile
except ImportError:  # invoked as tests.<module> rather than by discovery
    from tests.test_camera_profile import write_profile


def _scene() -> np.ndarray:
    rng = np.random.default_rng(11)
    return (rng.random((12, 16, 3), dtype=np.float32) ** 2.0) * 0.8


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


if __name__ == "__main__":
    unittest.main()
