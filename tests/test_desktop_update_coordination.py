# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import http.client
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import catalog
from jobs import JobRegistry
import server
from server_updates import UpdateCoordinator


class DesktopUpdateCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = catalog.Catalog(self.root / 'catalog.sqlite3')
        self.source = self.catalog.add_source(self.root / 'photos')
        self.jobs = JobRegistry()
        self.coordinator = UpdateCoordinator(server)
        self.patches = [
            patch.object(server, 'UPDATES', self.coordinator),
            patch.object(server, 'open_catalog', return_value=self.catalog),
            patch.object(server, 'configured_backup_directory', return_value=self.root / 'Backups'),
            patch.object(server, 'JOBS', self.jobs),
            patch.object(server, 'SCANNER', None),
        ]
        for item in self.patches:
            item.start()
        self.completed_requests = queue.Queue()
        completed_requests = self.completed_requests
        class Handler(server.Handler):
            def do_POST(self):
                try:
                    super().do_POST()
                finally:
                    completed_requests.put(None)
        self.http = server.LightTableServer(('127.0.0.1', 0), Handler)
        self.http_patch = patch.object(server, 'HTTPD', self.http)
        self.http_patch.start()
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.thread.join(3)
        self.http.server_close()
        self.http_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.catalog.close()
        self.temporary.cleanup()

    def post(self, action, *, authorized=True):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['X-LightTable-Token'] = server.INSTANCE_TOKEN
        connection.request('POST', '/api/updates/' + action, '{}', headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        # Reading response bytes can precede the handler's finally block.
        # Account for the complete request before inspecting admission state.
        self.completed_requests.get(timeout=5)
        return result

    def test_unauthenticated_updates_cannot_freeze_or_stop_server(self):
        for action in ('prepare', 'shutdown', 'cancel', 'apply'):
            with self.subTest(action=action):
                self.assertEqual(self.post(action, authorized=False)[0], 401)
        self.assertFalse(self.coordinator.blocked)
        self.assertTrue(self.thread.is_alive())

    def test_pending_export_prevents_shutdown_without_backup_or_cancellation(self):
        job = self.jobs.create('export', state='running')
        code, body = self.post('prepare')
        self.assertEqual(code, 409)
        self.assertEqual(body['code'], 'update-busy')
        self.assertFalse(self.coordinator.blocked)
        self.assertFalse((self.root / 'Backups').exists())
        self.assertEqual(self.jobs.get(job['id'])['state'], 'running')
        self.assertTrue(self.thread.is_alive())

    def test_backup_covers_current_catalog_and_admission_reopens_on_cancel(self):
        code, body = self.post('prepare')
        self.assertEqual(code, 200, body)
        self.assertTrue(body['ok'])
        self.assertTrue(Path(body['backup']).is_file())
        self.assertTrue(catalog.verify_backup(Path(body['backup'])))
        with self.assertRaises(server.APIError):
            self.coordinator.enter('/api/state')
        self.assertEqual(self.post('cancel')[0], 200)
        self.coordinator.enter('/api/state')
        self.coordinator.leave()
        self.assertEqual(self.coordinator.active_requests, 0)
        self.assertEqual(self.catalog.sources()[0]['id'], self.source)

    def test_backup_failure_leaves_original_catalog_open_and_editing_enabled(self):
        with patch.object(self.catalog, 'backup', side_effect=OSError('disk full')):
            self.assertGreaterEqual(self.post('prepare')[0], 400)
        self.assertFalse(self.coordinator.blocked)
        self.assertEqual(self.catalog.sources()[0]['id'], self.source)
        self.assertTrue(self.thread.is_alive())

    def test_inflight_save_or_watched_ingest_prevents_backup(self):
        self.coordinator.enter('/api/state')
        try:
            self.assertEqual(self.post('prepare')[0], 409)
        finally:
            self.coordinator.leave()
        with self.coordinator.background_work() as allowed:
            self.assertTrue(allowed)
            self.assertEqual(self.post('prepare')[0], 409)
        self.assertEqual(self.post('prepare')[0], 200)
        with self.coordinator.background_work() as allowed:
            self.assertFalse(allowed)

    def test_shutdown_requires_preparation_and_exits_serve_loop_cleanly(self):
        self.assertEqual(self.post('shutdown')[0], 409)
        self.assertEqual(self.post('prepare')[0], 200)
        self.assertEqual(self.post('shutdown')[0], 200)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.coordinator.active_requests, 0)

    def test_cancel_reaps_worker_before_recovering_staged_update_and_editing(self):
        events = []
        self.coordinator.phase = 'ready'
        self.coordinator.helper_directory = self.root / 'pending'
        self.coordinator.helper_directory.mkdir()
        self.coordinator.apply_process = SimpleNamespace(
            poll=lambda: None, terminate=lambda: events.append('terminate'),
            wait=lambda timeout: events.append('reaped'))
        def recover():
            self.assertTrue(self.coordinator.blocked)
            self.assertTrue((self.root / 'pending/cancelled').is_file())
            events.append('ready')
        self.coordinator.worker = SimpleNamespace(cancel_apply=recover)
        self.assertEqual(self.post('cancel')[0], 200)
        self.assertEqual(events, ['terminate', 'reaped', 'ready'])
        self.assertFalse(self.coordinator.blocked)
        self.assertIsNone(self.coordinator.apply_process)

    def test_automatic_checks_honor_preference_and_persist_daily_limit(self):
        checked = Mock()
        worker = SimpleNamespace(status=lambda: {'supported': True, 'state': 'idle'}, check=checked)
        with patch.object(self.coordinator, '_linux', return_value=worker), \
             patch.object(server, 'CACHE', self.root), \
             patch.object(server, 'load_preferences', return_value={'automaticUpdateChecks': False}) as prefs:
            self.coordinator.start('check', automatic=True)
            checked.assert_not_called()
            prefs.return_value = {'automaticUpdateChecks': True}
            # Corrupt cache data should not disable checks indefinitely.
            stamp = self.root / 'automatic-update-check.json'
            stamp.write_text('{"at": "invalid"}')
            self.coordinator.start('check', automatic=True)
            deadline = time.monotonic() + 2
            while self.coordinator.operation and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNone(self.coordinator.operation)
            checked.assert_called_once()
            self.coordinator.start('check', automatic=True)
            checked.assert_called_once()
            # The throttle survives a fresh coordinator (app restart).
            fresh = UpdateCoordinator(server)
            with patch.object(fresh, '_linux', return_value=worker):
                fresh.start('check', automatic=True)
            checked.assert_called_once()


if __name__ == '__main__':
    unittest.main()
