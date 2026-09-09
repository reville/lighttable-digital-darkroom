"""Real cache tiers and bounded import backfill for the library grid."""
import io
from pathlib import Path
from unittest import mock

from PIL import Image

import server
from tests.test_server_catalog import CatalogServerTestCase


class GridThumbnailTests(CatalogServerTestCase):
    def test_grid_ignores_legacy_small_cache_and_reuses_large_cache(self):
        Image.new('RGB', (2400, 1600), 'red').save(self.root / 'a.jpg')
        cache = Path(self._dir.name) / 'cache'
        (cache / 'thumb').mkdir(parents=True)
        with mock.patch.object(server, 'CACHE', cache):
            small = server.thumb_jpeg('a.jpg')
            large = server.thumb_jpeg('a.jpg', 1024)
            with Image.open(io.BytesIO(small)) as image:
                self.assertEqual(max(image.size), 240)
            with Image.open(io.BytesIO(large)) as image:
                self.assertEqual(max(image.size), 1024)
            with mock.patch.object(server, '_build_thumb') as build:
                self.assertEqual(server.thumb_jpeg('a.jpg', 1024), large)
                self.assertEqual(server.thumb_jpeg('a.jpg', 999999), large)
            build.assert_not_called()

    def test_import_prepares_large_tier_used_by_grid(self):
        Image.new('RGB', (1800, 1200), 'blue').save(self.root / 'a.jpg')
        cache = Path(self._dir.name) / 'cache'
        (cache / 'thumb').mkdir(parents=True)
        with mock.patch.object(server, 'CACHE', cache):
            server._warm_thumbnail(self.qualified('a.jpg'))
            with mock.patch.object(server, '_build_thumb') as build:
                payload = server.thumb_jpeg(self.qualified('a.jpg'), 1024)
            build.assert_not_called()
            with Image.open(io.BytesIO(payload)) as image:
                self.assertEqual(max(image.size), 1024)

    def test_backfill_pages_only_available_local_photos(self):
        rows = server._thumbnail_backlog(self.source, 0, 1)
        self.assertEqual(len(rows), 1)
        following = server._thumbnail_backlog(self.source, rows[0][0], 1)
        self.assertEqual(len(following), 1)
        self.assertNotEqual(rows[0][1], following[0][1])
        self.assertEqual(server._thumbnail_backlog(self.source, following[0][0], 1), [])
        with self.catalog.write() as conn:
            conn.execute("UPDATE files SET availability='cloud-only' WHERE relpath='a.jpg'")
            conn.execute("UPDATE files SET missing=1 WHERE relpath='sub/b.jpg'")
        self.assertEqual(server._thumbnail_backlog(self.source, 0, 100), [])
