from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageCms

import platform_image


class PortableImageTests(unittest.TestCase):
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
            self.assertTrue(embedded)

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
