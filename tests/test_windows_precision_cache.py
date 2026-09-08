"""Existing installations must not reuse old low-precision export inputs."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import tifffile

import server


class ProcessedPrecisionCacheTests(unittest.TestCase):
    def exercise_cache(self, platform, raw=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"source identity; decoder is substituted")
            pixels = np.arange(8 * 6 * 3, dtype=np.uint16).reshape(6, 8, 3) * 311
            old_key = "wb" if raw else "romm"
            targets = []
            for category in ("tiff", "neutral"):
                folder = root / category
                folder.mkdir()
                old = folder / f"v{server.INPUT_CACHE_VERSION}_key_{old_key}.tif"
                tifffile.imwrite(old, np.zeros((6, 8, 3), np.uint8), photometric="rgb")
                targets.append(old)

            for target, name, value in (
                (server, "CACHE", root), (server.sys, "platform", platform),
                (server, "src_path", mock.Mock(return_value=source)),
                (server, "file_key", mock.Mock(return_value="key")),
                (server, "is_raw", mock.Mock(return_value=raw)),
                (server.color_pipeline, "raw_decode_fingerprint", mock.Mock(return_value="wb")),
            ):
                stack.enter_context(mock.patch.object(target, name, value))

            def convert(_source, destination, **_kwargs):
                tifffile.imwrite(destination, pixels, photometric="rgb")

            conversion = stack.enter_context(mock.patch.object(
                server.platform_image, "convert_processed_to_tiff", side_effect=convert))
            for operation, old in zip((server.tiff_for, server.neutral_tiff_for), targets):
                actual = operation("source.tif")
                if platform != "darwin" and not raw:
                    self.assertNotEqual(actual, old)
                    np.testing.assert_array_equal(tifffile.imread(actual), pixels)
                    self.assertEqual(tifffile.imread(old).dtype, np.uint8)
                else:
                    self.assertEqual(actual, old)
                self.assertEqual(operation("source.tif"), actual)
            self.assertEqual(conversion.call_count, 2 if platform != "darwin" and not raw else 0)

    def test_windows_rebuilds_old_processed_inputs_and_reuses_new_ones(self):
        self.exercise_cache("win32")

    def test_linux_rebuilds_old_processed_inputs_and_reuses_new_ones(self):
        self.exercise_cache("linux")

    def test_macos_keeps_existing_processed_input_caches(self):
        self.exercise_cache("darwin")

    def test_raw_input_cache_identity_is_unchanged_on_all_platforms(self):
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                self.exercise_cache(platform, raw=True)


if __name__ == "__main__":
    unittest.main()
