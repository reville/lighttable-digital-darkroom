"""Real file delivery and cancellation across queue, rendering and publication."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

import export_workflow as ew
from jobs import JobRegistry
import server


class ExportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.photos = {}
        self.roots = {}
        self.registry = JobRegistry()
        self.pool = self.resources.enter_context(ThreadPoolExecutor(max_workers=2))
        for target, value in [('FOLDER', self.root), ('EXPORT_POOL', self.pool),
                              ('JOBS', self.registry), ('RUST_WORKER_BIN', 'test-renderer'),
                              ('EXPORT', {'running': False})]:
            self.resources.enter_context(mock.patch.object(server, target, value))
        self.resources.enter_context(mock.patch.object(server, 'catalog_handle', return_value=None))
        self.resources.enter_context(mock.patch.object(server, 'guard_photo'))
        self.resources.enter_context(mock.patch.object(server, 'src_path', side_effect=lambda n: self.photos[n]))
        self.resources.enter_context(mock.patch.object(server, 'resolve_name', side_effect=self.resolve))
        self.resources.enter_context(mock.patch.object(server, 'source_root', side_effect=lambda i: self.roots[i]))
        self.resources.enter_context(mock.patch.object(server, 'exif_for', return_value={'DateTimeOriginal': '2026:01:02 03:04:05', 'OffsetTimeOriginal': '+02:30'}))
        self.resources.enter_context(mock.patch.object(server, 'export_metadata_fields', return_value={}))
        self.resources.enter_context(mock.patch.object(server.edits, 'lens_profile_for', return_value=None))
        self.resources.enter_context(mock.patch.object(server, 'effective_new_photo_defaults', return_value=({}, {})))
        self.resources.enter_context(mock.patch.object(server, 'export_candidates', side_effect=self.candidates))
        self.resources.enter_context(mock.patch.object(server, 'export_with_resident_engine', side_effect=self.render))

    def resolve(self, name):
        sid = int(name.split(':', 1)[0])
        return self.photos[name], sid, str(self.photos[name].relative_to(self.roots[sid])), server.catalog_module.parse_name(name)[2]

    def add_photo(self, sid, index):
        root = self.root / f'volume-{sid}' / 'Photos'
        self.roots[sid] = root
        relative = f'day-{index % 4}/IMG_{index:04}.png'
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Every source has distinct pixels, including across source roots.
        color = (index % 256, (index // 256) + sid * 40, 117)
        Image.new('RGB', (24 + index % 3, 16), color).save(path)
        name = f'{sid}:{relative}'
        self.photos[name] = path
        return name

    def candidates(self):
        return [(n, {'status': 'approved', 'rating': 1, 'params': {'profile_enabled': True},
                     'grade': {}, 'crop': None, 'masks': [], 'heals': [], 'optics': {}},
                 'Virtual look' if server.library_workflow.is_virtual(n) else p.stem) for n, p in self.photos.items()]

    def render(self, name, destination, job):
        # Exercise actual float loading/encoding and the production finisher;
        # substitute only the expensive film-engine stage, not publication.
        server.finish_export(self.photos[name], destination, job)
        return {'backend': 'fixture', 'total_ms': 0}

    def wait(self, ident):
        for _ in range(500):
            record = self.registry.get(ident)
            if record['state'] in ('done', 'failed', 'cancelled'):
                return record
            time.sleep(.02)
        self.fail('Export did not settle within ten seconds')

    def test_hundreds_from_mixed_sources_keep_pixels_hierarchy_and_capture_time(self):
        for index in range(240):
            self.add_photo(1 + index % 2, index)
        original_bytes = {n: p.read_bytes() for n, p in self.photos.items()}
        opts = {'which': 'all', 'format': 'png', 'destination': str(self.root / 'delivery'),
                'destinationMode': 'preserve-source-hierarchy', 'filenameTemplate': '{filename}',
                'metadata': 'none', 'sidecar': False, 'preserveCaptureTime': True}
        preview = server.preview_export(opts)
        self.assertEqual(preview['destinationCount'], 4)
        self.assertEqual(len(preview['samples']), 3)
        self.assertFalse((self.root / 'delivery').exists())
        result = self.wait(server.start_export(opts)['jobId'])
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(result['result']['completed'], 240)
        outputs = list((self.root / 'delivery').rglob('*.png'))
        self.assertEqual(len(outputs), 240)
        def pixels(path):
            with Image.open(path) as image:
                return image.size, image.getpixel((0, 0))
        output_pixels = sorted(pixels(p) for p in outputs)
        original_pixels = sorted(pixels(p) for p in self.photos.values())
        self.assertEqual(output_pixels, original_pixels)
        wanted = datetime(2026, 1, 2, 0, 34, 5, tzinfo=timezone.utc).timestamp()
        self.assertTrue(all(abs(p.stat().st_mtime - wanted) < 1 for p in outputs))
        self.assertEqual(original_bytes, {n: p.read_bytes() for n, p in self.photos.items()})

    def test_cancel_preserves_completed_and_previous_outputs_and_blocks_restart_until_cleanup(self):
        names = [self.add_photo(1, i) for i in range(40)]
        destination = self.root / 'delivery'
        destination.mkdir()
        previous = destination / 'IMG_0001.png'
        previous.write_bytes(b'previous complete delivery')
        prepared = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        render = self.render
        def gated(name, path, job):
            value = render(name, path, job)
            if name != names[0]:
                prepared.set()
                if not release.wait(5): raise TimeoutError('test gate')
            return value
        opts = {'format': 'png', 'destination': str(destination), 'filenameTemplate': '{filename}',
                'collision': 'overwrite', 'metadata': 'none', 'sidecar': True}
        with mock.patch.object(server, 'export_with_resident_engine', side_effect=gated):
            ident = server.start_export(opts)['jobId']
            self.assertTrue(prepared.wait(5))
            for _ in range(200):
                if (destination / 'IMG_0000.png').exists(): break
                time.sleep(.01)
            cancelled = self.registry.cancel(ident)
            self.assertEqual(cancelled['state'], 'running')
            self.assertTrue(cancelled['cancelRequested'])
            self.assertIn('already running', server.start_export(opts)['error'])
            release.set()
            result = self.wait(ident)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(previous.read_bytes(), b'previous complete delivery')
        self.assertEqual(result['result']['completed'], 1)
        self.assertEqual(result['result']['revealPath'], str((destination / 'IMG_0000.png').resolve()))
        self.assertEqual(result['result']['cancelledCount'], 39)
        self.assertEqual(len(list(destination.glob('*.png'))), 2)
        self.assertEqual(len(list(destination.glob('*.lighttable.json'))), 1)
        self.assertFalse(list(destination.glob('.*')))
        next_result = self.wait(server.start_export(dict(opts, names=[names[2]], collision='rename'))['jobId'])
        self.assertEqual(next_result['state'], 'done')
        self.assertEqual(self.registry.update(ident, state='done', progress=999), result)
        self.assertEqual(server.EXPORT['jobId'], next_result['id'])

    def test_colliding_names_virtual_copies_skip_and_warnings(self):
        first = self.add_photo(1, 0)
        self.add_photo(2, 0)
        self.photos[server.library_workflow.virtual_name(first, 'look')] = self.photos[first]
        destination = self.root / 'delivery'
        opts = {'format': 'png', 'destination': str(destination), 'filenameTemplate': 'same',
                'metadata': 'none', 'sidecar': True}
        def warning_render(name, path, job):
            result = self.render(name, path, job)
            job['warnings'].append('Test metadata capability warning')
            return result
        with mock.patch.object(server, 'export_with_resident_engine', side_effect=warning_render):
            record = self.wait(server.start_export(opts)['jobId'])
        self.assertEqual(record['state'], 'done')
        self.assertEqual(len(list(destination.glob('*.png'))), 3)
        self.assertEqual(len(record['result']['warnings']), 3)
        before = {p.name: p.read_bytes() for p in destination.iterdir()}
        record = self.wait(server.start_export(dict(opts, collision='skip'))['jobId'])
        self.assertEqual(record['result']['skipped'], 3)
        self.assertNotIn('revealPath', record['result'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in destination.iterdir()})

    def test_reveal_path_identifies_published_output_after_rename_in_each_destination_mode(self):
        name = self.add_photo(1, 0)
        for mode in ('fixed', 'original-folder-relative', 'preserve-source-hierarchy'):
            with self.subTest(mode=mode):
                opts = {'names': [name], 'format': 'png', 'destination': 'finished',
                        'destinationMode': mode, 'filenameTemplate': 'same',
                        'metadata': 'none', 'sidecar': False, 'collision': 'rename'}
                first = self.wait(server.start_export(opts)['jobId'])
                second = self.wait(server.start_export(opts)['jobId'])
                original = Path(first['result']['revealPath'])
                renamed = Path(second['result']['revealPath'])
                self.assertEqual(second['result']['completed'], 1)
                self.assertTrue(renamed.is_file())
                self.assertNotEqual(original, renamed)
                self.assertEqual(original.parent, renamed.parent)
                if mode == 'original-folder-relative':
                    self.assertEqual(renamed.parent, (self.photos[name].parent / 'finished').resolve())

    def test_single_and_batch_use_same_original_relative_paths_and_virtual_name(self):
        first = self.add_photo(1, 1)
        self.add_photo(2, 2)
        copy = server.library_workflow.virtual_name(first, 'look')
        self.photos[copy] = self.photos[first]
        opts = {'format': 'png', 'destination': 'finished',
                'destinationMode': 'original-folder-relative', 'filenameTemplate': '{filename}',
                'metadata': 'none', 'sidecar': False}
        all_items, _ = server.prepare_export(opts)
        one, _ = server.prepare_export(dict(opts, names=[copy]))
        self.assertEqual(one[0][1]['destination'], dict(all_items)[copy]['destination'])
        target = server.export_requested_path(copy, one[0][1], {})
        self.assertEqual(target, self.photos[first].parent.resolve() / 'finished' / 'Virtual look.png')
        result = self.wait(server.start_export(dict(opts, names=[copy]))['jobId'])
        self.assertEqual(result['result']['completed'], 1)
        self.assertTrue(target.is_file())

    def test_implicit_pair_scope_collapses_only_when_both_members_match(self):
        original = self.add_photo(1, 0)
        image = self.photos.pop(original)
        raw = image.with_suffix('.dng'); raw.write_bytes(image.read_bytes())
        jpeg = image.with_suffix('.jpg')
        with Image.open(image) as pixels:
            pixels.save(jpeg)
        raw_name = original.rsplit('.', 1)[0] + '.dng'
        jpeg_name = original.rsplit('.', 1)[0] + '.jpg'
        self.photos.update({raw_name: raw, jpeg_name: jpeg})
        self.assertEqual([name for name, _ in server.prepare_export({'pairView': 'jpeg'})[0]], [jpeg_name])
        self.assertEqual([name for name, _ in server.prepare_export({'pairView': 'raw', 'names': [jpeg_name]})[0]], [jpeg_name])
        candidates = self.candidates()
        candidates[0][1]['status'] = 'skipped'
        with mock.patch.object(server, 'export_candidates', return_value=candidates):
            self.assertEqual([name for name, _ in server.prepare_export({'pairView': 'raw', 'which': 'all'})[0]], [jpeg_name])


class DestinationTimeTests(unittest.TestCase):
    def test_relative_folder_cannot_escape_via_dotdot_absolute_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / 'source'; root.mkdir()
            source = root / 'a.jpg'; source.touch()
            outside = base / 'outside'; outside.mkdir()
            (root / 'redirect').symlink_to(outside, target_is_directory=True)
            for value in ('.', '../outside', str(outside), 'redirect', 'C:\\outside'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ew.photo_destination({'destinationMode': 'original-folder-relative', 'destination': value},
                                         library_root=root, source=source, source_root=root)
            self.assertEqual(ew.photo_destination({'destinationMode': 'original-folder-relative', 'destination': 'exports'},
                                                  library_root=root, source=source, source_root=root), (root / 'exports').resolve())

    def test_missing_offset_never_silently_assumes_utc_and_dst_is_capture_date_specific(self):
        data = {'DateTimeOriginal': '2026:01:02 03:04:05'}
        value, warning = ew.capture_timestamp(data)
        self.assertIsNone(value); self.assertIn('timezone', warning)
        if not hasattr(time, 'tzset'): return
        original = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'; time.tzset()
            winter, _ = ew.capture_timestamp(data, 'local')
            summer, _ = ew.capture_timestamp({'DateTimeOriginal': '2026:07:02 03:04:05'}, 'local')
            self.assertEqual(datetime.fromtimestamp(winter, timezone.utc).hour, 8)
            self.assertEqual(datetime.fromtimestamp(summer, timezone.utc).hour, 7)
        finally:
            if original is None: os.environ.pop('TZ', None)
            else: os.environ['TZ'] = original
            time.tzset()
        self.assertIsNone(ew.capture_timestamp({'DateTimeOriginal': '0000:00:00 00:00:00'})[0])

    def test_cancel_terminates_export_subprocess_and_waits_for_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'started'
            batch = server.ExportBatch([], directory)
            command = [sys.executable, '-c', 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(20)', str(marker)]
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(server._run_export_process, command, dict(os.environ), batch)
                for _ in range(200):
                    if marker.exists(): break
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                batch.cancelled.set()
                with self.assertRaises(server.ExportCancelled):
                    future.result(timeout=3)


if __name__ == '__main__':
    unittest.main()
