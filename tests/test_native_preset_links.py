"""Run native link validation and startup queue behavior without opening an app."""
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires Swift on macOS")
class NativePresetLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lighttable-preset-links-")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        source = (ROOT / "app/main.swift").read_text()
        parser = source.split("// MARK: - Preset links", 1)[1].split("// MARK: - About", 1)[0]
        methods = source.split("    func application(_ application: NSApplication, open urls: [URL])", 1)[1].split(
            "    func applicationWillTerminate", 1)[0]
        methods = "    func application(_ application: StubApplication, open urls: [URL])" + methods
        main = directory / "main.swift"
        main.write_text("import Foundation\n" + parser + '''
final class StubWindow { func makeKeyAndOrderFront(_ sender: Any?) {} }
final class StubApplication { func activate(ignoringOtherApps: Bool) {} }
final class Host {
    var pendingPresetLinks: [String] = []
    var presetLinksReady = false
    var window: StubWindow? = StubWindow()
    var webView: Bool? = true
    var events: [[String: Any]] = []
    func sendEvent(_ payload: [String: Any]) { events.append(payload) }
    func ready() { presetLinksReady = true; deliverPresetLinks() }
''' + methods + '''
}
if CommandLine.arguments[1] == "--export" {
    let text = "{\\"name\\":\\"Café\\"}"
    precondition(try! presetExportData(content: text) == Data(text.utf8))
    precondition(try! presetExportData(content: "UEsDBAD/", encoding: "base64") == Data([80,75,3,4,0,255]))
    for invalid in ["not base64!", "UEsDBAD_", "AA", "AB==", "AA==\\n"] {
        precondition((try? presetExportData(content: invalid, encoding: "base64")) == nil)
    }
    precondition((try? presetExportData(content: "text", encoding: "hex")) == nil)
    precondition((try? presetExportData(content: String(repeating: "A", count: 15 * 1024 * 1024 + 1), encoding: "base64")) == nil)
    print("export passed")
} else if CommandLine.arguments[1] == "--queue" {
    let host = Host()
    let app = StubApplication()
    let link = URL(string: "lighttable://preset/lighttable/warm-film")!
    host.application(app, open: [link, URL(string: "https://example.com/preset")!])
    precondition(host.events.isEmpty)
    host.ready()
    precondition(host.events.count == 1)
    precondition(host.events[0]["id"] as? String == "lighttable/warm-film")
    host.application(app, open: [link])
    precondition(host.events.count == 2)
    host.presetLinksReady = false
    for index in 0..<12 {
        host.application(app, open: [URL(string: "lighttable://preset/test/look-\\(index)")!])
    }
    precondition(host.events.count == 2 && host.pendingPresetLinks.count == 8)
    host.ready()
    precondition(host.events.count == 10)
    precondition(host.events[2]["id"] as? String == "test/look-4")
    precondition(host.events.last?["id"] as? String == "test/look-11")
    precondition(host.pendingPresetLinks.isEmpty)
    print("queue passed")
} else {
    let ids = CommandLine.arguments.dropFirst().map { presetID(from: $0) as Any? ?? NSNull() }
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: ids))
}
''')
        cls.binary = directory / "preset-links"
        result = subprocess.run(["swiftc", "-swift-version", "5", "-module-cache-path",
                                 str(directory / "modules"), str(main), "-o", str(cls.binary)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def parse(self, *links):
        result = subprocess.run([str(self.binary), *links], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_accepts_bounded_canonical_ids(self):
        ids = ["lighttable/warm-film", "alice-2/portrait-1", "a" * 64 + "/" + "b" * 64]
        self.assertEqual(self.parse(*(f"lighttable://preset/{value}" for value in ids)), ids)

    def test_rejects_fetch_urls_traversal_encoding_and_oversized_ids(self):
        links = ["https://preset/a/b", "file:///a/b", "lighttable://preset:80/a/b",
                 "lighttable://user@preset/a/b"]
        suffixes = ["", "a", "a/", "/a", "a/b/c", "../a", "a/../b", "a/%2e%2e",
                    "a/b?url=https://evil.example", "a/b#x", "a/b\\c", "é/b", "A/b",
                    "a/b\n", "a_b/c", "-a/b", "a/" + "b" * 65]
        links += [f"lighttable://preset/{value}" for value in suffixes]
        self.assertEqual(self.parse(*links), [None] * len(links))

    def test_text_and_base64_exports_preserve_bytes_and_reject_invalid_input(self):
        result = subprocess.run([str(self.binary), "--export"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("export passed", result.stdout)

    def test_startup_delivery_repeated_links_and_reload_queue(self):
        result = subprocess.run([str(self.binary), "--queue"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("queue passed", result.stdout)


class PresetPackagingTests(unittest.TestCase):
    def test_each_runtime_bundles_offline_presets_and_the_library_module(self):
        release = (ROOT / "scripts/build-release.sh").read_text()
        personal = (ROOT / "scripts/update-personal-app.sh").read_text()
        windows = (ROOT / "scripts/windows/build-release.ps1").read_text()
        self.assertIn('"$ROOT/presets" "$PAYLOAD/presets"', release)
        self.assertIn('"$ROOT/presets/" "$STAGE_PAYLOAD/presets/"', personal)
        self.assertIn('"$ROOT"/*.py', release)
        self.assertIn('"$ROOT"/*.py', personal)
        self.assertIn('"stage-python-modules.py"', windows)
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary) / "payload"
            subprocess.run([sys.executable, str(ROOT / "scripts/windows/stage-python-modules.py"),
                            str(ROOT), str(resources)], check=True, capture_output=True, timeout=10)
            for name in ("preset_library.py", "preset_submission.py"):
                self.assertEqual((resources / name).read_bytes(), (ROOT / name).read_bytes())
        self.assertIn('Copy-Item (Join-Path $Project "presets") $Resources -Recurse', windows)
        hash_source = personal.split('SOURCE_TREE_HASH="$(hash_sources', 1)[1].split('SOURCE_REVISION=', 1)[0]
        self.assertIn('"$ROOT/presets"', hash_source)

    def test_developer_bundle_declares_the_same_preset_protocol_as_release(self):
        build = (ROOT / "build-app.sh").read_text()
        template = build.split('<<PLIST\n', 1)[1].split('\nPLIST', 1)[0]
        developer = plistlib.loads(template.encode())
        release = plistlib.loads((ROOT / "app/Info.plist").read_bytes())
        self.assertEqual(developer["CFBundleURLTypes"], release["CFBundleURLTypes"])

    def test_macos_protocol_registration_is_refreshed_by_personal_updates(self):
        plist = plistlib.loads((ROOT / "app/Info.plist").read_bytes())
        self.assertEqual(plist["CFBundleURLTypes"][0]["CFBundleURLSchemes"], ["lighttable"])
        personal = (ROOT / "scripts/update-personal-app.sh").read_text()
        self.assertIn('target["CFBundleURLTypes"] = source["CFBundleURLTypes"]', personal)


if __name__ == "__main__":
    unittest.main()
