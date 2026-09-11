# SPDX-License-Identifier: GPL-3.0-only
"""Execute native import IO and first-run persistence without accessing Photos."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native Photos import is macOS-only")
class PhotosLibraryImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise unittest.SkipTest("swiftc is unavailable")
        source = (ROOT / "app" / "main.swift").read_text()
        locale_core = source.split("// BEGIN NATIVE LOCALIZATION CORE", 1)[1].split(
            "// END NATIVE LOCALIZATION CORE", 1)[0].split("\n", 1)[1]
        importer = source[source.index("private final class PhotosLibraryImporter"):].split(
            "\n// MARK:", 1)[0]
        initialization = source[source.index("        let defaults = UserDefaults.standard"):
                                source.index("        var isDir: ObjCBool = false", source.index("func applicationDidFinishLaunching"))]
        launch = source[source.index("    private func launch(folder: String)"):]
        persistence = launch[launch.index("        let automated ="):
                             launch.index("        window.title =")]

        def method(name):
            start = source.index(f"    private func {name}(")
            end = source.index("\n    }", start) + len("\n    }")
            return source[start:end]

        methods = "\n\n".join(method(name) for name in (
            "finishFirstRun", "loadSources", "saveSources", "addSource",
            "normalized", "isDirectory", "sourcePayload", "replaySetupEvents",
            "completeSetupCatalogImport",
        ))

        def isolate(body):
            # Execute the actual control flow with disposable inputs. Only
            # process-global defaults/environment are replaced by test fields.
            return body.replace("UserDefaults.standard", "self.defaults").replace(
                "ProcessInfo.processInfo.environment", "self.environment")

        harness = (ROOT / "tests" / "fixtures" / "photos-library-import.swift").read_text()
        for placeholder, implementation in (
            ("IMPORTER_SOURCE", importer),
            ("INITIALIZATION_SOURCE", isolate(initialization)),
            ("STARTUP_PERSISTENCE_SOURCE", isolate(persistence)),
            ("FIRST_RUN_METHODS", isolate(methods)),
        ):
            harness = harness.replace(placeholder, implementation)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        swift_file = directory / "main.swift"
        # Use the production locale core with isolated, absent catalogs. Import
        # IO tests exercise the same English fallback without user preferences.
        locale_setup = "\n" + locale_core + f'''
private let nativeLocalization = NativeLocaleStore(
    directory: URL(fileURLWithPath: {json.dumps(str(directory))}),
    preferencesURL: URL(fileURLWithPath: {json.dumps(str(directory / "prefs.json"))}))
private func L(_ source: String, _ arguments: [String: String] = [:]) -> String {{
    nativeLocalization.text(source, arguments)
}}
'''
        swift_file.write_text(harness.replace("import Foundation", "import Foundation\n" + locale_setup, 1))
        cls.executable = directory / "photos-import-test"
        result = subprocess.run([
            swiftc, "-swift-version", "5", "-module-cache-path",
            str(directory / "module-cache"), str(swift_file), "-o", str(cls.executable),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_fixture(self, scenario):
        result = subprocess.run([str(self.executable), scenario], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)

    def test_streamed_originals_retry_cancellation_and_concurrent_imports(self):
        self.run_fixture("importer")

    def test_first_run_detection_persistence_and_navigation_replay(self):
        self.run_fixture("first-run")
