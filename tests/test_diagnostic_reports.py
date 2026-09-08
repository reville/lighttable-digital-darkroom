"""Run the actual Swift report store across process deaths and clean launches."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit
import Foundation
import Darwin

let root = URL(fileURLWithPath: CommandLine.arguments[2])
let store = DiagnosticStore(root: root.appendingPathComponent("Diagnostics"))
let log = root.appendingPathComponent("server.log")
let fault = root.appendingPathComponent("fault.log")
func begin() { store.begin(log: log, fault: fault, catalog: root) }
func output(_ value: Any) {
    print(String(data: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), encoding: .utf8)!)
}
switch CommandLine.arguments[1] {
case "crash":
    begin()
    // Exit without the normal termination hook. The kernel releases the lock.
    exit(2)
case "hold":
    begin()
    print("ready"); fflush(stdout)
    sleep(20) // hard cap; the Python test terminates only this child in finally.
    store.end()
case "quit":
    begin(); store.end()
case "inspect":
    begin()
    output(["count": store.incidents().count, "pending": store.pending != nil,
            "report": store.incidents().last?.report ?? ""])
    store.end()
case "acknowledge":
    begin()
    if let incident = store.pending { store.markPresented(incident) }
    store.end()
case "engine":
    begin()
    let start = Date()
    store.captureEngine(id: "fixture", pid: 42, executable: "/engine/python", startedAt: start, status: 11)
    store.captureEngine(id: "fixture", pid: 42, executable: "/engine/python", startedAt: start, status: 11)
    output(["count": store.incidents().count, "report": store.incidents().last!.report])
    store.end()
case "retention":
    begin()
    for index in 0..<14 {
        store.captureEngine(id: "fixture-\(index)", pid: 42, executable: "/engine/python", startedAt: Date(), status: 11)
    }
    output(["count": store.incidents().count])
    store.end()
case "redact":
    output(["report": DiagnosticReport.logExcerpt(DiagnosticReport.read(log)),
            "mailto": DiagnosticReport.emailURL().absoluteString])
case "mac":
    let incident = DiagnosticIncident(id: "fixture", kind: "engine",
        startedAt: Date().addingTimeInterval(-60), pid: 42,
        executable: "/engine/python", report: "fixture")
    output(["report": MacCrashReport.collect(for: incident, directory: root) ?? ""])
case "buttons":
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let clipboard = NSPasteboard.withUniqueName()
    defer { clipboard.releaseGlobally() }
    var opened = ""
    let panel = DiagnosticReportWindow(report: "fixture diagnostic report", incident: nil,
        pasteboard: clipboard, openEmail: { url in opened = url.absoluteString; return false })
    func buttons(_ view: NSView) -> [NSButton] {
        (view as? NSButton).map { [$0] } ?? view.subviews.flatMap(buttons)
    }
    let controls = buttons(panel.window!.contentView!)
    controls.first(where: { $0.title == "Copy report" })!.performClick(nil)
    let copied = clipboard.string(forType: .string) == "fixture diagnostic report"
    clipboard.clearContents()
    controls.first(where: { $0.title == "Email maintainer" })!.performClick(nil)
    output(["copied": copied, "emailCopied": clipboard.string(forType: .string) == "fixture diagnostic report",
            "opened": opened, "buttons": controls.map { $0.title }])
    panel.close()
default: fatalError("Unknown harness mode")
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "Requires macOS Swift")
class DiagnosticReportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="lighttable-diagnostics-build-")
        cls.addClassCleanup(cls.build.cleanup)
        main = Path(cls.build.name) / "main.swift"
        main.write_text(HARNESS)
        cls.executable = Path(cls.build.name) / "diagnostics-test"
        subprocess.run(["swiftc", "-swift-version", "5", "-module-cache-path",
                        "/tmp/lighttable-crash-report-swift-cache", str(main),
                        str(ROOT / "app/DiagnosticReports.swift"), "-o", str(cls.executable)],
                       check=True, capture_output=True, text=True, timeout=120)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lighttable-diagnostics-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_mode(self, mode, expected=0):
        result = subprocess.run([str(self.executable), mode, str(self.root)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_clean_quit_and_first_launch_do_not_prompt(self):
        self.run_mode("quit")
        self.assertEqual(self.run_mode("inspect")["count"], 0)

    def test_unclean_native_exit_survives_restart_and_prompts_once(self):
        self.run_mode("crash", expected=2)
        incident = self.run_mode("inspect")
        self.assertEqual(incident["count"], 1)
        self.assertTrue(incident["pending"])
        self.assertIn("Component: native app", incident["report"])
        self.assertIn("No error trace was available", incident["report"])
        self.run_mode("acknowledge")
        after = self.run_mode("inspect")
        self.assertEqual(after["count"], 1)
        self.assertFalse(after["pending"])

    def test_live_second_instance_is_not_reported_as_a_crash(self):
        child = subprocess.Popen([str(self.executable), "hold", str(self.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(self.run_mode("inspect")["count"], 0)
        finally:
            child.terminate()
            child.communicate(timeout=10)
        self.assertEqual(self.run_mode("inspect")["count"], 1)

    def test_engine_report_preserves_trace_before_log_rotation_and_deduplicates(self):
        (self.root / "fault.log").write_text('Fatal Python error: Segmentation fault\n  File "/private/runtime/server.py", line 2975, in decode\n')
        (self.root / "server.log").write_text("Preview failed: '/Users/Test/Private portrait.NEF'\n")
        (self.root / "inflight.json").write_text(json.dumps({"stage": "decode", "name": "Private portrait.NEF"}))
        report = self.run_mode("engine")
        self.assertEqual(report["count"], 1)
        self.assertIn("Segmentation fault", report["report"])
        self.assertIn("server.py, line 2975", report["report"])
        self.assertIn("Last recorded operation: decode", report["report"])
        self.assertNotIn("Private portrait", report["report"])
        (self.root / "server.log").write_text("new launch")
        self.assertIn("Segmentation fault", self.run_mode("inspect")["report"])

    def test_reports_are_bounded(self):
        self.assertEqual(self.run_mode("retention")["count"], 10)

    @unittest.skipUnless(os.environ.get("LIGHTTABLE_NATIVE_UI_TESTS") == "1",
                         "Requires access to the macOS pasteboard service")
    def test_report_buttons_copy_and_prepare_email_without_sending(self):
        result = self.run_mode("buttons")
        self.assertTrue(result["copied"])
        self.assertTrue(result["emailCopied"])
        self.assertTrue(result["opened"].startswith("mailto:team@lighttable.app?"))
        self.assertIn("Dismiss", result["buttons"])

    def test_private_values_are_filtered_and_mail_is_fixed(self):
        (self.root / "server.log").write_text("""Error /Users/Jane Doe/Pictures/Secret Party.NEF
token=not-a-real-token
Authorization: Bearer fixture-credential
Error C:\\Users\\Jane Doe\\Secret Party.jpg
GET /api/render?name=Secret%20Party.NEF
Contact jane@example.test
Error: 'Private keyword'
    secret = 'source-code-fixture'
  File "/private/runtime/server.py", line 12, in render
""")
        result = self.run_mode("redact")
        for private in ["Jane", "Secret", "not-a-real-token", "fixture-credential",
                        "jane@example", "Private keyword", "source-code-fixture", "/private"]:
            self.assertNotIn(private, result["report"])
        self.assertIn("server.py, line 12", result["report"])
        self.assertTrue(result["mailto"].startswith("mailto:team@lighttable.app?"))
        self.assertNotIn("fixture", result["mailto"])

    def test_macos_report_matches_process_and_excludes_private_metadata(self):
        payload = {"pid": 42, "procPath": "/engine/python", "crashReporterKey": "private-device-id",
                   "exception": {"type": "EXC_BAD_ACCESS"}, "faultingThread": 0,
                   "threads": [{"queue": "private-queue", "frames": [{"symbol": "decode_raw", "imageIndex": 0}]}],
                   "usedImages": [{"name": "python", "uuid": "binary-fixture-uuid", "path": "/Users/Private/python"}]}
        path = self.root / "Python.ips"
        path.write_text('{}\n' + json.dumps(payload))
        report = self.run_mode("mac")["report"]
        self.assertIn("decode_raw", report)
        self.assertIn("binary-fixture-uuid", report)
        for private in ["private-device-id", "private-queue", "/Users"]:
            self.assertNotIn(private, report)
        payload["procPath"] = "/another/python"
        path.write_text('{}\n' + json.dumps(payload))
        self.assertEqual(self.run_mode("mac")["report"], "")
        payload["procPath"] = "/engine/python"
        payload["pid"] = 43
        path.write_text('{}\n' + json.dumps(payload))
        self.assertEqual(self.run_mode("mac")["report"], "")


class FatalDiagnosticsTests(unittest.TestCase):
    def test_fatal_child_writes_trace_directly_even_when_stderr_is_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fault.log"
            environment = {**os.environ, "LIGHTTABLE_FAULT_LOG": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-c", "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); import fatal_diagnostics; fatal_diagnostics.install(); import os; os.abort()"],
                                    cwd=ROOT, env=environment, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Fatal Python error: Aborted", path.read_text())

    def test_unwritable_fault_destination_does_not_break_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "not-a-directory"
            file.write_text("fixture")
            subprocess.run([sys.executable, "-c", "import fatal_diagnostics; fatal_diagnostics.install()"],
                           cwd=ROOT, env={**os.environ, "LIGHTTABLE_FAULT_LOG": str(file / "fault.log")},
                           check=True, capture_output=True, timeout=20)
