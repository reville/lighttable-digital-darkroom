"""Decoder headers define preview geometry, independently of EXIF thumbnails."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import catalog_scan
import source_geometry


class SourceGeometryTests(unittest.TestCase):
    def test_raw_output_dimensions_override_embedded_preview_and_sensor_sizes(self):
        # Fuji embedded JPEG, reduced Canon RAW, Sony sensor border, portrait.
        for old, decoded, flip, expected in (
            ((2048, 1536), (4032, 3012), 0, (4032, 3012)),
            ((6000, 4000), (3000, 2000), 0, (3000, 2000)),
            ((7168, 5120), (7028, 4688), 0, (7028, 4688)),
            ((4416, 2944), (7752, 5178), 6, (5178, 7752)),
        ):
            with self.subTest(old=old, decoded=decoded, flip=flip):
                raw = mock.MagicMock()
                raw.__enter__.return_value = raw
                raw.sizes = mock.Mock(iwidth=decoded[0], iheight=decoded[1],
                                      flip=flip, pixel_aspect=1.0)
                with mock.patch('rawpy.RawPy', return_value=raw), \
                        mock.patch.object(catalog_scan.media_availability, 'availability', return_value='local'), \
                        mock.patch.object(catalog_scan, '_read_exif_metadata', return_value={
                            'width': old[0], 'height': old[1], 'orientation': 1}):
                    info = catalog_scan.read_metadata(Path('photo.RAF'))
                    self.assertEqual((info['width'], info['height']), decoded)
                    self.assertEqual(source_geometry.dimensions(Path('photo.RAF')), expected)
                    self.assertEqual(info['orientation'], 6 if flip == 6 else 1)
                    self.assertEqual(info['metadata_version'], catalog_scan.METADATA_VERSION)
                raw.unpack.assert_not_called()
                raw.postprocess.assert_not_called()

    def test_processed_header_overrides_stale_exif_dimensions_and_rotates_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'portrait.jpg'
            exif = Image.Exif()
            exif[274] = 6
            exif[40962], exif[40963] = 6000, 4000
            Image.new('RGB', (90, 60)).save(path, exif=exif)
            info = catalog_scan.read_metadata(path)
            self.assertEqual((info['width'], info['height'], info['orientation']), (90, 60, 6))
            self.assertEqual(source_geometry.dimensions(path), (60, 90))

    def test_header_geometry_survives_unavailable_exif_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'photo.png'
            Image.new('RGB', (90, 60)).save(path)
            with mock.patch.object(catalog_scan, '_read_exif_metadata', return_value={}):
                self.assertEqual(catalog_scan.read_metadata(path), {
                    'width': 90, 'height': 60, 'orientation': 1,
                    'metadata_version': catalog_scan.METADATA_VERSION})

    def test_cloud_placeholders_are_not_opened(self):
        with mock.patch.object(catalog_scan.media_availability, 'availability', return_value='cloud'), \
                mock.patch.object(source_geometry, 'metadata') as read:
            self.assertEqual(catalog_scan.read_metadata(Path('photo.RAF')), {})
            read.assert_not_called()

    def test_non_square_decoder_pixels_do_not_claim_exact_geometry(self):
        raw = mock.MagicMock()
        raw.__enter__.return_value = raw
        raw.sizes.pixel_aspect = 2.0
        with mock.patch('rawpy.RawPy', return_value=raw):
            with self.assertRaises(ValueError):
                source_geometry.dimensions(Path('photo.RAF'))
