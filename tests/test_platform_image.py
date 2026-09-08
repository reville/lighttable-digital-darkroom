from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile
from PIL import Image, ImageCms, ImageOps

import platform_image


class PortableImageTests(unittest.TestCase):
    @staticmethod
    def _profile_root(root: Path) -> bytes:
        profiles = root / "color-profiles"
        profiles.mkdir()
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        (profiles / "sRGB-v4.icc").write_bytes(profile)
        return profile

    def test_macos_thumbnail_routes_through_in_process_imageio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "thumb.jpg"
            Image.new("RGB", (40, 20), "black").save(source, "JPEG")
            with mock.patch.object(platform_image.sys, "platform", "darwin"), \
                    mock.patch.object(
                        platform_image, "_build_thumbnail_imageio") as native:
                platform_image.build_thumbnail(source, output)
            native.assert_called_once_with(source, output)

    def test_jpeg_icc_is_inserted_without_reencoding_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "profiled.jpg"
            profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")).tobytes()
            Image.new("RGB", (24, 16), (20, 40, 60)).save(
                output, "JPEG", quality=90)
            before = Image.open(output).tobytes()

            platform_image.embed_jpeg_icc(output, profile)

            with Image.open(output) as image:
                self.assertEqual(image.info["icc_profile"], profile)
                self.assertEqual(image.tobytes(), before)

    def test_portable_thumbnail_applies_exif_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "landscape.jpg"
            output = Path(directory) / "thumb.jpg"
            image = Image.new("RGB", (400, 200), (80, 120, 160))
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, "JPEG", exif=exif)

            platform_image.build_thumbnail(
                source, output, force_portable=True)

            with Image.open(output) as thumbnail:
                self.assertEqual(thumbnail.size, (120, 240))

    def test_portable_tiff_conversion_keeps_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "output.tif"
            Image.new("RGB", (48, 32), (32, 64, 96)).save(source, "JPEG")

            platform_image.convert_processed_to_tiff(
                source,
                output,
                app_root=Path(__file__).resolve().parents[1],
                output_space="srgb",
                force_portable=True,
            )

            with Image.open(output) as converted:
                self.assertEqual(converted.size, (48, 32))
                self.assertEqual(converted.mode, "RGB")

    def test_windows_conversion_uses_bundled_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "color-profiles"
            profiles.mkdir()
            profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes()
            for filename in platform_image.PROFILE_FILENAMES.values():
                (profiles / filename).write_bytes(profile)
            source = root / "source.jpg"
            output = root / "output.tif"
            Image.new("RGB", (20, 10), (32, 64, 96)).save(source, "JPEG")

            with mock.patch.object(platform_image.sys, "platform", "win32"):
                self.assertEqual(
                    platform_image.profile_path(root, "display_p3"),
                    profiles / "DisplayP3-v4.icc",
                )
                platform_image.convert_processed_to_tiff(
                    source,
                    output,
                    app_root=root,
                    output_space="display_p3",
                    force_portable=True,
                )

            with Image.open(output) as converted:
                self.assertTrue(converted.info.get("icc_profile"))

    def test_portable_tiff_conversion_preserves_16_bit_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile_root(root)
            ramp = np.linspace(0, 65535, 4096, dtype=np.uint16)
            pixels = np.stack((ramp, ramp[::-1], np.roll(ramp, 73)), axis=-1)[None]
            source, output = root / "source.tif", root / "output.tif"
            tifffile.imwrite(source, pixels, photometric="rgb", iccprofile=profile)

            with mock.patch.object(platform_image.sys, "platform", "win32"):
                platform_image.convert_processed_to_tiff(
                    source, output, app_root=root, output_space="srgb")

            with tifffile.TiffFile(output) as converted:
                result = converted.asarray()
                self.assertEqual(result.dtype, np.uint16)
                self.assertEqual(converted.pages[0].tags[34675].value, profile)
                self.assertEqual(np.unique(result[..., 0]).size, 4096)
                np.testing.assert_array_equal(result, pixels)

    def test_portable_tiff_conversion_preserves_float_precision_and_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile_root(root)
            ramp = np.linspace(-0.125, 1.25, 4096, dtype=np.float32)
            pixels = np.repeat(ramp[None, :, None], 3, axis=2)
            source, output = root / "source.tif", root / "output.tif"
            tifffile.imwrite(source, pixels, photometric="rgb", iccprofile=profile)

            with mock.patch.object(platform_image.sys, "platform", "win32"):
                platform_image.convert_processed_to_tiff(
                    source, output, app_root=root, output_space="srgb")

            result = tifffile.imread(output)
            self.assertEqual(result.dtype, np.float32)
            self.assertEqual(np.unique(result[..., 0]).size, 4096)
            np.testing.assert_allclose(result, pixels, rtol=0, atol=1e-7)

    def test_portable_conversion_preserves_16_bit_rgb_and_gray_png(self):
        import imagecodecs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._profile_root(root)
            ramp = np.linspace(0, 65535, 4096, dtype=np.uint16)[None]
            rgb = np.repeat(ramp[..., None], 3, axis=2)
            for pixels in (ramp, rgb):
                with self.subTest(channels=pixels.ndim):
                    source, output = root / "source.png", root / "output.tif"
                    source.write_bytes(imagecodecs.png_encode(pixels))
                    with mock.patch.object(platform_image.sys, "platform", "win32"):
                        platform_image.convert_processed_to_tiff(
                            source, output, app_root=root, output_space="srgb")
                    np.testing.assert_array_equal(tifffile.imread(output), rgb)

    def test_portable_16_bit_conversion_applies_all_exif_orientations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile_root(root)
            original = np.arange(6 * 10 * 3, dtype=np.uint8).reshape(6, 10, 3)
            pixels = original.astype(np.uint16) * 257
            source, output = root / "source.tif", root / "output.tif"
            for orientation in range(1, 9):
                with self.subTest(orientation=orientation):
                    reference = Image.fromarray(original)
                    reference.getexif()[274] = orientation
                    expected = np.asarray(ImageOps.exif_transpose(reference)).astype(np.uint16) * 257
                    tifffile.imwrite(
                        source, pixels, photometric="rgb", iccprofile=profile,
                        extratags=[(274, "H", 1, orientation, False)])
                    with mock.patch.object(platform_image.sys, "platform", "win32"):
                        platform_image.convert_processed_to_tiff(
                            source, output, app_root=root, output_space="srgb")
                    with tifffile.TiffFile(output) as converted:
                        np.testing.assert_array_equal(converted.asarray(), expected)
                        self.assertEqual(converted.pages[0].tags.get(274, 1), 1)

    def test_portable_color_conversion_agrees_with_pillow_icc_reference(self):
        import imagecodecs

        pixels = np.random.default_rng(15).integers(0, 256, (16, 32, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._profile_root(root)
            # Distinct valid ICCs exercise source/target selection without
            # requiring downloaded packaging assets in an offline unit test.
            (root / "color-profiles" / "DisplayP3-v4.icc").write_bytes(
                imagecodecs.cms_profile("adobergb"))
            (root / "color-profiles" / "ProPhoto-v4.icc").write_bytes(
                imagecodecs.cms_profile("linearrgb"))
            source, output = root / "source.png", root / "output.tif"
            for source_space, target_space in (("srgb", "display_p3"),
                                                ("display_p3", "prophoto"),
                                                ("prophoto", "srgb")):
                with self.subTest(source_space=source_space, target_space=target_space), \
                        mock.patch.object(platform_image.sys, "platform", "win32"):
                    source_profile = platform_image.profile_path(root, source_space).read_bytes()
                    target_profile = platform_image.profile_path(root, target_space).read_bytes()
                    image = Image.fromarray(pixels)
                    image.save(source, icc_profile=source_profile)
                    reference = ImageCms.profileToProfile(
                        image,
                        ImageCms.ImageCmsProfile(io.BytesIO(source_profile)),
                        ImageCms.ImageCmsProfile(io.BytesIO(target_profile)),
                        outputMode="RGB", renderingIntent=ImageCms.Intent.PERCEPTUAL,
                        flags=ImageCms.Flags.NOOPTIMIZE)
                    platform_image.convert_processed_to_tiff(
                        source, output, app_root=root, output_space=target_space)
                    result = tifffile.imread(output)
                    self.assertEqual(result.dtype, np.uint16)
                    np.testing.assert_allclose(result / 257.0, np.asarray(reference),
                                               rtol=0, atol=1.0)
                    with tifffile.TiffFile(output) as converted:
                        self.assertEqual(converted.pages[0].tags[34675].value, target_profile)

    def test_portable_conversion_drops_alpha_without_darkening_color(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile_root(root)
            pixels = np.full((4, 8, 4), (32111, 17222, 45333, 16000), dtype=np.uint16)
            source, output = root / "source.tif", root / "output.tif"
            tifffile.imwrite(source, pixels, photometric="rgb", iccprofile=profile,
                             extrasamples="unassalpha")
            with mock.patch.object(platform_image.sys, "platform", "win32"):
                platform_image.convert_processed_to_tiff(
                    source, output, app_root=root, output_space="srgb")
            np.testing.assert_array_equal(tifffile.imread(output), pixels[..., :3])

    def test_macos_full_resolution_conversion_still_uses_sips(self):
        with mock.patch.object(platform_image, "_use_macos_tools", return_value=True), \
                mock.patch.object(platform_image, "orientation_degrees", return_value=90), \
                mock.patch.object(platform_image.subprocess, "run") as run, \
                mock.patch.object(platform_image, "_open_portable_full_precision") as portable:
            platform_image.convert_processed_to_tiff(
                Path("source.tif"), Path("output.tif"),
                app_root=Path(__file__).resolve().parents[1], output_space="srgb")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0], "sips")
            self.assertEqual(run.call_args_list[1].args[0], ["sips", "-r", "90", "output.tif"])
            portable.assert_not_called()

    def test_broad_decoder_retains_embedded_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profiled.jpg"
            profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes()
            Image.new("RGB", (8, 8), "black").save(
                source, "JPEG", icc_profile=profile)

            with mock.patch.object(
                platform_image.Image, "open", side_effect=OSError("fallback")
            ):
                image, embedded = platform_image._open_portable(source)

            self.assertEqual(image.size, (8, 8))
            self.assertEqual(embedded, profile)

    def test_portable_metadata_reads_standard_exif(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "metadata.jpg"
            exif = Image.Exif()
            exif[272] = "Test Camera"
            Image.new("RGB", (8, 8), "black").save(source, "JPEG", exif=exif)

            values = platform_image.metadata(source, force_portable=True)

            self.assertEqual(values["Model"], "Test Camera")
            self.assertIn("FileSize", values)


if __name__ == "__main__":
    unittest.main()
