# SPDX-License-Identifier: GPL-3.0-only
"""Catalog checks are strict by default and may tolerate lagging locales between releases."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('localization_catalogs', ROOT / 'scripts/localization.py')
localization = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localization)
HELP_CONTENT = localization.load_extractor('help_content.py')


class CatalogCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.messages = ['Export {0} photos', 'Open a photo']
        self.digest = localization.source_digest(self.messages)
        self.plurals_digest = localization.plural_digest([])
        self.write('docs/localization/source.json', {
            'version': 1, 'sourceDigest': self.digest, 'pluralsDigest': self.plurals_digest,
            'pluralPairs': [], 'messages': self.messages})
        self.write('web/locales/manifest.json', {'version': 1, 'locales': [{'code': 'en'}, {'code': 'fr'}]})
        self.write('web/locales/en.json', self.catalog('en', {m: m for m in self.messages}))
        self.write('web/locales/fr.json', self.catalog('fr', {m: 'Traduction : ' + m for m in self.messages}))
        patches = [mock.patch.object(localization, 'ROOT', self.root),
                   mock.patch.object(localization, 'plural_pairs', lambda: []),
                   mock.patch.object(localization, 'plural_categories', lambda code: ['one', 'other']),
                   mock.patch.object(localization, 'load_extractor', lambda name: HELP_CONTENT)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def catalog(self, code, messages, digest=None):
        return {'version': 1, 'locale': code, 'sourceDigest': digest or self.digest,
                'pluralsDigest': self.plurals_digest, 'plurals': {}, 'messages': messages}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def check(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            pending = localization.check(self.messages, **kwargs)
        return pending, out.getvalue()

    def test_complete_catalogs_pass_in_both_modes(self):
        self.assertEqual(self.check()[0], {})
        pending, output = self.check(allow_pending=True)
        self.assertEqual(pending, {})
        self.assertIn('Localization current: 2 locales, 2 messages.', output)

    def test_a_lagging_locale_fails_strict_mode_and_is_reported_in_pending_mode(self):
        self.write('web/locales/fr.json', self.catalog('fr', {'Open a photo': 'Ouvrir une photo'}, digest='old'))
        with self.assertRaisesRegex(ValueError, 'fr: translation source version is stale'):
            self.check()
        pending, output = self.check(allow_pending=True)
        self.assertEqual(pending, {'fr': ['Export {0} photos']})
        self.assertIn('Localization pending: 1 of 2 locales lag the source: fr (1 missing)', output)
        self.assertIn('translate-locales.py', output)

    def test_pending_mode_still_validates_the_translations_that_exist(self):
        self.write('web/locales/fr.json', self.catalog('fr', {'Export {0} photos': 'Exporter des photos'}, digest='old'))
        with self.assertRaisesRegex(ValueError, 'fr: interpolation or filename tokens changed'):
            self.check(allow_pending=True)
        self.write('web/locales/fr.json', self.catalog('fr', {'Export {0} photos': '', 'Open a photo': 'Ouvrir'}))
        with self.assertRaisesRegex(ValueError, 'fr: empty translation'):
            self.check(allow_pending=True)

    def test_a_stale_source_manifest_fails_even_in_pending_mode(self):
        with self.assertRaisesRegex(ValueError, 'Source manifest is stale'):
            with contextlib.redirect_stdout(io.StringIO()):
                localization.check(self.messages + ['New string'], allow_pending=True)

    def test_command_line_modes(self):
        self.write('web/locales/fr.json', self.catalog('fr', {'Open a photo': 'Ouvrir une photo'}, digest='old'))
        with mock.patch.object(localization, 'collect', lambda annotate=False: self.messages):
            with mock.patch('sys.argv', ['localization.py', 'check']), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(localization.main(), 1)
            self.assertIn('stale', err.getvalue())
            with mock.patch('sys.argv', ['localization.py', 'check', '--allow-pending']), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(localization.main(), 0)
            self.assertIn('Localization pending', out.getvalue())
            with mock.patch('sys.argv', ['localization.py', 'pending', '--json']), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(localization.main(), 0)
            self.assertEqual(json.loads(out.getvalue().split('\n{', 1)[1].join(['{', ''])), {'fr': ['Export {0} photos']})
            with mock.patch('sys.argv', ['localization.py', 'extract', '--allow-pending']), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    localization.main()


if __name__ == '__main__':
    unittest.main()
