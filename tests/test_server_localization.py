# SPDX-License-Identifier: GPL-3.0-only
"""Server-owned messages localize without modifying data or error protocols."""
import ast
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import server_localization as localization


ROOT = Path(__file__).resolve().parents[1]


class ServerLocalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefs = self.root / 'preferences.json'
        self.messages = {
            'Choose a photo to edit': 'Choisissez une photo à modifier',
            'Photo {name} is unavailable': 'La photo {name} est indisponible',
            'job is not cancellable': 'Cette tâche ne peut pas être annulée',
            'the catalog is not available': 'Le catalogue est indisponible',
            'Export': 'Exporter',
            'could not decode {name}: {error}': 'Impossible de décoder {name} : {error}',
            'Select no more than 5000 photos for a keyword batch':
                'Sélectionnez au plus 5000 photos pour un lot de mots-clés',
            'External XMP changes conflict with pending edits ({properties}). Read the sidecar metadata before syncing again.':
                'Les modifications XMP externes sont en conflit ({properties}). Relisez les métadonnées avant de synchroniser.',
        }
        self.write_catalog()
        self.set_locale('fr')
        self.translator = localization.Translator(self.root, self.prefs)
        self.patch = mock.patch.object(localization, '_translator', self.translator)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def set_locale(self, locale):
        self.prefs.write_text(json.dumps({'locale': locale}), encoding='utf-8')

    def write_catalog(self):
        path = self.root / 'web/locales/fr.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'locale': 'fr',
                                    'messages': self.messages}, ensure_ascii=False), encoding='utf-8')

    def test_current_preferences_locale_is_used_without_restart(self):
        self.assertEqual(localization.T('Choose a photo to edit'), self.messages['Choose a photo to edit'])
        self.set_locale('en')
        self.assertEqual(localization.T('Choose a photo to edit'), 'Choose a photo to edit')
        self.set_locale('fr')
        self.assertEqual(localization.T('Choose a photo to edit'), self.messages['Choose a photo to edit'])

    def test_catalog_reads_are_cached_and_reloaded_after_file_change(self):
        with mock.patch.object(localization.json, 'loads', wraps=json.loads) as loads:
            for _ in range(5):
                self.translator('Choose a photo to edit')
            self.assertEqual(loads.call_count, 2, 'parse prefs/catalog once, then use their stat-keyed cache')
            self.messages['Choose a photo to edit'] = 'Sélectionnez une photo'
            self.write_catalog()
            self.assertEqual(self.translator('Choose a photo to edit'), 'Sélectionnez une photo')
            self.assertEqual(loads.call_count, 3)

    def test_filenames_and_metadata_are_never_translation_inputs(self):
        result = localization.T('Photo {name} is unavailable', name='Export')
        self.assertEqual(result, 'La photo Export est indisponible')
        data = {'name': 'Export', 'keywords': ['Export'], 'status': 'unavailable'}
        self.assertIs(localization.refresh(data), data)
        payload = json.loads(json.dumps({'error': result, **data}))
        self.assertEqual(payload['name'], 'Export')
        self.assertEqual(payload['keywords'], ['Export'])
        self.assertEqual(payload['status'], 'unavailable')

    def test_inserted_values_are_not_reinterpreted_as_template_code(self):
        name = '{other.__class__} {name}'
        result = localization.T('Photo {name} is unavailable', name=name)
        self.assertEqual(result, 'La photo {other.__class__} {name} est indisponible')

    def test_original_error_message_survives_copying_and_translation(self):
        error = RuntimeError(localization.T('job is not cancellable'))
        self.assertEqual(str(error), self.messages['job is not cancellable'])
        self.assertEqual(localization.source_message(error), 'job is not cancellable')
        self.assertEqual(localization.source_message(copy.deepcopy(error)), 'job is not cancellable')

    def test_marked_cached_capability_can_refresh_but_plain_data_cannot(self):
        message = localization.T('Photo {name} is unavailable', name='file.jpg')
        self.set_locale('en')
        self.assertEqual(localization.refresh(message), 'Photo file.jpg is unavailable')
        self.assertEqual(localization.refresh('Export'), 'Export')

    def test_missing_or_invalid_catalog_cannot_break_error_reporting(self):
        self.assertEqual(self.translator('Missing English message'), 'Missing English message')
        self.messages['Photo {name} is unavailable'] = 'Photo {different}'
        self.write_catalog()
        self.assertEqual(self.translator('Photo {name} is unavailable', name='file.jpg'),
                         'Photo file.jpg is unavailable')
        (self.root / 'web/locales/fr.json').write_text('{bad json')
        self.assertEqual(self.translator('Export'), 'Export')
        self.set_locale('../outside')
        self.assertEqual(self.translator.locale(), 'en')

    def test_portable_decoder_error_preserves_filename_and_native_diagnostic(self):
        import platform_image

        buffer = types.SimpleNamespace(spec=lambda: types.SimpleNamespace(width=0, height=0),
                                       geterror=lambda: 'Export {error}')
        decoder = types.SimpleNamespace(
            ImageSpec=lambda: types.SimpleNamespace(attribute=lambda *args: None),
            ImageBuf=lambda *args: buffer)
        with mock.patch.dict(sys.modules, {'OpenImageIO': decoder}):
            with self.assertRaises(RuntimeError) as caught:
                platform_image._open_portable_full_precision(Path('Export {name}.tif'))
        self.assertEqual(str(caught.exception),
                         'Impossible de décoder Export {name}.tif : Export {error}')
        self.assertEqual(localization.source_message(caught.exception),
                         'could not decode Export {name}.tif: Export {error}')

    def test_env_and_callable_preferences_paths_support_isolated_profiles(self):
        with mock.patch.dict(os.environ, {'LIGHTTABLE_PREFS_FILE': str(self.prefs)}):
            self.assertEqual(localization.Translator(self.root)('Export'), 'Exporter')
        current = [self.prefs]
        translator = localization.Translator(self.root, lambda: current[0])
        self.assertEqual(translator('Export'), 'Exporter')
        current[0] = self.root / 'other-profile.json'
        current[0].write_text('{"locale":"en"}')
        self.assertEqual(translator('Export'), 'Export')

    def server_function(self, name, namespace):
        tree = ast.parse((ROOT / 'server.py').read_text())
        node = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == name)
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, 'server.py', 'exec'), namespace)
        return namespace[name]

    def test_real_external_edit_endpoint_localizes_its_empty_selection_error(self):
        function = self.server_function('start_external_edit', {'T': localization.T})
        self.assertEqual(function({'names': []}), {
            'ok': False, 'error': self.messages['Choose a photo to edit']})

    def test_dam_keyword_endpoint_localizes_validation_without_touching_selection(self):
        function = self.server_function('keyword_batch_action', {
            'T': localization.T, 'require_catalog': lambda: object(),
        })
        request = {'action': 'add', 'names': 'Export', 'keywords': ['Export']}
        with self.assertRaisesRegex(ValueError, 'Sélectionnez au plus 5000'):
            function(request)
        self.assertEqual(request, {'action': 'add', 'names': 'Export', 'keywords': ['Export']})

    def test_background_job_messages_follow_preferences_and_keep_job_protocol(self):
        from jobs import JobRegistry

        jobs = JobRegistry()
        queued = jobs.create('export', result={'name': 'Export'})
        with ThreadPoolExecutor(max_workers=1) as pool:
            # Workers may already exist when the user changes their language.
            self.set_locale('en')
            self.assertEqual(pool.submit(localization.T, 'Export').result(), 'Export')
            self.set_locale('fr')
            error = pool.submit(localization.T, 'Photo {name} is unavailable', name='Export').result()
            completed = jobs.update(queued['id'], state='failed', errors=[error])
        self.assertEqual(completed['errors'], ['La photo Export est indisponible'])
        self.assertEqual(completed['kind'], 'export')
        self.assertEqual(completed['state'], 'failed')
        self.assertEqual(completed['result'], {'name': 'Export'})
        self.set_locale('en')
        # Completed records remain an honest record of what the worker reported.
        self.assertEqual(jobs.get(queued['id'])['errors'], ['La photo Export est indisponible'])

    def test_xmp_conflict_is_translated_and_preserves_identifiers_and_external_bytes(self):
        import xmp_sidecar

        source = self.root / 'Export.CR3'
        source.write_bytes(b'original')
        sidecar = source.with_suffix('.xmp')
        sidecar.write_text(xmp_sidecar.build_sidecar({'rating': 1}))
        before = xmp_sidecar.sidecar_snapshot(source)
        external = xmp_sidecar.build_sidecar({'rating': 2})
        sidecar.write_text(external)
        errors = []
        self.assertFalse(xmp_sidecar.write_sidecar(source, {'rating': 4}, errors=errors,
                                                 expected_snapshot=before))
        self.assertIn('Les modifications XMP externes sont en conflit (', errors[0])
        self.assertIn('xmp:Rating', errors[0])
        self.assertIn('lighttable:edit', errors[0])
        self.assertEqual(sidecar.read_text(), external)
        self.assertEqual(source.read_bytes(), b'original')

    def test_real_http_exception_classifier_keeps_busy_code_in_french(self):
        class APIError(RuntimeError):
            pass

        class ValidationError(ValueError):
            pass

        function = self.server_function('_handle_exception', {
            'APIError': APIError, 'ValidationError': ValidationError, 'json': json,
            'source_message': localization.source_message,
        })
        responses = []
        handler = types.SimpleNamespace(_json=lambda payload, status: responses.append((payload, status)))
        function(handler, RuntimeError(localization.T('job is not cancellable')))
        self.assertEqual(responses[0], ({'error': self.messages['job is not cancellable'], 'code': 'busy'}, 409))

    def test_extractor_accepts_only_complete_explicit_literal_markers(self):
        module = self.root / 'sample.py'
        module.write_text('from server_localization import T\nvalue = T("Photo {name}", name=filename)\n')
        extracted = localization.extract_source(self.root, ('sample.py',))
        self.assertEqual(extracted['messages'], ['Photo {name}'])
        for source in ['T(message)', 'T(f"Photo {name}")', 'T("Photo {name}")', 'T("Photo", name=value)']:
            with self.subTest(source=source):
                module.write_text(source)
                with self.assertRaises(ValueError):
                    localization.extract_source(self.root, ('sample.py',))

    def test_checked_in_manifest_tracks_real_message_markers(self):
        self.assertEqual(localization.extract_source(ROOT), json.loads((ROOT / localization.SOURCE_FILE).read_text()))


if __name__ == '__main__':
    unittest.main()
