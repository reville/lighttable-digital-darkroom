# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import numpy as np

import color_pipeline
import platform_image

try:
    import exiv2

    HAVE_EXIV2 = True
except Exception:  # noqa: BLE001 - the binding is optional on some platforms
    exiv2 = None
    HAVE_EXIV2 = False

FIELDS = {
    "title": "Harbour at dusk",
    "caption": "Portra 400 pushed one stop",
    "creator": "Nicholas Reville",
    "copyright": "(c) 2026 Nicholas Reville",
    "keywords": ["harbour", "dusk"],
    "keywordPaths": ["Places|Coast|Harbour"],
    "rating": 4,
    "label": "Green",
}


def _read(path: Path) -> tuple[dict, dict, bytes | None]:
    """Return ``(exif, xmp, icc)`` read back from one file with exiv2."""
    image = exiv2.ImageFactory.open(str(path))
    image.readMetadata()
    exif = {datum.key(): datum.toString() for datum in image.exifData()}
    xmp = {datum.key(): datum.toString() for datum in image.xmpData()}
    icc = (bytes(image.iccProfile().data())
           if image.iccProfileDefined() else None)
    return exif, xmp, icc


@unittest.skipUnless(HAVE_EXIV2, "the exiv2 binding is unavailable")
class WriteMetadataTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.pixels = np.linspace(
            0.05, 0.95, 40 * 60 * 3, dtype=np.float32).reshape(40, 60, 3)
        self.source = self._source()

    def _source(self) -> Path:
        """A camera-like original with EXIF, GPS, and a rotation."""
        source = self.root / "original.jpg"
        color_pipeline.save_export_image(
            self.pixels, source, fmt="jpeg", quality=90, output_space="srgb")
        image = exiv2.ImageFactory.open(str(source))
        image.readMetadata()
        exif = image.exifData()
        exif["Exif.Image.Make"] = "Fujifilm"
        exif["Exif.Image.Model"] = "X-T5"
        exif["Exif.Image.Orientation"] = 6
        exif["Exif.Photo.DateTimeOriginal"] = "2026:01:02 03:04:05"
        exif["Exif.Photo.DateTimeDigitized"] = "2026:02:03 04:05:06"
        exif["Exif.Photo.OffsetTimeOriginal"] = "+02:00"
        exif["Exif.Photo.OffsetTimeDigitized"] = "+01:00"
        exif["Exif.Photo.SubSecTimeOriginal"] = "125"
        exif["Exif.Photo.ISOSpeedRatings"] = 400
        exif["Exif.GPSInfo.GPSLatitudeRef"] = "N"
        exif["Exif.GPSInfo.GPSLatitude"] = "41/1 23/1 12/1"
        exif["Exif.GPSInfo.GPSLongitudeRef"] = "W"
        image.setExifData(exif)
        image.writeMetadata()
        return source

    def _export(self, name: str, fmt: str, output_space: str) -> Path:
        destination = self.root / name
        color_pipeline.save_export_image(
            self.pixels, destination, fmt=fmt, quality=92,
            output_space=output_space)
        return destination

    def test_policies_round_trip_on_jpeg_and_tiff(self):
        cases = (("delivery.jpg", "jpeg", "display_p3"),
                 ("delivery.tif", "tif", "prophoto"))
        for name, fmt, output_space in cases:
            with self.subTest(format=fmt):
                destination = self._export(name, fmt, output_space)
                self.assertTrue(platform_image.write_metadata(
                    destination, self.source, "all-except-location", FIELDS))
                exif, xmp, _ = _read(destination)

                self.assertEqual(exif["Exif.Image.Make"], "Fujifilm")
                self.assertEqual(exif["Exif.Photo.DateTimeOriginal"],
                                 "2026:01:02 03:04:05")
                self.assertEqual(exif["Exif.Photo.OffsetTimeOriginal"], "+02:00")
                self.assertEqual(exif["Exif.Photo.OffsetTimeDigitized"], "+01:00")
                self.assertEqual(exif["Exif.Photo.SubSecTimeOriginal"], "125")
                self.assertEqual(exif["Exif.Image.Software"], "LightTable")
                self.assertEqual(exif["Exif.Image.Orientation"], "1")
                self.assertEqual(exif["Exif.Photo.PixelXDimension"], "60")
                self.assertEqual(exif["Exif.Photo.PixelYDimension"], "40")
                self.assertNotIn("Exif.GPSInfo.GPSLatitude", exif)

                self.assertIn(FIELDS["copyright"], xmp["Xmp.dc.rights"])
                self.assertIn(FIELDS["creator"], xmp["Xmp.dc.creator"])
                self.assertIn(FIELDS["title"], xmp["Xmp.dc.title"])
                self.assertIn(FIELDS["caption"], xmp["Xmp.dc.description"])
                self.assertIn("harbour", xmp["Xmp.dc.subject"])
                self.assertIn("dusk", xmp["Xmp.dc.subject"])
                self.assertEqual(xmp["Xmp.lr.hierarchicalSubject"],
                                 "Places|Coast|Harbour")
                self.assertEqual(xmp["Xmp.xmp.Rating"], "4")
                self.assertEqual(xmp["Xmp.xmp.Label"], "Green")

    def test_capture_clock_override_is_embedded_in_jpeg_and_tiff_with_zone(self):
        source_bytes = self.source.read_bytes()
        for fmt, suffix in (("jpeg", "jpg"), ("tif", "tif")):
            destination = self._export(f"corrected.{suffix}", fmt, "srgb")
            self.assertTrue(platform_image.write_metadata(destination, self.source, "all-except-location",
                {"captureTime": "2026-01-03T04:05:06+05:30"}))
            exif, xmp, _ = _read(destination)
            self.assertEqual(exif["Exif.Photo.DateTimeOriginal"], "2026:01:03 04:05:06")
            self.assertEqual(exif["Exif.Photo.OffsetTimeOriginal"], "+05:30")
            self.assertEqual(exif["Exif.Photo.DateTimeDigitized"], "2026:02:03 04:05:06")
            self.assertEqual(exif["Exif.Photo.OffsetTimeDigitized"], "+01:00")
            self.assertEqual(xmp["Xmp.exif.DateTimeOriginal"], "2026-01-03T04:05:06+05:30")
            self.assertEqual(self.source.read_bytes(), source_bytes)
        rights = self._export("clock-private.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(rights, self.source, "copyright",
            {"captureTime": "2026-01-03T04:05:06+05:30", "copyright": "Example"}))
        exif, xmp, _ = _read(rights)
        self.assertNotIn("Exif.Photo.DateTimeOriginal", exif)
        self.assertNotIn("Xmp.exif.DateTimeOriginal", xmp)

    def test_capture_clock_removing_zone_clears_original_offset_only(self):
        destination = self._export("no-zone.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(destination, self.source, "all",
            {"captureTime": "2026-01-03T04:05:06"}))
        exif, _, _ = _read(destination)
        self.assertNotIn("Exif.Photo.OffsetTimeOriginal", exif)
        self.assertEqual(exif["Exif.Photo.OffsetTimeDigitized"], "+01:00")

    def test_icc_profile_survives_the_rewrite(self):
        cases = (("keep.jpg", "jpeg", "display_p3"),
                 ("keep.tif", "tif", "prophoto"))
        for name, fmt, output_space in cases:
            with self.subTest(format=fmt):
                destination = self._export(name, fmt, output_space)
                _, _, before = _read(destination)
                self.assertEqual(before, color_pipeline.icc_bytes(output_space))

                self.assertTrue(platform_image.write_metadata(
                    destination, self.source, "all", FIELDS))
                _, _, after = _read(destination)
                self.assertIsNotNone(after)
                self.assertEqual(before, after)

    def test_tiff_pixels_are_untouched_by_the_metadata_pass(self):
        import tifffile

        destination = self._export("pixels.tif", "tif", "prophoto")
        before = tifffile.imread(destination)
        self.assertTrue(platform_image.write_metadata(
            destination, self.source, "all", FIELDS))
        self.assertTrue(np.array_equal(before, tifffile.imread(destination)))

    def test_all_keeps_location_and_all_except_location_drops_it(self):
        keep = self._export("keep-gps.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(keep, self.source, "all"))
        kept, _, _ = _read(keep)
        self.assertEqual(kept["Exif.GPSInfo.GPSLatitude"], "41/1 23/1 12/1")
        self.assertEqual(kept["Exif.GPSInfo.GPSLatitudeRef"], "N")

        dropped = self._export("drop-gps.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(
            dropped, self.source, "all-except-location"))
        stripped, _, _ = _read(dropped)
        self.assertFalse(
            [key for key in stripped if key.startswith("Exif.GPSInfo")])
        self.assertEqual(stripped["Exif.Image.Make"], "Fujifilm")

    def test_copyright_policy_writes_rights_only(self):
        destination = self._export("rights.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(
            destination, self.source, "copyright", FIELDS))
        exif, xmp, icc = _read(destination)

        self.assertIn(FIELDS["copyright"], xmp["Xmp.dc.rights"])
        self.assertIn(FIELDS["creator"], xmp["Xmp.dc.creator"])
        self.assertNotIn("Xmp.dc.title", xmp)
        self.assertNotIn("Xmp.dc.subject", xmp)
        self.assertNotIn("Xmp.xmp.Rating", xmp)
        self.assertNotIn("Exif.Image.Make", exif)
        self.assertFalse(
            [key for key in exif if key.startswith("Exif.GPSInfo")])
        self.assertEqual(exif["Exif.Image.Orientation"], "1")
        self.assertEqual(exif["Exif.Image.Software"], "LightTable")
        self.assertEqual(icc, color_pipeline.icc_bytes("srgb"))

    def test_none_policy_leaves_the_file_alone(self):
        destination = self._export("bare.jpg", "jpeg", "srgb")
        original = destination.read_bytes()
        self.assertFalse(platform_image.write_metadata(
            destination, self.source, "none", FIELDS))
        self.assertEqual(destination.read_bytes(), original)

    def test_failures_return_false_instead_of_raising(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            self.assertFalse(platform_image.write_metadata(
                self.root / "missing.jpg", self.source, "all", FIELDS))
        self.assertIn("missing.jpg", log.getvalue())

        destination = self._export("no-source.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(
            destination, self.root / "gone.jpg", "all", FIELDS))
        exif, xmp, _ = _read(destination)
        self.assertEqual(exif["Exif.Image.Software"], "LightTable")
        self.assertIn(FIELDS["title"], xmp["Xmp.dc.title"])

    def test_unknown_policy_falls_back_to_all_except_location(self):
        destination = self._export("fallback.jpg", "jpeg", "srgb")
        self.assertTrue(platform_image.write_metadata(
            destination, self.source, "everything", FIELDS))
        exif, _, _ = _read(destination)
        self.assertEqual(exif["Exif.Image.Make"], "Fujifilm")
        self.assertFalse(
            [key for key in exif if key.startswith("Exif.GPSInfo")])


if __name__ == "__main__":
    unittest.main()
