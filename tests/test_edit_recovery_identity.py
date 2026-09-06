"""Use real catalog rows, live source signatures, history and the state route."""
from unittest import mock
import os
import server
from tests.test_server_catalog import CatalogServerTestCase

class RecoveryIdentityTests(CatalogServerTestCase):
    def test_catalog_and_folder_drafts_use_the_same_live_source_revision(self):
        rows, _ = server.library_payload()
        row = next(row for row in rows if row['name'] == self.qualified('a.jpg'))
        recovered = server.recovery_state_for(row['name'])
        self.assertEqual(row['recoverySourceKey'], recovered['_recoverySourceKey'])
        self.assertNotEqual(row['fileKey'], row['recoverySourceKey'])
        with mock.patch.object(server, 'catalog_handle', return_value=None):
            rows, _ = server.library_payload()
            row = next(row for row in rows if row['name'] == 'a.jpg')
            self.assertEqual(row['recoverySourceKey'], server.recovery_state_for('a.jpg')['_recoverySourceKey'])

    def test_source_replacement_after_prompt_is_rejected_before_state_write(self):
        name = self.qualified('a.jpg')
        original = server.recovery_state_for(name)['_recoverySourceKey']
        path = self.root / 'a.jpg'; before = path.stat().st_mtime_ns
        os.utime(path, ns=(before + 12345678, before + 12345678))
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/state'; handler.headers = {}
        handler._body = lambda: {'name': name, 'grade': {'exposure': 1}, 'expectedRecoverySourceKey': original}
        handler._enforce_security = lambda **_: None
        results = []
        handler._json = lambda value, status=200: results.append((status, value))
        handler._log_request = lambda _: None
        handler.do_POST()
        self.assertIn('original changed', results[0][1]['error'])
        self.assertIsNone(server.catalog_entry_for(name)['grade'])

    def test_recovery_exposes_separate_history_acknowledgement(self):
        name = self.qualified('a.jpg'); image_id = server.catalog_image_id(name)
        state = {'grade': {'exposure': .5}}
        server.save_image_state(name, state)
        self.assertIsNone(server.recovery_state_for(name)['_recoveryHistory'])
        self.catalog.add_history(image_id, 'Exposure', state)
        self.assertEqual(server.recovery_state_for(name)['_recoveryHistory'], state)
