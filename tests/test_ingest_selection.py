"""Reviewed ingest selections and cooperative cancellation preserve source files."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import ingest_workflow
from jobs import JobRegistry
import server


class IngestSelectionTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / 'card'
        self.source.mkdir()
        self.destination = self.root / 'library'
        self.items = []
        for index in range(3):
            photo = self.source / f'{index}.jpg'
            photo.write_bytes(f'disposable source photo {index}'.encode())
            self.items.append({'source': str(photo), 'destination': str(self.destination / photo.name)})
        self.registry = JobRegistry()
        for name, value in [('JOBS', self.registry), ('INGEST', {'running': False}),
                            ('catalog_handle', lambda: None)]:
            self.resources.enter_context(mock.patch.object(server, name, value))
        self.request = {'destination': str(self.destination), 'verify': 'hash'}

    def wait(self, ident):
        for _ in range(500):
            record = self.registry.get(ident)
            if record['state'] in ('done', 'failed', 'cancelled'):
                return record
            time.sleep(.01)
        self.fail('Import did not finish within five seconds')

    def test_explicit_empty_or_malformed_plan_never_scans_or_starts_a_job(self):
        with mock.patch.object(server, 'scan_ingest_source') as scan:
            for plan in ({'items': []}, {}, None, {'items': 'all'}, {'items': [None]}):
                with self.subTest(plan=plan), self.assertRaises(ValueError):
                    server.start_ingest({'plan': plan, 'path': str(self.source), 'request': self.request})
            scan.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(self.destination.exists())

    def test_explicit_subset_copies_only_selected_files_and_preserves_originals(self):
        originals = {path.name: path.read_bytes() for path in self.source.iterdir()}
        with mock.patch.object(server, 'scan_ingest_source') as scan:
            started = server.start_ingest({'plan': {'items': [self.items[2]]}, 'request': self.request})
            result = self.wait(started['jobId'])
            scan.assert_not_called()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['result']['copied'], 1)
        self.assertEqual([path.name for path in self.destination.iterdir()], ['2.jpg'])
        self.assertEqual(originals, {path.name: path.read_bytes() for path in self.source.iterdir()})

    def test_omitting_the_plan_preserves_scan_and_import_for_legacy_clients(self):
        with mock.patch.object(server, 'scan_ingest_source', return_value={'plan': {'items': self.items}}) as scan:
            started = server.start_ingest({'path': str(self.source), 'request': self.request})
            self.assertEqual(self.wait(started['jobId'])['result']['copied'], 3)
            scan.assert_called_once()

    def test_cancel_finishes_current_verified_copy_then_stops_and_keeps_both_sources_and_copies(self):
        reached = threading.Event()
        release = threading.Event()
        copy = ingest_workflow.copy_item
        def gated(item, **kwargs):
            if item == self.items[1]:
                reached.set()
                if not release.wait(5):
                    raise TimeoutError('test copy gate')
            return copy(item, **kwargs)
        with mock.patch.object(ingest_workflow, 'copy_item', side_effect=gated):
            started = server.start_ingest({'plan': {'items': self.items}, 'request': self.request})
            ident = started['jobId']
            try:
                self.assertTrue(reached.wait(5))
                cancelled = self.registry.cancel(ident)
                self.assertEqual(cancelled['state'], 'running')
                self.assertTrue(cancelled['cancelRequested'])
                self.assertIn('already running', server.start_ingest({'plan': {'items': self.items}, 'request': self.request})['error'])
            finally:
                release.set()
            result = self.wait(ident)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['result']['copied'], 2)
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()), ['0.jpg', '1.jpg'])
        for item in self.items[:2]:
            self.assertEqual(Path(item['source']).read_bytes(), Path(item['destination']).read_bytes())
        self.assertEqual(len(list(self.source.iterdir())), 3)


if __name__ == '__main__':
    unittest.main()
