# SPDX-License-Identifier: GPL-3.0-only
"""Check native web UI trust boundaries and one-shot confirmation replies."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"),
                     "requires Swift on macOS")
class NativeWebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lighttable-native-web-ui-")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        source = (ROOT / "app/main.swift").read_text()
        helpers = source.split("// MARK: - Native web UI", 1)[1].split(
            "// MARK: - Preset links", 1)[0]
        main = directory / "main.swift"
        main.write_text("import Foundation\n" + helpers + r'''
switch CommandLine.arguments[1] {
case "origin":
    precondition(isLocalEditorPage(URL(string: "http://127.0.0.1:8350/"), port: 8350))
    precondition(isLocalEditorPage(URL(string: "http://127.0.0.1:8350/web/index.html"), port: 8350))
    for raw in ["https://127.0.0.1:8350/", "http://127.0.0.1:8351/",
                "http://localhost:8350/", "http://127.0.0.1.evil.example:8350/",
                "https://example.com/", "file:///tmp/index.html", "about:blank",
                "http://user:password@127.0.0.1:8350/"] {
        precondition(!isLocalEditorPage(URL(string: raw), port: 8350), raw)
    }
    precondition(!isLocalEditorPage(nil, port: 8350))
    precondition(!isLocalEditorPage(URL(string: "http://127.0.0.1:0/"), port: 0))
case "links":
    for raw in ["https://maps.apple.com/?ll=42,-71&q=Photo", "http://example.com/help"] {
        let url = URL(string: raw)!
        precondition(externalWebURL(url) == url)
    }
    for raw in ["file:///tmp/photo.jpg", "javascript:alert(1)", "data:text/html,test",
                "lighttable://preset/test/example", "mailto:user@example.com",
                "https://user:password@example.com/", "https:/missing-host"] {
        precondition(externalWebURL(URL(string: raw)) == nil, raw)
    }
    precondition(externalWebURL(nil) == nil)
case "replies":
    var values: [Bool] = []
    var reply: NativeJavaScriptReply? = NativeJavaScriptReply { values.append($0) }
    reply?.resolve(true)
    reply?.resolve(false) // navigation/close after the user's OK
    reply = nil
    precondition(values == [true])
    values.removeAll()
    reply = NativeJavaScriptReply { values.append($0) }
    reply?.resolve(false) // navigation/close before the sheet callback
    reply?.resolve(true)
    reply = nil
    precondition(values == [false])
    values.removeAll()
    reply = NativeJavaScriptReply { values.append($0) }
    reply = nil // an abandoned request always cancels
    precondition(values == [false])
    values.removeAll()
    reply = NativeJavaScriptReply {
        values.append($0)
        reply?.resolve(false) // reentrant completion cannot run twice
    }
    reply?.resolve(true)
    reply = nil
    precondition(values == [true])
default:
    fatalError("unknown case")
}
print("passed")
''')
        cls.binary = directory / "native-web-ui"
        result = subprocess.run([
            "swiftc", "-swift-version", "5", "-module-cache-path",
            str(directory / "modules"), str(main), "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, case):
        result = subprocess.run([str(self.binary), case], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_current_local_editor_origin_is_trusted(self):
        self.run_case("origin")

    def test_external_links_allow_only_http_and_https_without_credentials(self):
        self.run_case("links")

    def test_confirmation_completes_once_and_defaults_to_cancel(self):
        self.run_case("replies")


if __name__ == "__main__":
    unittest.main()
