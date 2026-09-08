from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import tempfile

import catalog
import catalog_scan
import media_availability


class StorageReadinessTests(TestCase):
    def test_index_probe_distinguishes_empty_missing_and_cloud_without_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty, local = root / 'empty.HIF', root / 'local.jpg'
            empty.touch()
            local.write_bytes(b'photo bytes')
            with mock.patch.object(Path, 'open', side_effect=AssertionError('must not read content')):
                self.assertEqual(media_availability.index_availability(empty), 'empty')
                self.assertEqual(media_availability.index_availability(local), 'local')
                self.assertEqual(media_availability.index_availability(root / 'missing.RAF'), 'unavailable')
                with mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_flags=0x40000000, st_size=0)):
                    self.assertEqual(media_availability.index_availability(empty), 'cloud-only')

    def test_stat_only_probe_never_opens_placeholder_and_portable_stat_is_local(self):
        path = Path('/not-a-real-original/photo.RAF')
        with mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_flags=0x40000000)), \
             mock.patch.object(Path, 'open', side_effect=AssertionError('must not hydrate')):
            self.assertEqual(media_availability.availability(path), 'cloud-only')
            self.assertEqual(catalog_scan.header_hash(path), '')
            self.assertEqual(catalog_scan.read_metadata(path), {})
            with self.assertRaisesRegex(OSError, 'Download Now'):
                media_availability.require_local(path)
        self.assertEqual(media_availability.from_stat(SimpleNamespace()), 'local')

    def test_eviction_keeps_identity_metadata_edits_and_hydration_backfills_unchanged_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / 'photos'
            photos.mkdir()
            for i in range(240):
                (photos / f'{i:04}.jpg').write_bytes(f'unique capture {i}'.encode() * 30)
            cat = catalog.Catalog(root / 'catalog.sqlite3')
            source = cat.add_source(photos)
            real_walk, real_hash = catalog_scan.walk_source, catalog_scan.header_hash
            cloud = {f'{i:04}.jpg' for i in range(0, 240, 3)}
            content_reads = []

            def walk(*args, **kwargs):
                for record in real_walk(*args, **kwargs):
                    record['availability'] = 'cloud-only' if record['filename'] in cloud else 'local'
                    yield record

            def hashed(path):
                self.assertNotIn(path.name, cloud, 'placeholder fingerprint would hydrate content')
                content_reads.append(path.name)
                return real_hash(path)

            def metadata(path):
                self.assertNotIn(path.name, cloud, 'placeholder metadata would hydrate content')
                return {'metadata_version': catalog_scan.METADATA_VERSION, 'camera_model': path.stem,
                        'capture_time': '2024-03-04T12:30:00', 'width': 640, 'height': 480}

            try:
                with mock.patch.object(catalog_scan, 'walk_source', side_effect=walk), \
                     mock.patch.object(catalog_scan, 'header_hash', side_effect=hashed), \
                     mock.patch.object(catalog_scan, 'read_metadata', side_effect=metadata):
                    result = catalog_scan.scan_source(cat, source)
                    self.assertEqual((result['added'], result['cloudOnly']), (240, 80))
                    rows = {r['filename']: r for r in cat.query({'limit': 500})['items']}
                    selected = rows['0001.jpg']
                    cat.save_state(selected['id'], {'rating': 5, 'grade': {'exposure': .3}})
                    cloud.add('0001.jpg')
                    before_hash = selected['fileKey']
                    catalog_scan.scan_source(cat, source)
                    row = next(r for r in cat.query({'limit': 500})['items'] if r['filename'] == '0001.jpg')
                    self.assertEqual((row['availability'], row['fileKey'], row['width'], row['rating']),
                                     ('cloud-only', before_hash, 640, 5))
                    self.assertEqual(cat.state_for(selected['id'])['grade'], {'exposure': .3})
                    # Downloading can preserve the exact original stat tuple.
                    cloud.clear()
                    content_reads.clear()
                    catalog_scan.scan_source(cat, source)
                    self.assertEqual(len(content_reads), 80)
                    restored = cat.query({'limit': 500})['items']
                    self.assertTrue(all(r['availability'] == 'local' and r['width'] == 640 and r['fileKey'] for r in restored))
                    self.assertEqual(cat.state_for(selected['id'])['rating'], 5)
                    content_reads.clear()
                    catalog_scan.scan_source(cat, source)
                    self.assertEqual(content_reads, [], 'warm scan should not fingerprint again')
                cat.close()
                cat = catalog.Catalog(root / 'catalog.sqlite3')
                self.assertEqual(cat.query({'limit': 1})['items'][0]['availability'], 'local')
            finally:
                cat.close()

    def test_schema_four_migration_retains_existing_capture(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.sqlite3'
            conn = sqlite3.connect(path)
            conn.executescript(catalog._SCHEMA.replace("    availability TEXT NOT NULL DEFAULT 'local',\n", ''))
            conn.execute('DROP TABLE capture_overrides')
            conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','4')")
            conn.execute("INSERT INTO sources(id,path,display_name,added_at) VALUES(1,?,'Photos',0)", (directory,))
            conn.execute("INSERT INTO files(source_id,relpath,filename,ext,capture_time,added_at) VALUES(1,'x.jpg','x.jpg','.jpg','2022-01-02T03:04:05',0)")
            conn.commit()
            conn.close()
            cat = catalog.Catalog(path)
            try:
                row = cat.connection.execute('SELECT capture_time,availability FROM files').fetchone()
                self.assertEqual(tuple(row), ('2022-01-02T03:04:05', 'local'))
                self.assertEqual(cat.stats()['schema'], catalog.SCHEMA_VERSION)
                with cat.write() as conn:
                    conn.execute("INSERT INTO capture_overrides VALUES(1,'2022-01-02T04:04:05-05:00',0)")
                recovered_path = Path(directory) / 'recovered.sqlite3'
                recovered = catalog.salvage(path, recovered_path)
                self.assertTrue(recovered['ok'])
                self.assertEqual(recovered['counts']['capture_overrides'], 1)
                salvage = catalog.Catalog(recovered_path)
                try:
                    self.assertEqual(salvage.connection.execute('SELECT capture_time FROM capture_overrides').fetchone()[0],
                                     '2022-01-02T04:04:05-05:00')
                finally:
                    salvage.close()
            finally:
                cat.close()


class CloudReadEntryTests(TestCase):
    def test_import_scan_reports_skipped_placeholder_and_no_copy_touches_existing_destination(self):
        import ingest_workflow as ingest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source' / 'photo.jpg'
            source.parent.mkdir()
            source.write_bytes(b'original bytes')
            destination = root / 'destination.jpg'
            destination.write_bytes(b'previous completed output')
            with mock.patch.object(media_availability, 'from_stat', return_value='cloud-only'), \
                 mock.patch.object(Path, 'open', side_effect=AssertionError('must not read content')):
                items = ingest.scan_source(source.parent)
                self.assertEqual(items[0]['availability'], 'cloud-only')
                plan = ingest.build_plan(items, {'destination': str(root / 'exports')})
                self.assertEqual(plan['total'], 0)
                self.assertEqual(plan['skipped'][0]['reason'], 'cloud-only')
                for run in (lambda: ingest.header_hash(source), lambda: ingest._file_hash(source),
                            lambda: ingest._capture_and_camera(source)):
                    with self.assertRaisesRegex(OSError, 'Download Now'):
                        run()
                result = ingest.copy_item({'source': str(source), 'destination': str(destination)})
                self.assertFalse(result['ok'])
                self.assertIn('Download Now', result['error'])
            self.assertEqual(destination.read_bytes(), b'previous completed output')
            self.assertEqual(source.read_bytes(), b'original bytes')

    def test_watched_cloud_arrival_waits_for_download_then_two_stable_polls(self):
        import watch_workflow
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / 'photos'
            photos.mkdir()
            Image.new('RGB', (12, 8), 'navy').save(photos / 'download.jpg')
            cat = catalog.Catalog(root / 'catalog.sqlite3')
            cat.add_source(photos)
            watch = {'id': 'cloud', 'name': 'Cloud', 'path': str(photos), 'enabled': True, 'mode': 'catalog'}
            service = watch_workflow.WatchService(cat, lambda: [watch])
            try:
                with mock.patch.object(media_availability, 'from_stat', return_value='cloud-only'), \
                     mock.patch.object(watch_workflow.ingest_workflow, 'header_hash', side_effect=AssertionError('must not fingerprint')):
                    service.poll_once()
                    service.poll_once()
                    self.assertEqual(cat.query({'limit': 10})['total'], 0)
                    self.assertIn('Download Now', service.status[0]['error'])
                service.poll_once()
                self.assertEqual(cat.query({'limit': 10})['total'], 0)
                service.poll_once()
                self.assertEqual(cat.query({'limit': 10})['total'], 1)
                self.assertEqual(service.status[0]['error'], '')
            finally:
                cat.close()

    def test_render_thumbnail_and_metadata_stop_before_decode(self):
        import server
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'photo.jpg'
            path.write_bytes(b'original bytes')
            with mock.patch.object(server, 'src_path', return_value=path), \
                 mock.patch.object(server, 'guard_photo'), \
                 mock.patch.object(server, '_EXIF_CACHE', {}), \
                 mock.patch.object(media_availability, 'availability', return_value='cloud-only'), \
                 mock.patch.object(server, '_render_preview', side_effect=AssertionError('must not render')), \
                 mock.patch.object(server, '_build_thumb', side_effect=AssertionError('must not decode')), \
                 mock.patch.object(server.platform_image, 'metadata', side_effect=AssertionError('must not extract')):
                for run in (lambda: server.render_preview('photo.jpg', {}, 100),
                            lambda: server.thumb_jpeg('photo.jpg'),
                            lambda: server.exif_for('photo.jpg')):
                    with self.assertRaises(server.APIError) as caught:
                        run()
                    self.assertEqual((caught.exception.status, caught.exception.code), (409, 'cloud-only'))
                    self.assertIn('Download Now', str(caught.exception))
