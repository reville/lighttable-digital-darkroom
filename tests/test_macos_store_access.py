# SPDX-License-Identifier: GPL-3.0-only
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('store_access_test', ROOT / 'macos_store_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)

class StoreAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)
        access._active.clear()
        self.addCleanup(access._active.clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('LIGHTTABLE_STORE_GRANTS_FILE', None)

    def grants(self, values):
        path = self.tmp_path / 'grants.json'
        path.write_text(json.dumps(values))
        os.environ['LIGHTTABLE_STORE_GRANTS_FILE'] = str(path)
        return path

    def test_direct_channel_never_loads_corefoundation(self):
        with patch.object(access, '_resolve', side_effect=AssertionError('direct channel acquired grant')):
            access.refresh()

    def test_new_grants_after_child_launch_are_acquired_once(self):
        calls = []
        path = self.grants({'/one': base64.b64encode(b'one').decode()})
        with patch.object(access, '_resolve', side_effect=lambda data: calls.append(data) or len(calls)):
            access.refresh(); access.refresh()
            path.write_text(json.dumps({'/one': base64.b64encode(b'one').decode(), '/two': base64.b64encode(b'two').decode()}))
            access.refresh()
        self.assertEqual(calls, [b'one', b'two'])

    def test_resolution_denial_is_not_recorded(self):
        self.grants({'/one': base64.b64encode(b'one').decode()})
        with patch.object(access, '_resolve', side_effect=PermissionError('reselect')):
            with self.assertRaises(PermissionError): access.refresh()
        self.assertFalse(access._active)

    def test_invalid_bookmark_fails_closed(self):
        self.grants({'/one': 'invalid!'})
        with patch.object(access, '_resolve', side_effect=AssertionError('invalid bookmark resolved')):
            with self.assertRaises(ValueError): access.refresh()

    def test_scope_is_held_through_worker_lifetime_and_balanced(self):
        self.grants({'/one': base64.b64encode(b'one').decode()})
        calls = []
        class CF:
            def CFURLStopAccessingSecurityScopedResource(self, url): calls.append(('stop', url))
            def CFRelease(self, url): calls.append(('release', url))
        with patch.object(access, '_resolve', return_value=42), patch.object(access, '_cf', CF()):
            access.refresh()
            self.assertEqual(calls, [])
            access.close(); access.close()
        self.assertEqual(calls, [('release', 42)])

    def bootstrap(self, store):
        (self.tmp_path / 'sitecustomize.py').write_bytes((ROOT / 'sitecustomize.py').read_bytes())
        env = dict(os.environ, PYTHONPATH=str(self.tmp_path))
        if store:
            env['LIGHTTABLE_STORE_GRANTS_FILE'] = str(self.tmp_path / 'grants.json')
        return subprocess.run([sys.executable, '-c', 'print("application-started")'], cwd=self.tmp_path,
                              env=env, capture_output=True, text=True)

    def test_missing_grant_bridge_stops_worker_before_application_code(self):
        result = self.bootstrap(True)
        self.assertEqual(result.returncode, 78)
        self.assertNotIn('application-started', result.stdout)
        self.assertIn('ModuleNotFoundError', result.stderr)

    def test_direct_worker_bootstrap_without_grant_environment_is_unchanged(self):
        result = self.bootstrap(False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), 'application-started')
