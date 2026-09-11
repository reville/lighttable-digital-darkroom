# SPDX-License-Identifier: GPL-3.0-only
"""A full Linux /dev/shm must fall back to TIFF before any mapped page is touched."""
from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import server


class Segment:
    def __init__(self, size):
        self._fd = 71
        self.name = "psm_test"
        self.bytes = bytearray(size)
        self.touched = False
        self.closed = False
        self.unlinked = False

    @property
    def buf(self):
        self.touched = True
        return self.bytes

    def close(self):
        self.closed = True

    def unlink(self):
        self.unlinked = True


class LinuxSharedInputTests(unittest.TestCase):
    def setUp(self):
        self.rgb = np.array([[[0, 32768, 65535], [7, 8, 9]]], dtype=np.uint16)
        self.length = server.RAW_SHARED_HEADER.size + self.rgb.nbytes
        self.segment = Segment(self.length)
        self.platform_patch = mock.patch.object(server.sys, "platform", "linux")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.segment_patch = mock.patch("multiprocessing.shared_memory.SharedMemory", return_value=self.segment)
        self.segment_patch.start()
        self.addCleanup(self.segment_patch.stop)

    def test_reserves_backing_pages_before_header_or_pixels_are_written(self):
        def reserve(fd, offset, size):
            self.assertEqual((fd, offset, size), (71, 0, self.length))
            self.assertFalse(self.segment.touched)

        with mock.patch.object(server.os, "posix_fallocate", side_effect=reserve, create=True) as allocation:
            with server.array_shared_input(self.rgb, "fixture") as request:
                allocation.assert_called_once()
                self.assertEqual(request["input_shm_len"], self.length)
                self.assertEqual(server.RAW_SHARED_HEADER.unpack_from(self.segment.bytes),
                                 (server.RAW_SHARED_MAGIC, 2, 1, 12))
                pixels = np.frombuffer(self.segment.bytes, dtype="<u2", offset=server.RAW_SHARED_HEADER.size)
                np.testing.assert_array_equal(pixels, self.rgb.reshape(-1))
                del pixels
        self.assertTrue(self.segment.closed)
        self.assertTrue(self.segment.unlinked)

    def test_full_shared_memory_cleans_up_without_touching_pages(self):
        with mock.patch.object(server.os, "posix_fallocate", create=True,
                               side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError) as error:
                with server.array_shared_input(self.rgb, "fixture"):
                    self.fail("an unreserved shared segment was exposed")
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertFalse(self.segment.touched)
        self.assertTrue(self.segment.closed)
        self.assertTrue(self.segment.unlinked)

    def test_reservation_failure_finishes_full_render_through_tiff(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.tif"
            source.write_bytes(b"cached input")
            engine = mock.Mock()
            engine.probe_input.return_value = False
            engine.render.return_value = {"width": 2, "height": 1}
            with mock.patch.object(server.os, "posix_fallocate", create=True,
                                   side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                    mock.patch.object(server, "shared_input_supported", return_value=True), \
                    mock.patch.object(server, "is_raw", return_value=True), \
                    mock.patch.object(server, "file_key", return_value="source"), \
                    mock.patch.object(server, "src_path", return_value=source), \
                    mock.patch.object(server.color_pipeline, "decode_raw", return_value=self.rgb), \
                    mock.patch.object(server, "BACKGROUND_ENGINE", engine), \
                    mock.patch.object(server, "tiff_for", return_value=source):
                result = server._resident_render_full("frame.dng", {}, {})
            self.assertEqual(result["input_transport"], "tiff-fallback")
            self.assertEqual(result["input_fallback"], "OSError")
            self.assertEqual(engine.render.call_args.args[0]["input"], str(source))
            self.assertNotIn("input_shm", engine.render.call_args.args[0])
        self.assertFalse(self.segment.touched)
        self.assertTrue(self.segment.unlinked)

    def test_macos_does_not_use_linux_reservation(self):
        with mock.patch.object(server.sys, "platform", "darwin"), \
                mock.patch.object(server.os, "posix_fallocate", create=True) as allocation:
            with server.array_shared_input(self.rgb, "fixture"):
                allocation.assert_not_called()
        self.assertTrue(self.segment.touched)


if __name__ == "__main__":
    unittest.main()
