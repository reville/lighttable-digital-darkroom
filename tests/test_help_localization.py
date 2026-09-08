"""Translated help must be complete, current, and keep working IDs and tokens."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('help_content_localization', ROOT / 'scripts/help_content.py')
help_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(help_content)


class HelpLocalizationTests(unittest.TestCase):
    def test_capture_time_remove_command_is_not_translated(self):
        source = 'Leave it blank to keep the existing zone, or enter remove to clear it.'
        with self.assertRaisesRegex(help_content.HelpError, 'literal input'):
            help_content.validate_translation(source, 'Geben Sie entfernen ein.', 'de')
        help_content.validate_translation(source, 'Geben Sie remove ein.', 'de')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = {'version': 1, 'articles': [
            {'id': 'export-file', 'title': 'Export a photo', 'category': 'Export',
             'summary': 'Choose an output folder.', 'keywords': ['export', 'save'],
             'sections': [{'title': 'Choose a name',
                           'paragraphs': ['Use {filename}_{sequence} and keep .lighttable-state.json.',
                                          'Supported extensions include .xmp and .ltpreset.'],
                           'steps': ['Open Export.', 'Choose a folder.'],
                           'tips': ['Check photo.png before continuing.']}],
             'related': ['edit-photo']},
            {'id': 'edit-photo', 'title': 'Edit a photo', 'category': 'Editing',
             'summary': 'Change the image.', 'keywords': ['adjustment'],
             'sections': [{'title': 'Start editing', 'steps': ['Open a still photo.']}],
             'related': ['export-file']},
        ]}
        self.messages = sorted(set(help_content.help_messages(self.bundle) + ['Export {0} photos']))
        self.source_digest = help_content.localization_source_digest(self.messages)
        self.catalog = {
            'version': 1, 'locale': 'fr', 'sourceDigest': self.source_digest,
            'generation': {'model': 'test', 'reviewStatus': 'machine-translated'},
            'messages': {source: 'Traduction : ' + source for source in self.messages},
        }
        self.write(help_content.BUNDLE, self.bundle)
        self.write(help_content.LOCALIZATION_SOURCE,
                   {'version': 1, 'messages': self.messages, 'sourceDigest': self.source_digest})
        self.write(help_content.LOCALE_MANIFEST, {'version': 1, 'locales': [
            {'code': 'en', 'name': 'English', 'nativeName': 'English', 'dir': 'ltr'},
            {'code': 'fr', 'name': 'French', 'nativeName': 'Français', 'dir': 'ltr'},
        ]})
        self.write('web/locales/fr.json', self.catalog)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def run_command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return help_content.run(list(args), self.root)

    def localized(self):
        return json.loads((self.root / 'web/locales/help/fr.json').read_text())

    def test_digest_uses_sorted_unique_compact_unicode_json(self):
        expected = hashlib.sha256('["Alpha","Éclair"]'.encode('utf-8')).hexdigest()
        self.assertEqual(help_content.localization_source_digest(['Éclair', 'Alpha', 'Alpha']), expected)

    def test_every_prose_leaf_is_translated_and_structural_fields_stay_stable(self):
        before = copy.deepcopy(self.bundle)
        english_bytes = (self.root / help_content.BUNDLE).read_bytes()
        self.assertEqual(self.run_command('localization-build'), 0)
        localized = self.localized()
        self.assertEqual(localized['locale'], 'fr')
        self.assertEqual(localized['sourceDigest'], self.source_digest)
        self.assertEqual(len(localized['articles']), len(self.bundle['articles']))
        for original, translated in zip(self.bundle['articles'], localized['articles']):
            for field in ('id', 'category', 'related'):
                self.assertEqual(translated[field], original[field])
            for field in ('title', 'summary'):
                self.assertEqual(translated[field], self.catalog['messages'][original[field]])
            self.assertEqual(translated['categoryLabel'], self.catalog['messages'][original['category']])
            self.assertEqual(translated['keywords'], [self.catalog['messages'][s] for s in original['keywords']])
            for old_section, new_section in zip(original['sections'], translated['sections']):
                self.assertEqual(set(old_section), set(new_section))
                self.assertEqual(new_section['title'], self.catalog['messages'][old_section['title']])
                for field in ('paragraphs', 'steps', 'tips'):
                    if field in old_section:
                        self.assertEqual(new_section[field], [self.catalog['messages'][s] for s in old_section[field]])
        self.assertEqual(self.bundle, before)
        self.assertEqual((self.root / help_content.BUNDLE).read_bytes(), english_bytes)
        self.assertEqual(self.run_command('localization-check'), 0)
        self.assertFalse((self.root / 'web/locales/help/en.json').exists())

    def test_categories_are_part_of_source_messages_even_without_articles(self):
        self.assertTrue(set(help_content.CATEGORIES).issubset(help_content.help_messages(self.bundle)))

    def test_missing_help_translation_does_not_fall_back_to_english(self):
        self.catalog['messages'].pop('Choose an output folder.')
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)
        self.assertFalse((self.root / 'web/locales/help/fr.json').exists())

    def test_catalog_must_also_cover_non_help_ui_messages(self):
        self.catalog['messages'].pop('Export {0} photos')
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_blank_and_obsolete_translations_fail(self):
        self.catalog['messages']['export'] = '  '
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)
        self.catalog['messages']['export'] = 'Exporter'
        self.catalog['messages']['Removed wording'] = 'Ancien texte'
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_explicit_identity_translation_is_allowed_for_technical_names(self):
        self.catalog['messages']['Export'] = 'Export'
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 0)
        self.assertEqual(self.localized()['articles'][0]['categoryLabel'], 'Export')

    def test_stale_catalog_and_stale_manifest_digests_fail(self):
        self.catalog['sourceDigest'] = 'outdated'
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)
        self.catalog['sourceDigest'] = self.source_digest
        self.write('web/locales/fr.json', self.catalog)
        self.write(help_content.LOCALIZATION_SOURCE,
                   {'version': 1, 'messages': self.messages, 'sourceDigest': 'outdated'})
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_new_help_prose_requires_source_manifest_refresh(self):
        self.bundle['articles'][0]['summary'] = 'The output behavior has changed.'
        self.write(help_content.BUNDLE, self.bundle)
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_placeholder_names_numbers_and_counts_are_preserved(self):
        for original, translated in [
            ('Use {filename}.', 'Utiliser {nom}.'),
            ('Export {0} photos', 'Exporter {1} photos'),
            ('Use {name} with {name}.', 'Utiliser {name}.'),
            ('Open Export.', 'Ouvrir {dialog}.'),
        ]:
            with self.subTest(original=original):
                with self.assertRaisesRegex(help_content.HelpError, 'placeholders changed'):
                    help_content.validate_translation(original, translated, 'fr')
        help_content.validate_translation('Export {0} to {folder}', 'Vers {folder}, exporter {0}', 'fr')

    def test_filename_and_extension_literals_cannot_be_translated(self):
        for source, translated in [
            ('Keep .lighttable-state.json.', 'Garder .etat-lighttable.json.'),
            ('Open photo.png.', 'Ouvrir photo.jpg.'),
            ('Import .ltpreset or .xmp.', 'Importer .preset ou .xmp.'),
        ]:
            with self.subTest(source=source):
                with self.assertRaisesRegex(help_content.HelpError, 'filenames or extensions changed'):
                    help_content.validate_translation(source, translated, 'fr')
        help_content.validate_translation('Keep .lighttable-state.json and photo.png.',
                                          '保留.lighttable-state.json和photo.png。', 'zh-CN')

    def test_stale_or_missing_generated_bundle_fails_check(self):
        self.assertEqual(self.run_command('localization-check'), 1)
        self.assertEqual(self.run_command('localization-build'), 0)
        localized = self.localized()
        localized['articles'][0]['id'] = 'broken-link'
        self.write('web/locales/help/fr.json', localized)
        self.assertEqual(self.run_command('localization-check'), 1)
        self.assertEqual(self.run_command('localization-build'), 0)
        self.catalog['messages']['Export a photo'] = 'Exporter une photo'
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-check'), 1)

    def test_default_build_requires_all_declared_catalogs_before_any_write(self):
        self.write(help_content.LOCALE_MANIFEST, {'version': 1, 'locales': ['en', 'fr', 'de']})
        self.assertEqual(self.run_command('localization-build'), 1)
        self.assertFalse((self.root / 'web/locales/help/fr.json').exists())
        self.assertEqual(self.run_command('localization-build', '--locale', 'fr'), 0)
        self.assertEqual(self.run_command('localization-check', '--locale', 'fr'), 0)

    def test_locale_identity_and_path_safety(self):
        self.assertEqual(self.run_command('localization-build', '--locale', '../fr'), 1)
        self.assertEqual(self.run_command('localization-build', '--locale', 'es'), 1)
        self.assertEqual(self.run_command('localization-build', '--locale', 'en'), 1)
        self.catalog['locale'] = 'es'
        self.write('web/locales/fr.json', self.catalog)
        self.assertEqual(self.run_command('localization-build'), 1)
        self.write(help_content.LOCALE_MANIFEST, {'version': 1, 'locales': ['en', '../fr']})
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_unknown_prose_fields_cannot_silently_ship_in_english(self):
        self.bundle['articles'][0]['sections'][0]['newProse'] = 'Needs a translation too.'
        self.write(help_content.BUNDLE, self.bundle)
        self.assertEqual(self.run_command('localization-build'), 1)

    def test_localization_never_acknowledges_source_reviews(self):
        self.write(help_content.LOCK, {'sentinel': 'review state must not change'})
        before = (self.root / help_content.LOCK).read_bytes()
        self.assertEqual(self.run_command('localization-build'), 0)
        self.assertEqual((self.root / help_content.LOCK).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
