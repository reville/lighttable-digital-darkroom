"""Linux processed TIFF imports retain sample precision before edits and export."""
from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import numpy.testing
import tifffile
from PIL import Image

import platform_image


def gamma_profile(gamma):
    """Build an RGB matrix profile whose independent reference is a power law."""
    cms = platform_image._littlecms()
    ptr = ctypes.c_void_p
    cms.cmsBuildGamma.argtypes = [ptr, ctypes.c_double]
    cms.cmsBuildGamma.restype = ptr
    cms.cmsFreeToneCurve.argtypes = [ptr]
    cms.cmsCreateRGBProfile.argtypes = [ptr, ptr, ptr]
    cms.cmsCreateRGBProfile.restype = ptr
    cms.cmsSaveProfileToMem.argtypes = [ptr, ptr, ctypes.POINTER(ctypes.c_uint32)]
    cms.cmsSaveProfileToMem.restype = ctypes.c_int
    white = (ctypes.c_double * 3)(0.3127, 0.3290, 1.0)
    primaries = (ctypes.c_double * 9)(0.64, 0.33, 1, 0.30, 0.60, 1, 0.15, 0.06, 1)
    curve = cms.cmsBuildGamma(None, gamma)
    profile = None
    try:
        curves = (ptr * 3)(curve, curve, curve)
        profile = cms.cmsCreateRGBProfile(white, primaries, curves)
        if not profile:
            raise RuntimeError("test profile creation failed")
        length = ctypes.c_uint32()
        if not cms.cmsSaveProfileToMem(profile, None, ctypes.byref(length)):
            raise RuntimeError("test profile sizing failed")
        buffer = ctypes.create_string_buffer(length.value)
        if not cms.cmsSaveProfileToMem(profile, buffer, ctypes.byref(length)):
            raise RuntimeError("test profile serialization failed")
        return buffer.raw[:length.value]
    finally:
        if profile:
            cms.cmsCloseProfile(profile)
        cms.cmsFreeToneCurve(curve)


class LinuxTiffPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.tif"
        self.output = self.root / "converted.tif"
        self.platform = mock.patch.object(platform_image.sys, "platform", "linux")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def convert(self):
        platform_image.convert_processed_to_tiff(self.source, self.output,
            app_root=self.root, output_space="prophoto")
        return tifffile.imread(self.output)

    def test_uint16_ramp_retains_every_sample_in_uncompressed_cache(self):
        ramp = np.repeat(np.arange(65536, dtype=np.uint16).reshape(256, 256, 1), 3, axis=2)
        tifffile.imwrite(self.source, ramp, photometric="rgb")
        actual = self.convert()
        self.assertEqual(actual.dtype, np.float32)
        self.assertEqual(len(np.unique(actual[..., 0])), 65536)
        np.testing.assert_allclose(actual, ramp.astype(np.float32) / 65535, rtol=0, atol=6e-8)
        with tifffile.TiffFile(self.output) as converted:
            self.assertEqual(converted.pages[0].compression, 1)
            self.assertEqual(converted.pages[0].bitspersample, 32)

    def test_float_tiff_and_preview_preserve_sub_uint16_steps_and_hdr_values(self):
        ramp = np.array([0.25, 0.2500001, 0.2500002, 0.8, 1.1, -0.1], dtype=np.float32)
        pixels = np.repeat(ramp.reshape(2, 3, 1), 3, axis=2)
        tifffile.imwrite(self.source, pixels, photometric="rgb")
        np.testing.assert_array_equal(self.convert(), pixels)
        preview = platform_image.processed_preview(self.source, 3,
            app_root=self.root, output_space="prophoto")
        np.testing.assert_array_equal(preview, pixels)

    def test_embedded_icc_uses_float_transform_with_power_law_reference(self):
        source_profile, target_profile = gamma_profile(1.0), gamma_profile(2.0)
        profiles = self.root / "color-profiles"
        profiles.mkdir()
        (profiles / "ProPhoto-v4.icc").write_bytes(target_profile)
        # Deliberately different default: conversion must honor the embedded ICC.
        (profiles / "sRGB-v4.icc").write_bytes(target_profile)
        pixels = np.linspace(0.01, 0.99, 4096 * 3, dtype=np.float32).reshape(32, 128, 3)
        tifffile.imwrite(self.source, pixels, photometric="rgb", extratags=[
            (34675, "B", len(source_profile), source_profile, False)])
        actual = self.convert()
        np.testing.assert_allclose(actual, np.sqrt(pixels), rtol=0, atol=5e-5)
        self.assertGreater(len(np.unique(actual[..., 0])), 4000)
        with tifffile.TiffFile(self.output) as converted:
            self.assertEqual(converted.pages[0].tags[34675].value, target_profile)

    def test_tiff_orientations_are_applied_once(self):
        pixels = np.repeat(np.arange(1, 7, dtype=np.uint16).reshape(2, 3, 1), 3, axis=2)
        expected = {1:[[1,2,3],[4,5,6]], 2:[[3,2,1],[6,5,4]],
                    3:[[6,5,4],[3,2,1]], 4:[[4,5,6],[1,2,3]],
                    5:[[1,4],[2,5],[3,6]], 6:[[4,1],[5,2],[6,3]],
                    7:[[6,3],[5,2],[4,1]], 8:[[3,6],[2,5],[1,4]]}
        for orientation, values in expected.items():
            with self.subTest(orientation=orientation):
                tifffile.imwrite(self.source, pixels, photometric="rgb", extratags=[
                    (274, "H", 1, orientation, False)])
                actual = self.convert()
                np.testing.assert_array_equal(np.rint(actual[..., 0] * 65535), values)

    def test_alpha_does_not_darken_straight_or_associated_tiff_colors(self):
        color = np.array([[[0.8, 0.4, 0.2]]], dtype=np.float32)
        alpha = np.full((1, 1, 1), 0.5, dtype=np.float32)
        for kind, rgb in (("unassalpha", color), ("assocalpha", color * alpha)):
            with self.subTest(kind=kind):
                tifffile.imwrite(self.source, np.concatenate([rgb, alpha], axis=2),
                                 photometric="rgb", extrasamples=[kind])
                np.testing.assert_allclose(self.convert(), color, rtol=0, atol=1e-7)

    def test_lzw_tiff_input_uses_broad_decoder_without_imagecodecs(self):
        import OpenImageIO as oiio
        pixels = np.repeat(np.arange(4096, dtype=np.uint16).reshape(64, 64, 1), 3, axis=2)
        writer = oiio.ImageOutput.create(str(self.source))
        spec = oiio.ImageSpec(64, 64, 3, oiio.UINT16)
        spec.attribute("compression", "lzw")
        self.assertTrue(writer.open(str(self.source), spec))
        self.assertTrue(writer.write_image(pixels))
        self.assertTrue(writer.close())
        actual = self.convert()
        self.assertEqual(len(np.unique(actual)), 4096)
        np.testing.assert_allclose(actual, pixels / 65535, rtol=0, atol=6e-8)

    def test_linux_jpeg_cache_is_readable_without_optional_codecs(self):
        source = self.root / "source.jpg"
        Image.new("RGB", (12, 8), (30, 60, 90)).save(source)
        platform_image.convert_processed_to_tiff(source, self.output,
            app_root=self.root, output_space="srgb")
        self.assertEqual(tifffile.imread(self.output).shape, (8, 12, 3))
        with tifffile.TiffFile(self.output) as converted:
            self.assertEqual(converted.pages[0].compression, 1)

    def test_invalid_embedded_profile_fails_instead_of_relabeling_samples(self):
        profiles = self.root / "color-profiles"
        profiles.mkdir()
        (profiles / "ProPhoto-v4.icc").write_bytes(gamma_profile(2.0))
        invalid = b"not an ICC profile"
        tifffile.imwrite(self.source, np.full((2, 3, 3), 0.5, dtype=np.float32),
            photometric="rgb", extratags=[(34675,"B",len(invalid),invalid,False)])
        with self.assertRaisesRegex(ValueError, "profile could not be read"):
            self.convert()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
