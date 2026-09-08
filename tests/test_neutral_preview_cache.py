"""Develop conversion is shared across zoom sizes, with capture-safe identity."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import server
from raw_decode_cache import DecodedRawCache


class NeutralPreviewCacheTests(unittest.TestCase):
    def test_zoom_reuses_development_but_capture_changes_invalidate_it(self):
        pixels = np.linspace(0, 1, 192 * 128 * 3, dtype=np.float32).reshape(128, 192, 3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'photo.dng'
            source.write_bytes(b'first raw')
            cache = DecodedRawCache(512 * 1024)
            with (
                mock.patch.object(server, 'CACHE', root / 'cache'),
                mock.patch.object(server, 'NEUTRAL_DISPLAY_CACHE', cache),
                mock.patch.object(server, 'src_path', return_value=source),
                mock.patch.object(server, 'file_key', return_value='capture'),
                mock.patch.object(server.color_pipeline, 'decode_raw', return_value=pixels) as decode,
                mock.patch.object(server.color_pipeline, 'linear_prophoto_to_display_srgb',
                                  side_effect=lambda image, params: image) as develop,
            ):
                first = server.build_neutral_preview('photo.dng', 64)
                larger = server.build_neutral_preview('photo.dng', 96)
                rotated = server.build_neutral_preview('photo.dng', 96, 90)
                self.assertEqual(develop.call_count, 1)
                with Image.open(first) as image:
                    self.assertEqual(image.size, (64, 43))
                with Image.open(larger) as image:
                    self.assertEqual(image.size, (96, 64))
                with Image.open(rotated) as image:
                    self.assertEqual(image.size, (64, 96))
                server.build_neutral_preview('photo.dng', 64, params={'developProfile': 'linear'})
                self.assertEqual(develop.call_count, 2, 'a new tone curve is a different conversion')
                source.write_bytes(b'replaced raw')
                server.build_neutral_preview('photo.dng', 128)
                self.assertEqual(develop.call_count, 3, 'replacement must not reuse the old capture')
                decode.return_value = pixels.repeat(2, axis=0).repeat(2, axis=1)
                server.build_neutral_preview('photo.dng', 160)
                self.assertEqual(develop.call_count, 4, 'full and half demosaics stay distinct')
                self.assertLessEqual(cache.stats()['bytes'], cache.stats()['budget'])

    def test_rgb16_cache_keeps_preview_rounding_below_one_code_value(self):
        pixels = np.random.default_rng(17).random((64, 96, 3), dtype=np.float32)
        expected = server.color_pipeline.resize_float_width(pixels, 48)
        expected = (expected * 255 + .5).astype(np.uint8)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(server, 'CACHE', Path(directory)), \
                mock.patch.object(server, 'src_path', return_value=Path(directory) / 'missing.dng'), \
                mock.patch.object(server, 'file_key', return_value='precision'), \
                mock.patch.object(server.color_pipeline, 'decode_raw', return_value=pixels), \
                mock.patch.object(server.color_pipeline, 'linear_prophoto_to_display_srgb', return_value=pixels), \
                mock.patch.object(server, 'jpeg_bytes', return_value=b'jpeg') as encode:
            server.build_neutral_preview('missing.dng', 48)
            actual = encode.call_args.args[0]
            self.assertLessEqual(np.abs(actual.astype(int) - expected.astype(int)).max(), 1)

    def test_purge_releases_developed_pixels(self):
        cache = DecodedRawCache(1024)
        cache.get_or_build('capture', lambda: np.ones((4, 4, 3), dtype=np.uint16), lambda: None)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(server, 'CACHE', Path(directory)), \
                mock.patch.object(server, 'NEUTRAL_DISPLAY_CACHE', cache):
            server.purge_generated_cache()
        self.assertEqual(cache.stats()['bytes'], 0)
