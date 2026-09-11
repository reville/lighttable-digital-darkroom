# SPDX-License-Identifier: GPL-3.0-only
from enum import Enum
from types import SimpleNamespace
import unittest
from unittest import mock

import raw_decode_runtime


class RawDecodeRuntimeTests(unittest.TestCase):
    def runtimes(self, *, xtrans=False):
        class Error(Exception):
            pass
        class NativeError(Exception):
            pass
        stock_raw = mock.MagicMock()
        stock_raw.__enter__.return_value = stock_raw
        accelerated_raw = mock.MagicMock(is_xtrans=xtrans)
        accelerated_raw.__enter__.return_value = accelerated_raw
        stock = SimpleNamespace(imread=mock.Mock(return_value=stock_raw),
                                LibRawError=Error)
        accelerated = SimpleNamespace(imread=mock.Mock(return_value=accelerated_raw),
                                      LibRawError=NativeError)
        return stock, accelerated, stock_raw, accelerated_raw

    def test_bayer_uses_one_header_and_parallel_decoder(self):
        stock, accelerated, _, raw = self.runtimes()
        with mock.patch.object(raw_decode_runtime.importlib, "import_module",
                               side_effect=[stock, accelerated]):
            with raw_decode_runtime.open_raw("photo.dng") as result:
                self.assertEqual(result, (raw, accelerated))
        stock.imread.assert_not_called()
        accelerated.imread.assert_called_once_with("photo.dng")
        raw.__exit__.assert_called_once()

    def test_xtrans_closes_probe_and_preserves_stock_decoder(self):
        stock, accelerated, stock_raw, probe = self.runtimes(xtrans=True)
        with mock.patch.object(raw_decode_runtime.importlib, "import_module",
                               side_effect=[stock, accelerated]):
            with raw_decode_runtime.open_raw("photo.raf") as result:
                self.assertEqual(result, (stock_raw, stock))
                probe.close.assert_called_once()
                probe.unpack.assert_not_called()
        stock_raw.__exit__.assert_called_once()

    def test_optional_accelerator_absence_keeps_portable_path(self):
        stock, _, raw, _ = self.runtimes()
        with mock.patch.object(raw_decode_runtime.importlib, "import_module",
                               side_effect=[stock, ImportError("not installed")]):
            with raw_decode_runtime.open_raw("photo.dng") as result:
                self.assertEqual(result, (raw, stock))

    def test_verified_xtrans_schedule_uses_parallel_decoder_without_reopening(self):
        stock, accelerated, _, raw = self.runtimes(xtrans=True)
        accelerated.LIGHTTABLE_XTRANS_WAVEFRONT = 1
        with mock.patch.object(raw_decode_runtime.importlib, "import_module",
                               side_effect=[stock, accelerated]):
            with raw_decode_runtime.open_raw("photo.raf") as result:
                self.assertEqual(result, (raw, accelerated))
        stock.imread.assert_not_called()
        raw.close.assert_not_called()
        raw.__exit__.assert_called_once()

    def test_translate_native_exception_to_existing_public_error(self):
        stock, accelerated, _, _ = self.runtimes()
        with mock.patch.object(raw_decode_runtime.importlib, "import_module",
                               side_effect=[stock, accelerated]):
            with self.assertRaisesRegex(stock.LibRawError, "decode failed"):
                with raw_decode_runtime.open_raw("photo.dng"):
                    raise accelerated.LibRawError("decode failed")

    def test_options_preserve_values_across_extension_enum_classes(self):
        class ColorSpace(Enum):
            ProPhoto = 4
        NativeColorSpace = Enum("ColorSpace", {"ProPhoto": 4})
        options = {"output_color": ColorSpace.ProPhoto, "gamma": (1, 1),
                   "half_size": True}
        converted = raw_decode_runtime.native_options(
            options, SimpleNamespace(ColorSpace=NativeColorSpace))
        self.assertIs(converted["output_color"], NativeColorSpace.ProPhoto)
        self.assertEqual(converted["gamma"], (1, 1))
        self.assertTrue(converted["half_size"])


if __name__ == "__main__":
    unittest.main()
