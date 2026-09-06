"""Exercise the native disk writer across process exits, independently of Python."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires Swift on macOS")
class NativeEditRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="lighttable-native-recovery-test-")
        directory = Path(cls.build.name)
        source = (ROOT / "app/main.swift").read_text()
        store = source.split("// MARK: - Durable edit recovery", 1)[1].split("// MARK: - App", 1)[0]
        main = directory / "main.swift"
        main.write_text("import Foundation\nimport Darwin\n" + store + '''
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
do {
    let body = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
    let result = try EditRecoveryStore(root: root).perform(body)
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: ["result": result]))
} catch {
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: ["error": error.localizedDescription]))
    exit(1)
}
''')
        cls.binary = directory / "recovery"
        env = dict(os.environ, SWIFT_MODULECACHE_PATH=str(directory / "swift-cache"),
                   CLANG_MODULE_CACHE_PATH=str(directory / "clang-cache"))
        subprocess.run(["swiftc", str(main), "-o", str(cls.binary)], env=env,
                       check=True, capture_output=True, timeout=90)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scope = "a" * 64
        self.key = "b" * 64

    def request(self, operation, **fields):
        response = subprocess.run([str(self.binary), self.directory.name],
            input=json.dumps((dict(operation=operation, scope=self.scope, key=self.key) | fields)),
            text=True, capture_output=True, timeout=10)
        return response.returncode, json.loads(response.stdout)

    def record(self, token, exposure):
        return {"token": token, "name": "a.RAW", "payload": {
            "state": {"name": "a.RAW", "grade": {"exposure": exposure},
                      "crop": {"x": .2}, "masks": [{"data": [1, 2]}]}}}

    def test_process_restart_and_old_ack_preserve_the_newest_complete_draft(self):
        self.assertEqual(self.request("put", value=self.record("old", 1))[0], 0)
        self.assertEqual(self.request("put", value=self.record("new", 2))[0], 0)
        self.assertEqual(self.request("remove", token="old")[0], 0)
        self.assertEqual(self.request("list")[1]["result"], [self.record("new", 2)])
        self.assertEqual(self.request("remove", token="new")[0], 0)
        self.assertEqual(self.request("list")[1]["result"], [])

    def test_malformed_draft_is_preserved_for_recovery(self):
        self.request("put", value=self.record("one", 1))
        path = Path(self.directory.name) / self.scope / (self.key + ".json")
        path.write_text("{damaged")
        self.assertIn("journalError", self.request("list")[1]["result"][0])
        self.assertEqual(self.request("put", value=self.record("two", 2))[0], 1)
        self.assertEqual(path.read_text(), "{damaged")

    def test_invalid_identifier_and_unwritable_storage_fail_without_ack(self):
        self.assertEqual(self.request("put", scope="../escape", value=self.record("one", 1))[0], 1)
        path = Path(self.directory.name) / self.scope
        path.write_text("blocking file")
        self.assertEqual(self.request("put", value=self.record("one", 1))[0], 1)
        self.assertEqual(path.read_text(), "blocking file")
