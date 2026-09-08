"""English message declarations stay extractable without running the app."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'extract-web-i18n.py'
SPEC = importlib.util.spec_from_file_location('web_i18n_extraction', SCRIPT)
extractor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extractor)


class WebI18nExtractionTests(unittest.TestCase):
    def extract(self, source, filename='app.js'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'web').mkdir()
            (root / 'web' / filename).write_text(source)
            return extractor.extract(root)

    def test_aliases_plural_forms_and_template_expressions(self):
        messages, plurals = self.extract('''
            import { t as tr, tn as trn } from './i18n.js';
            tr('Save');
            const html = `<span>${tr("Photo {name}", {name})}</span>`;
            trn('{count} photo', '{count} photos', count);
        ''')
        self.assertEqual(messages, ['Photo {name}', 'Save', '{count} photo', '{count} photos'])
        self.assertEqual(plurals, {'pairs': [{'one': '{count} photo', 'other': '{count} photos'}]})

    def test_comments_strings_and_regex_are_not_declarations(self):
        messages, _ = self.extract(r'''
            import { t as tr } from './i18n.js';
            // tr('Comment')
            /* tr('Block comment') */
            const fixture = "tr('String')";
            const template = `tr('Template text')`;
            const pattern = /tr\('Regex'\)/;
            tr('Real message');
        ''')
        self.assertEqual(messages, ['Real message'])

    def test_nested_template_expressions_and_escaped_strings(self):
        messages, _ = self.extract(r'''
            import { t } from '/web/i18n.js';
            const html = `<p>${items.map(item => `${t('It\'s saved')} ${item.name}`).join('')}</p>`;
            t("Line one\nLine two");
        ''')
        self.assertEqual(messages, ["It's saved", 'Line one\nLine two'])

    def test_runtime_static_marker_lookup_is_allowed(self):
        messages, _ = self.extract("export function t(source) { return source; } export function tn(one, other, count) {} t(source); t('Load failed');", 'i18n.js')
        self.assertEqual(messages, ['Load failed'])

    def test_dynamic_and_concatenated_sources_are_rejected(self):
        for call in ["tr(message)", "tr('Partial ' + name)", "trn('One', plural, count)"]:
            with self.subTest(call=call), self.assertRaises(ValueError):
                self.extract("import { t as tr, tn as trn } from './i18n.js'; " + call)

    def test_unrelated_local_t_is_not_a_translation(self):
        messages, _ = self.extract("// import {t} from './i18n.js';\n function t(value) { return value; } t('Protocol value');")
        self.assertEqual(messages, [])


if __name__ == '__main__':
    unittest.main()
