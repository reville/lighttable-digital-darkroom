# SPDX-License-Identifier: GPL-3.0-only
"""Different valid photographs must never share preview or recovery identities."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image
import server


class SourceContentIdentityTests(unittest.TestCase):
    def test_equal_prefix_size_and_timestamp_keep_distinct_thumbnail_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / 'cache'
            (cache / 'thumb').mkdir(parents=True)
            first = np.full((256, 256, 3), 128, dtype=np.uint8)
            second = first.copy(); second[128:] = [20, 210, 40]
            for name, pixels in [('a.tif', first), ('b.tif', second)]:
                Image.fromarray(pixels).save(root / name, compression='raw')
                os.utime(root / name, ns=(1000000000, 1000000000))
            self.assertEqual((root/'a.tif').read_bytes()[:65536], (root/'b.tif').read_bytes()[:65536])
            with mock.patch.object(server, 'FOLDER', root), mock.patch.object(server, 'CATALOG', None), \
                    mock.patch.object(server, 'CACHE', cache), mock.patch.dict(server._HEADER_HASH_CACHE, clear=True):
                a = server.thumb_jpeg('a.tif'); b = server.thumb_jpeg('b.tif')
                self.assertNotEqual(server.file_key('a.tif'), server.file_key('b.tif'))
                with Image.open(io.BytesIO(a)) as one, Image.open(io.BytesIO(b)) as two:
                    self.assertGreater(np.abs(np.asarray(one).astype(float) - np.asarray(two)).mean(), 20)

    def test_atomic_replacement_with_preserved_size_and_mtime_invalidates_warm_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root/'photo.tif'; replacement = root/'new.tif'
            pixels = np.full((256, 256, 3), 128, dtype=np.uint8)
            Image.fromarray(pixels).save(path, compression='raw')
            before = path.stat()
            with mock.patch.object(server, 'FOLDER', root), mock.patch.object(server, 'CATALOG', None), \
                    mock.patch.dict(server._HEADER_HASH_CACHE, clear=True):
                original = server.file_key('photo.tif')
                pixels[128:] = 32
                Image.fromarray(pixels).save(replacement, compression='raw')
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(replacement, path)
                self.assertNotEqual(server.file_key('photo.tif'), original)
