"""Thumbnail failures remain actionable JSON and recover after file repair."""
import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import server
import media_availability
from tests.test_server_catalog import CatalogServerTestCase


class ThumbnailErrorsTests(CatalogServerTestCase):
    def request_thumbnail(self, name):
        handler = object.__new__(server.Handler)
        handler.path = '/api/thumb?name=' + name
        handler._enforce_security = mock.Mock()
        handler._log_request = mock.Mock()
        handler._send = mock.Mock()
        handler.do_GET()
        return handler._send.call_args.args

    def test_empty_original_stops_before_decode_and_returns_actionable_json(self):
        (self.root / 'a.jpg').write_bytes(b'')
        with mock.patch.object(server, '_build_thumb') as build:
            status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
        build.assert_not_called()
        payload = json.loads(data)
        self.assertEqual((status, kind, payload['code']), (409, 'application/json', 'empty-file'))
        self.assertIn('0 bytes', payload['error'])
        self.assertIn('restore', payload['error'])

    def test_dropbox_placeholder_returns_provider_recovery_message_before_decode(self):
        (self.root / 'a.jpg').write_bytes(b'')
        with mock.patch.object(media_availability.sys, 'platform', 'darwin'), \
             mock.patch.object(media_availability, '_macos_getxattr', return_value=lambda *args: 0), \
             mock.patch.object(media_availability, 'T', side_effect=lambda message: message), \
             mock.patch.object(server, '_build_thumb') as build:
            status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
        build.assert_not_called()
        payload = json.loads(data)
        self.assertEqual((status, kind, payload['code']), (409, 'application/json', 'cloud-only'))
        self.assertEqual(payload['error'],
                         'This photo is stored online in Dropbox. In Finder, choose “Make available offline,” then retry.')
        self.assertEqual(payload['details']['availability'], 'cloud-only')

    def test_file_provider_errors_name_the_service_without_reading_the_original(self):
        home = Path(self._dir.name)
        roots = (
            ('Library/CloudStorage/GoogleDrive-account', 'Google Drive', 'Drive for desktop'),
            ('Library/CloudStorage/OneDrive-Personal', 'OneDrive', 'Always keep on this device'),
            ('Library/CloudStorage/Dropbox-Team', 'Dropbox', 'Make available offline'),
            ('Library/CloudStorage/Box-Box', 'Box', 'containing folder'),
            ('Library/Mobile Documents/com~apple~CloudDocs', 'iCloud Drive', 'Download the original'),
            ('Library/CloudStorage/OtherProvider', None, 'cloud storage app'),
        )
        real_stat, real_open = Path.stat, Path.open
        for folder, provider, action in roots:
            original = home / folder / 'photo.RAF'

            def stat(path, *args, **kwargs):
                if path == original:
                    return SimpleNamespace(st_size=42_000_000, st_flags=media_availability.SF_DATALESS)
                return real_stat(path, *args, **kwargs)

            def open_file(path, *args, **kwargs):
                if path == original:
                    raise AssertionError('must not download a cloud original')
                return real_open(path, *args, **kwargs)

            with self.subTest(provider=provider), \
                 mock.patch.object(Path, 'home', return_value=home), \
                 mock.patch.object(Path, 'stat', stat), \
                 mock.patch.object(Path, 'open', open_file), \
                 mock.patch.object(media_availability.sys, 'platform', 'darwin'), \
                 mock.patch.object(server, 'src_path', return_value=original), \
                 mock.patch.object(server, '_build_thumb') as build:
                status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
                build.assert_not_called()
                payload = json.loads(data)
                self.assertEqual((status, kind, payload['code']), (409, 'application/json', 'cloud-only'))
                if provider:
                    self.assertIn(provider, payload['error'])
                self.assertIn(action, payload['error'])
                self.assertEqual(payload['details']['availability'], 'cloud-only')

    def test_windows_partial_download_uses_cloud_error_before_decode(self):
        original = self.root / 'a.jpg'
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == original:
                return SimpleNamespace(st_size=42_000_000, st_file_attributes=0x400000)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, 'stat', stat), \
             mock.patch.object(media_availability.sys, 'platform', 'win32'), \
             mock.patch.object(server, 'src_path', return_value=original), \
             mock.patch.object(server, '_build_thumb') as build:
            status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
        build.assert_not_called()
        payload = json.loads(data)
        self.assertEqual((status, kind, payload['code']), (409, 'application/json', 'cloud-only'))
        self.assertIn('not fully downloaded', payload['error'])
        self.assertNotIn('Finder', payload['error'])

    def test_decoder_failure_keeps_diagnostic_and_retry_after_repair_returns_jpeg(self):
        (self.root / 'a.jpg').write_bytes(b'not an image')
        with mock.patch.object(server, 'CACHE', Path(self._dir.name) / 'cache'), \
             mock.patch.object(server, '_build_thumb', side_effect=RuntimeError('decoder detail <bad header>')):
            status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
        payload = json.loads(data)
        self.assertEqual(status, 422)
        self.assertEqual(payload['code'], 'thumbnail-failed')
        self.assertIn('opens correctly', payload['error'])
        self.assertEqual(payload['details']['diagnostic'], 'decoder detail <bad header>')

        output = io.BytesIO()
        Image.new('RGB', (20, 10), 'teal').save(output, 'JPEG')
        (self.root / 'a.jpg').write_bytes(output.getvalue())
        (Path(self._dir.name) / 'cache' / 'thumb').mkdir(parents=True, exist_ok=True)
        with mock.patch.object(server, 'CACHE', Path(self._dir.name) / 'cache'), \
             mock.patch.object(server, 'prune_cache_throttled'):
            response = self.request_thumbnail(self.qualified('a.jpg'))
        self.assertEqual(response[0], 200, response[1])
        status, data, kind, cache = response
        self.assertEqual((status, kind), (200, 'image/jpeg'))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (20, 10))

    def test_existing_cloud_missing_and_permission_errors_remain_distinct(self):
        failures = [server.APIError(409, 'Download Now', 'cloud-only'),
                    FileNotFoundError('Missing original'), PermissionError('Permission denied')]
        for error, expected in zip(failures, [(409, 'cloud-only'), (404, 'not-found'), (403, 'forbidden')]):
            with self.subTest(expected=expected), \
                 mock.patch.object(server, 'thumb_jpeg', side_effect=error):
                status, data, kind = self.request_thumbnail(self.qualified('a.jpg'))
            self.assertEqual((status, json.loads(data)['code']), expected)
            self.assertEqual(json.loads(data)['error'], str(error))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_browser_error_lifecycle(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('thumbnail-errors.test.mjs'))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
