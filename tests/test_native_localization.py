# SPDX-License-Identifier: GPL-3.0-only
"""Native locale behavior and persistence checks, independent of a user catalog."""
import importlib.util
import json
import plistlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class NativeLocalizationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('swift'), 'Swift is required for native locale core checks')
    def test_swift_locale_validation_and_lossless_preferences(self):
        source = (ROOT / 'app/main.swift').read_text()
        core = source.split('// BEGIN NATIVE LOCALIZATION CORE', 1)[1].split('// END NATIVE LOCALIZATION CORE', 1)[0]
        core = core[core.index('\n') + 1:]
        with tempfile.TemporaryDirectory(prefix='lighttable-native-locale-') as tmp:
            root = Path(tmp)
            manifest = {'version': 1, 'sourceLocale': 'en', 'locales': [
                {'code': code, 'name': code, 'nativeName': code, 'dir': 'ltr'}
                for code in ['en', 'es', 'zh-Hans', 'zh-Hant']]}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            (root / 'es.json').write_text(json.dumps({'version': 1, 'locale': 'es', 'messages': {
                'Open {name} at {path}': 'En {path}, abre {name}',
                'Delete {name}': 'Borrar', 'Blank': '  ', 'Photo': 'Foto'}}))
            original = {'locale': 'es', 'localeChosen': True, 'autoAdvance': False,
                        'activeFolders': {'/Photos': 'Family'}, 'nested': [1, {'keep': 'yes'}]}
            (root / 'prefs.json').write_text(json.dumps(original))
            harness = r'''
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let prefsURL = root.appendingPathComponent("prefs.json")
let store = NativeLocaleStore(directory: root, preferencesURL: prefsURL)
precondition(store.locale == "es")
precondition(store.suggested(for: "es_MX") == "es")
precondition(store.suggested(for: "zh-TW") == "zh-Hant")
precondition(store.suggested(for: "zh-Hans-HK") == "zh-Hans")
precondition(store.suggested(for: "zz-ZZ") == "en")
precondition(NativeLocaleStore.isEnglish("en_US"))
precondition(!NativeLocaleStore.isEnglish("fr-FR"))
for code in ["../es", "es.json", "es/en", "en-"] { precondition(!NativeLocaleStore.safeCode(code)) }
precondition(store.text("Photo") == "Foto")
precondition(store.text("Delete {name}", ["name": "photo.jpg"]) == "Delete photo.jpg")
precondition(store.text("Blank") == "Blank")
precondition(store.text("Open {name} at {path}", ["name": "{path}", "path": "café/東京.jpg"]) == "En café/東京.jpg, abre {path}")
try store.saveChoice("zh-Hant")
precondition(store.locale == "zh-Hant")
let saved = try store.preferences()
precondition(saved["locale"] as? String == "zh-Hant")
precondition(saved["localeChosen"] as? Bool == true)
precondition(saved["autoAdvance"] as? Bool == false)
precondition((saved["activeFolders"] as? [String: String])?["/Photos"] == "Family")
precondition((saved["nested"] as? [Any])?.count == 2)
precondition(store.text("Photo") == "Photo") // Missing catalog safely falls back.
let before = try Data(contentsOf: prefsURL)
do { try store.saveChoice("../../en"); fatalError("Unsafe locale accepted") } catch {}
let afterInvalidChoice = try Data(contentsOf: prefsURL)
precondition(afterInvalidChoice == before)
try Data("broken".utf8).write(to: prefsURL)
do { try store.saveChoice("en"); fatalError("Malformed prefs overwritten") } catch {}
let malformed = try String(contentsOf: prefsURL, encoding: .utf8)
precondition(malformed == "broken")
precondition(store.locale == "zh-Hant")
let blocker = root.appendingPathComponent("parent-file")
try Data("keep".utf8).write(to: blocker)
let blocked = NativeLocaleStore(directory: root, preferencesURL: blocker.appendingPathComponent("prefs.json"))
do { try blocked.saveChoice("es"); fatalError("Failed write reported saved") } catch {}
precondition(blocked.locale == "en")
let preservedBlocker = try String(contentsOf: blocker, encoding: .utf8)
precondition(preservedBlocker == "keep")
print("Native locale validation, fallback, Unicode placeholders, and preference preservation passed")
'''
            script = root / 'main.swift'
            script.write_text('import Foundation\n' + core + harness)
            result = subprocess.run(['swift', '-module-cache-path', str(root / 'module-cache'), str(script), str(root)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inventory_detects_new_native_strings(self):
        spec = importlib.util.spec_from_file_location('native_sources', ROOT / 'scripts/native_localization_sources.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        current = module.extract_native_sources(ROOT)
        self.assertEqual(current, json.loads((ROOT / 'docs/localization/native-source.json').read_text()))
        with tempfile.TemporaryDirectory(prefix='lighttable-source-inventory-') as tmp:
            root = Path(tmp)
            for relative in module.FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((ROOT / relative).read_text())
            swift = root / 'app/main.swift'
            swift.write_text(swift.read_text() + '\nlet newLabel = L("New review action")\n')
            self.assertIn('New review action', module.extract_native_sources(root))
            swift.write_text(swift.read_text() + '\nlet untranslated = NSMenu(title: "Untranslated action")\n')
            with self.assertRaisesRegex(ValueError, 'native UI string needs L/tr'):
                module.extract_native_sources(root)

    def test_permission_resources_package_all_locales_without_changing_bundle_identity(self):
        spec = importlib.util.spec_from_file_location('native_sources', ROOT / 'scripts/native_localization_sources.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='lighttable-native-package-') as tmp:
            root = Path(tmp)
            (root / 'app').mkdir()
            permission = 'Read originals from your Photos library.'
            (root / 'app/Info.plist').write_bytes(plistlib.dumps({
                'NSPhotoLibraryUsageDescription': permission}))
            app = root / 'LightTable.app'
            contents = app / 'Contents'
            contents.mkdir(parents=True)
            plist = contents / 'Info.plist'
            plist.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'example.personal',
                                             'CFBundleName': 'Personal LightTable'}))
            catalogs = root / 'locales'
            catalogs.mkdir()
            (catalogs / 'manifest.json').write_text(json.dumps({
                'version': 1, 'sourceLocale': 'en',
                'locales': [{'code': code} for code in ['en', 'es']]}))
            translations = {'en': permission, 'es': 'Leer originales de “Fotos” & conservarlos.'}
            for code, text in translations.items():
                (catalogs / f'{code}.json').write_text(json.dumps({
                    'version': 1, 'locale': code, 'messages': {permission: text}}))
            self.assertEqual(module.bundle_localizations(app, catalogs, root), 2)
            saved = plistlib.loads(plist.read_bytes())
            self.assertEqual(saved['CFBundleIdentifier'], 'example.personal')
            self.assertEqual(saved['CFBundleName'], 'Personal LightTable')
            self.assertEqual(saved['CFBundleLocalizations'], ['en', 'es'])
            self.assertEqual(saved['NSPhotoLibraryUsageDescription'], permission)
            for code, text in translations.items():
                strings = contents / 'Resources' / f'{code}.lproj/InfoPlist.strings'
                self.assertEqual(plistlib.loads(strings.read_bytes()), {
                    'NSPhotoLibraryUsageDescription': text})
            before = {path: path.read_bytes() for path in contents.rglob('*') if path.is_file()}
            (catalogs / 'es.json').write_text(json.dumps({
                'version': 1, 'locale': 'es', 'messages': {}}))
            with self.assertRaisesRegex(ValueError, 'missing NSPhotoLibraryUsageDescription'):
                module.bundle_localizations(app, catalogs, root)
            self.assertEqual(before, {path: path.read_bytes() for path in contents.rglob('*') if path.is_file()})

    def test_mac_choice_precedes_startup_and_uses_saved_event(self):
        source = (ROOT / 'app/main.swift').read_text()
        launch = source.split('func applicationDidFinishLaunching', 1)[1].split('func applicationWillTerminate', 1)[0]
        self.assertLess(launch.index('chooseInitialLanguage()'), launch.index('buildMenu()'))
        self.assertLess(launch.index('chooseInitialLanguage()'), launch.index('pickFolder('))
        self.assertIn('try nativeLocalization.saveChoice(selected)', source)
        self.assertIn('while retry {', source)
        self.assertIn('JSONEncoder().encode(Locale.preferredLanguages)', source)
        event = source.split('case "localizationChanged":', 1)[1].split('case "openRecoveryFolder":', 1)[0]
        self.assertIn('try nativeLocalization.reload()', event)
        self.assertIn('buildMenu()', event)

if __name__ == '__main__':
    unittest.main()
