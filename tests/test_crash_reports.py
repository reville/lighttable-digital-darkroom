# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in crash reports: allowlisted content, consent, outbox and delivery."""
from __future__ import annotations

import http.client
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import crash_reports

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_789_000_000.0
IDENTITY = {"version": "0.7.9", "build": "12", "revision": "b" * 40,
            "modified": False, "packaging": "macos-app"}
SYSTEM = {"platform": "macos", "osVersion": "26.6.2", "osBuild": "25G83",
          "distribution": None, "arch": "arm64", "model": "Mac16,10",
          "memoryGB": 16, "cpuCount": 10}
# Strings that must never leave the computer, planted in every source.
PRIVATE = ("alice", "Pictures", "IMG_1234", "SECRET-KEY", "holiday", "/Users/", "private.jpg")


def fault_trace(app: Path, lib: Path, site: Path) -> str:
    return f"""Fatal Python error: Segmentation fault

Thread 0x000000017b463000 (most recent call first):
  File "{app}/platform_image.py", line 287 in metadata
  File "{app}/server.py", line 2992 in exif_for
  File "/Users/alice/Pictures/holiday/export.py", line 3 in <module>
  File "{lib}/socketserver.py", line 766 in __init__
  File "{app}/server.py", line 435 in handle; rm -rf IMG_1234
  <invalid frame>

Current thread 0x0000000176f47000 (most recent call first):
  File "{site}/numba/core/dispatcher.py", line 10 in _compile_for_args
  File "<frozen importlib._bootstrap>", line 1360 in _find_and_load
  File "{app}/server.py", line 7665 in do_GET@alice

Extension modules: numpy._core._multiarray_umath, exiv2._image, bad name!, lensfunpy._lensfun (total: 4)
"""


def ips_text(pid: int = 4242, proc: str = "python3.13", bug_type: str = "309") -> str:
    payload = {
        "pid": pid, "procName": proc, "userID": 501, "crashReporterKey": "SECRET-KEY-123",
        "procPath": f"/Users/alice/Applications/LightTable.app/Contents/Resources/Python/bin/{proc}",
        "parentProc": "LightTable", "coalitionName": "app.lighttable.LightTable",
        "exception": {"codes": "0x0000000000000001, 0x0000000000000010", "type": "EXC_BAD_ACCESS",
                      "signal": "SIGSEGV", "subtype": "KERN_INVALID_ADDRESS at 0x0000000000000010"},
        "termination": {"code": 11, "namespace": "SIGNAL", "indicator": "Segmentation fault: 11",
                        "byProc": "exc handler"},
        "asi": {"libsystem_c.dylib": ["/Users/alice/Pictures/private.jpg"]},
        "faultingThread": 1,
        "threads": [
            {"id": 1, "queue": "com.apple.main-thread",
             "frames": [{"imageOffset": 10, "symbol": "main", "imageIndex": 0}]},
            {"id": 2, "triggered": True, "name": "/Users/alice/holiday", "frames": [
                {"imageOffset": 123456, "imageIndex": 1, "symbolLocation": 40,
                 "symbol": "Exiv2::XmpParser::initialize(void (*)(void*, bool), void*)"},
                {"imageOffset": 99, "imageIndex": 1, "symbol": "see @alice #12"},
                {"imageOffset": 77, "imageIndex": 0},
            ]},
        ],
        "usedImages": [
            {"arch": "arm64", "base": 4294967296, "uuid": "11111111-2222-3333-4444-555555555555",
             "path": f"/Users/alice/Applications/{proc}", "name": proc},
            {"arch": "arm64", "base": 4295000000, "uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
             "path": "/Users/alice/lib/libexiv2.28.dylib", "name": "libexiv2.28.dylib"},
            {"arch": "arm64", "name": "unreferenced.dylib", "uuid": "AAAAAAAA-BBBB-CCCC-DDDD-000000000000"},
        ],
    }
    header = {"app_name": proc, "bug_type": bug_type, "name": proc, "incident_id": "SECRET-KEY-9"}
    return json.dumps(header) + "\n" + json.dumps(payload)


def assert_private(test: unittest.TestCase, payload) -> None:
    text = json.dumps(payload)
    for secret in PRIVATE:
        test.assertNotIn(secret, text)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def read(self, _limit=-1):
        return b'{"ok":true}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if result >= 300:
            raise HTTPError(request.full_url, result, "status", {}, None)
        return FakeResponse(result)


class FaultTraceTests(unittest.TestCase):
    def setUp(self):
        self.app, self.lib, self.site = (Path("/opt/lt/Resources/LightTable"),
                                         Path("/opt/lt/Python/lib/python3.13"),
                                         Path("/opt/lt/Python/lib/python3.13/site-packages"))
        self.roots = sorted([("app", self.app), ("lib", self.lib), ("site", self.site)],
                            key=lambda item: -len(str(item[1])))

    def test_keeps_code_locations_and_drops_everything_else(self):
        trace = crash_reports.parse_fault_trace(fault_trace(self.app, self.lib, self.site), self.roots)
        self.assertEqual(trace["fatalError"], "Segmentation fault")
        first, current = trace["threads"]
        self.assertFalse(first["current"])
        self.assertTrue(current["current"])
        self.assertEqual(first["frames"], [
            {"file": "app:platform_image.py", "line": 287, "function": "metadata"},
            {"file": "app:server.py", "line": 2992, "function": "exif_for"},
            {"file": "<other>", "line": 3, "function": "<module>"},
            {"file": "lib:socketserver.py", "line": 766, "function": "__init__"},
            {"marker": "invalid-frame"},
        ])
        self.assertEqual(current["frames"], [
            {"file": "site:numba/core/dispatcher.py", "line": 10, "function": "_compile_for_args"},
            {"file": "frozen:importlib._bootstrap", "line": 1360, "function": "_find_and_load"},
            {"file": "app:server.py", "line": 7665, "function": "<other>"},
        ])
        self.assertEqual(trace["extensionModules"],
                         ["numpy._core._multiarray_umath", "exiv2._image", "lensfunpy._lensfun"])
        assert_private(self, trace)

    def test_reads_the_file_names_the_macos_shell_already_reduced(self):
        text = ("Thread 0x000000017b463000 (most recent call first):\n"
                "  File platform_image.py, line 287 in metadata\n"
                "  File [quoted value], line 1360 in _find_and_load\n"
                "  File [path], line 9 in run\n")
        trace = crash_reports.parse_fault_trace(text, self.roots)
        self.assertEqual(trace["threads"][0]["frames"], [
            {"file": "code:platform_image.py", "line": 287, "function": "metadata"}])

    def test_windows_fatal_exceptions_and_hostile_messages(self):
        trace = crash_reports.parse_fault_trace(
            "Windows fatal exception: access violation\n\nStack (most recent call first):\n"
            f'  File "{self.app}/grade.py", line 5 in apply\n', self.roots)
        self.assertEqual(trace["fatalError"], "access violation")
        self.assertEqual(trace["threads"][0]["frames"][0]["file"], "app:grade.py")
        hostile = crash_reports.parse_fault_trace(
            "Fatal Python error: could not open /Users/alice/Pictures/private.jpg\n", self.roots)
        self.assertEqual(hostile["fatalError"], "<other>")

    def test_bounds_threads_and_frames(self):
        frame = f'  File "{self.app}/server.py", line 1 in run\n'
        text = "".join(f"Thread 0x{index:x} (most recent call first):\n" + frame * 200
                       for index in range(100))
        trace = crash_reports.parse_fault_trace(text, self.roots)
        self.assertLessEqual(len(trace["threads"]), crash_reports.MAX_THREADS)
        self.assertTrue(all(len(item["frames"]) <= crash_reports.MAX_FRAMES
                            for item in trace["threads"]))

    def test_code_roots_name_this_interpreter_s_own_files(self):
        import numpy
        import threading

        roots = crash_reports.code_roots(ROOT)
        lengths = [len(str(path)) for _, path in roots]
        self.assertEqual(lengths, sorted(lengths, reverse=True))
        self.assertIn(("app", ROOT), roots)
        # A symlinked interpreter or virtual environment must still match.
        self.assertEqual(crash_reports._frame_file(threading.__file__, roots), "lib:threading.py")
        self.assertEqual(crash_reports._frame_file(numpy.__file__, roots), "site:numpy/__init__.py")
        self.assertEqual(crash_reports._frame_file(str(ROOT / "server.py"), roots), "app:server.py")


class NativeCrashTests(unittest.TestCase):
    def test_summary_keeps_symbols_and_image_identity_only(self):
        header, _, body = ips_text().partition("\n")
        summary = crash_reports.native_summary(json.loads(body))
        self.assertEqual(summary["exceptionType"], "EXC_BAD_ACCESS")
        self.assertEqual(summary["exceptionSubtype"], "KERN_INVALID_ADDRESS at 0x0000000000000010")
        self.assertEqual(summary["termination"], "Segmentation fault: 11")
        self.assertEqual(summary["frames"], [
            {"image": "libexiv2.28.dylib", "offset": 123456,
             "symbol": "Exiv2::XmpParser::initialize(void (*)(void*, bool), void*)"},
            {"image": "libexiv2.28.dylib", "offset": 99, "symbol": None},
            {"image": "python3.13", "offset": 77, "symbol": None},
        ])
        self.assertEqual([image["name"] for image in summary["images"]],
                         ["libexiv2.28.dylib", "python3.13"])
        assert_private(self, summary)

    def test_matches_only_this_process_crash_in_its_launch_window(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            files = {
                "match.ips": ips_text(),
                "other-pid.ips": ips_text(pid=77),
                "other-name.ips": ips_text(proc="Safari"),
                "hang.ips": ips_text(bug_type="288"),
                "notes.txt": ips_text(),
            }
            for name, text in files.items():
                path = directory / name
                path.write_text(text)
                os.utime(path, (NOW, NOW))
            found = crash_reports.find_mac_crash(4242, {"python3.13"}, NOW - 5, NOW, directory)
            self.assertEqual(found["exceptionType"], "EXC_BAD_ACCESS")
            (directory / "match.ips").unlink()
            self.assertIsNone(crash_reports.find_mac_crash(4242, {"python3.13"}, NOW - 5, NOW, directory))
            os.utime(directory / "other-pid.ips", (NOW - 3600, NOW - 3600))
            self.assertIsNone(crash_reports.find_mac_crash(77, {"python3.13"}, NOW - 5, NOW, directory))
            self.assertIsNone(crash_reports.find_mac_crash(1, {"x"}, NOW, NOW, directory / "missing"))


class BuildIdentityTests(unittest.TestCase):
    def test_macos_bundle_reads_its_info_plist(self):
        with tempfile.TemporaryDirectory() as folder:
            contents = Path(folder) / "LightTable.app/Contents"
            app = contents / "Resources/LightTable"
            app.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "0.7.9", "CFBundleVersion": "202609171200",
                "LightTableSourceRevision": "a" * 40, "LightTableSourceDirty": False}))
            self.assertEqual(crash_reports.build_identity(app), {
                "version": "0.7.9", "build": "202609171200", "revision": "a" * 40,
                "modified": False, "packaging": "macos-app"})

    def test_portable_bundle_reads_its_manifest_and_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "lighttable"
            app = bundle / "Resources/LightTable"
            app.mkdir(parents=True)
            (bundle / "build-manifest.json").write_text(json.dumps({
                "version": "0.7.9", "source_revision": "c" * 40, "source_dirty": False,
                "private": "/home/alice"}))
            (bundle / "installation-owner.json").write_text('{"owner": "deb"}')
            identity = crash_reports.build_identity(app)
            self.assertEqual(identity["version"], "0.7.9")
            self.assertEqual(identity["revision"], "c" * 40)
            self.assertEqual(identity["packaging"], "deb")
            assert_private(self, identity)

    def test_a_source_checkout_is_not_a_release(self):
        self.assertIsNone(crash_reports.build_identity(ROOT))

    def test_system_facts_are_short_allowlisted_values(self):
        facts = crash_reports.system_facts()
        self.assertEqual(set(facts), set(SYSTEM))
        self.assertIn(facts["platform"], {"macos", "windows", "linux"})
        for value in facts.values():
            self.assertTrue(value is None or isinstance(value, (int, str)))
            self.assertLessEqual(len(str(value)), 40)


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.faults = self.base / "Diagnostics"
        self.faults.mkdir()
        self.prefs = {}
        self.now = [NOW]
        self.opener = FakeOpener()
        self.environ = {}

    def reporter(self, **overrides):
        options = dict(root=self.base / "Diagnostics/crash-reports", app_dir=ROOT,
                       prefs=lambda: self.prefs, fault_logs=self.faults,
                       own_fault=self.faults / "server-fault-9999.log", environ=self.environ,
                       identity=IDENTITY, system=SYSTEM, opener=self.opener,
                       clock=lambda: self.now[0])
        options.update(overrides)
        return crash_reports.CrashReporter(**options)

    def crashed(self, pid=4242, text=None):
        path = self.faults / f"server-fault-{pid}.log"
        path.write_text(fault_trace(ROOT, Path("/lib"), Path("/site")) if text is None else text)
        os.utime(path, (NOW - 10, NOW - 10))
        return {"startedAt": NOW - 30, "pid": pid, "revision": None, "exitStatus": None,
                "detectedAt": NOW, "inflight": {"stage": "decode", "name": "IMG_1234.CR3"}}

    def pending(self, reporter):
        return [json.loads(path.read_text()) for path in reporter._pending_files()]

    def test_a_crash_waits_for_consent_and_is_sent_once_after_it(self):
        reporter = self.reporter()
        self.assertEqual(reporter.status()["consent"], None)
        self.assertEqual(reporter.collect(self.crashed()), 1)
        record, = self.pending(reporter)
        payload = record["payload"]
        self.assertEqual(payload["crash"]["component"], "engine")
        self.assertEqual(payload["crash"]["operation"], "decode")
        self.assertEqual(payload["crash"]["uptimeSeconds"], 30)
        self.assertEqual(payload["crash"]["detectedOn"], "2026-09-10")
        self.assertEqual(payload["crash"]["threads"][0]["frames"][0],
                         {"file": "app:platform_image.py", "line": 287, "function": "metadata"})
        self.assertEqual(crash_reports.validate(payload), [])
        assert_private(self, payload)
        # The crashed run's file is consumed; this server's own file is not.
        own = self.faults / "server-fault-9999.log"
        own.write_text("")
        (self.faults / "server-fault-1.log").write_text("")
        reporter.collect(self.crashed(pid=4242))
        self.assertEqual(len(self.pending(reporter)), 1, "the ledger prevents a duplicate")
        self.assertFalse((self.faults / "server-fault-4242.log").exists())
        self.assertFalse((self.faults / "server-fault-1.log").exists())
        self.assertTrue(own.exists())

        self.assertEqual(reporter.send_pending()["sent"], 0)
        self.assertEqual(self.opener.requests, [])
        self.prefs["crashReports"] = True
        self.opener.results = [202]
        self.assertEqual(reporter.send_pending(), {"sent": 1, "dropped": 0, "deferred": 0})
        request, = self.opener.requests
        self.assertEqual(request.full_url, crash_reports.DEFAULT_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.headers["User-agent"], "LightTable/0.7.9 (macos)")
        self.assertEqual(self.pending(reporter), [])
        self.assertEqual(reporter.status()["lastSentAt"], NOW)

    def test_declining_discards_pending_reports_and_future_crashes(self):
        reporter = self.reporter()
        reporter.collect(self.crashed())
        self.assertEqual(len(self.pending(reporter)), 1)
        reporter.preference_changed(False)
        self.assertEqual(self.pending(reporter), [])
        self.prefs["crashReports"] = False
        self.assertEqual(reporter.collect(self.crashed(pid=5)), 0)
        self.assertEqual(self.pending(reporter), [])
        self.prefs["crashReports"] = True
        self.assertEqual(reporter.collect(self.crashed(pid=5)), 0, "a declined crash stays unsent")

    def test_a_forced_quit_without_a_fatal_stack_is_not_a_crash_report(self):
        reporter = self.reporter()
        self.assertEqual(reporter.collect(self.crashed(text="")), 0)
        stale = self.crashed(pid=6)
        os.utime(self.faults / "server-fault-6.log", (NOW - 7200, NOW - 7200))
        self.assertEqual(reporter.collect(stale), 0, "a reused process ID is not this run")

    def test_only_packaged_builds_participate_unless_forced(self):
        development = self.reporter(identity=None, app_dir=ROOT)
        self.assertFalse(development.available())
        (self.faults / "server-fault-3.log").write_text("")
        development.start(None)
        self.assertFalse((self.faults / "server-fault-3.log").exists())
        self.environ["LIGHTTABLE_CRASH_REPORTS"] = "0"
        self.assertFalse(self.reporter().available())
        self.environ["LIGHTTABLE_CRASH_REPORTS"] = "1"
        forced = self.reporter(identity=None, app_dir=ROOT)
        self.assertTrue(forced.available())
        self.assertEqual(forced.identity()["packaging"], "development")

    def test_failures_back_off_and_bad_or_old_reports_are_dropped(self):
        self.prefs["crashReports"] = True
        reporter = self.reporter()
        reporter.collect(self.crashed())
        self.opener.results = [URLError("offline")]
        self.assertEqual(reporter.send_pending()["deferred"], 1)
        record, = self.pending(reporter)
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["nextAttemptAt"], NOW + 3600)
        self.assertEqual(reporter.status()["lastError"], "URLError")
        self.assertEqual(reporter.send_pending()["deferred"], 1)
        self.assertEqual(len(self.opener.requests), 1, "not before the retry time")
        self.now[0] += 3601
        self.opener.results = [503]
        reporter.send_pending()
        self.assertEqual(self.pending(reporter)[0]["nextAttemptAt"], NOW + 3601 + 6 * 3600)
        self.now[0] += 7 * 3600
        self.opener.results = [422]
        self.assertEqual(reporter.send_pending()["dropped"], 1)
        self.assertEqual(self.pending(reporter), [])
        reporter.collect(self.crashed(pid=8))
        self.now[0] += crash_reports.MAX_AGE_SECONDS + 1
        self.assertEqual(reporter.send_pending()["dropped"], 1)

    def test_only_https_or_a_local_test_endpoint_is_used(self):
        self.prefs["crashReports"] = True
        self.environ["LIGHTTABLE_CRASH_REPORT_URL"] = "http://reports.example/v1/crash"
        reporter = self.reporter()
        reporter.collect(self.crashed())
        self.assertEqual(reporter.send_pending()["sent"], 0)
        self.assertEqual(self.opener.requests, [])

    def test_the_outbox_is_bounded(self):
        reporter = self.reporter()
        for pid in range(10, 10 + crash_reports.MAX_PENDING + 5):
            reporter.collect(self.crashed(pid=pid))
        self.assertEqual(len(self.pending(reporter)), crash_reports.MAX_PENDING)

    def native_incident(self, **fields):
        started = NOW - 40 - crash_reports.SWIFT_REFERENCE_EPOCH
        record = {"id": "engine-ABC", "kind": "engine", "startedAt": started,
                  "detectedAt": started + 38, "pid": 4242,
                  "executable": "/Users/alice/Applications/LightTable.app/Contents/Resources/Python/bin/python3.13",
                  "report": "LightTable diagnostic report ... IMG_1234.CR3 /Users/alice/Pictures",
                  "presented": True, "reportVersion": 1,
                  "errorTrace": "Current thread 0x1 (most recent call first):\n"
                                "  File platform_image.py, line 287 in metadata\n",
                  "exitStatus": 11, "exitReason": "signal", "operation": "render",
                  "appVersion": "0.7.6", "appBuild": "12", "sourceRevision": "d" * 40,
                  "sourceModified": False}
        record.update(fields)
        return record

    def test_macos_incidents_become_reports_with_their_own_build(self):
        native = self.base / "Native"
        native.mkdir()
        (native / "incident-engine-ABC.json").write_text(json.dumps(self.native_incident()))
        (native / "incident-old-format.json").write_text(json.dumps(
            {**self.native_incident(id="old"), "reportVersion": None}))
        (native / "incident-app-OLD.json").write_text(json.dumps(self.native_incident(
            id="app-OLD", kind="app",
            detectedAt=NOW - crash_reports.MAX_AGE_SECONDS - 60 - crash_reports.SWIFT_REFERENCE_EPOCH)))
        self.environ["LIGHTTABLE_DIAGNOSTICS_DIR"] = str(native)
        reporter = self.reporter(fault_logs=None)
        with mock.patch.object(crash_reports, "find_mac_crash", return_value={"frames": []}) as find:
            self.assertEqual(reporter.collect(self.crashed()), 1, "the ledger crash is the same exit")
        record, = self.pending(reporter)
        crash, app = record["payload"]["crash"], record["payload"]["app"]
        self.assertEqual((crash["exitStatus"], crash["signal"], crash["operation"]), (11, "SIGSEGV", "render"))
        self.assertEqual(crash["uptimeSeconds"], 38)
        self.assertEqual(crash["native"], {"frames": []})
        self.assertEqual(crash["threads"][0]["frames"][0]["file"], "code:platform_image.py")
        self.assertEqual((app["version"], app["revision"]), ("0.7.6", "d" * 40))
        assert_private(self, record["payload"])
        if sys.platform == "darwin":
            pid, names, started, detected, directory = find.call_args.args
            self.assertEqual(pid, 4242)
            self.assertIn("python3.13", names)
            self.assertEqual((started, detected), (NOW - 40, NOW - 2))
        self.assertEqual(reporter.collect(None), 0)

    def test_an_exit_status_alone_is_not_named_as_a_signal(self):
        payload = crash_reports.assemble(IDENTITY, SYSTEM, component="engine", detected=NOW,
                                         started=None, exit_status=11, exit_reason="exit",
                                         operation="export", trace={}, native=None)
        self.assertIsNone(payload["crash"]["signal"])
        self.assertEqual(crash_reports.validate(payload), [])
        payload["crash"]["exitStatus"] = None
        self.assertEqual(crash_reports.validate(payload), ["crash.evidence"])

    def test_background_start_collects_and_sends_without_blocking(self):
        self.prefs["crashReports"] = True
        self.opener.results = [202]
        reporter = self.reporter()
        reporter.start(self.crashed())
        deadline = time.monotonic() + 5
        while (reporter._worker is not None or reporter._pending_files()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.pending(reporter), [])


class RelaySchemaTests(unittest.TestCase):
    """The relay must accept exactly what the app builds (services/crash-reports)."""

    @unittest.skipUnless(shutil.which("node"), "node is required for the relay validator")
    def test_the_relay_accepts_a_report_the_app_built(self):
        header, _, body = ips_text().partition("\n")
        roots = [("app", ROOT), ("lib", Path("/lib"))]
        payload = crash_reports.assemble(
            IDENTITY, crash_reports.system_facts(), component="engine", detected=NOW,
            started=NOW - 12, exit_status=11, exit_reason="signal", operation="decode",
            trace=crash_reports.parse_fault_trace(fault_trace(ROOT, Path("/lib"), Path("/site")), roots),
            native=crash_reports.native_summary(json.loads(body)))
        self.assertEqual(crash_reports.validate(payload), [])
        script = """
import { validateReport } from '%s';
const problems = validateReport(JSON.parse(process.argv[1]));
console.log(JSON.stringify(problems));
""" % (ROOT / "services/crash-reports/src/report.js")
        result = subprocess.run(["node", "--input-type=module", "-e", script, "--",
                                 json.dumps(payload)],
                                capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(json.loads(result.stdout), [], result.stderr)


class FaultFileTests(unittest.TestCase):
    def run_child(self, code: str, catalog: Path) -> int:
        environment = {key: value for key, value in os.environ.items()
                       if key != "LIGHTTABLE_FAULT_LOG"}
        environment.update(LIGHTTABLE_CATALOG_FILE=str(catalog), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode

    @unittest.skipIf(sys.platform == "win32", "uses POSIX abort without a core file")
    def test_a_server_without_a_host_file_keeps_its_own_until_a_clean_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / "Catalog/library.sqlite3"
            diagnostics = catalog.parent / "Diagnostics"
            self.assertEqual(self.run_child(
                "import fatal_diagnostics; fatal_diagnostics.install()", catalog), 0)
            self.assertEqual(list(diagnostics.glob("server-fault-*.log")), [])
            code = ("import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
                    "import fatal_diagnostics, os; fatal_diagnostics.install(); "
                    "print(fatal_diagnostics.owned_path()); os.abort()")
            self.assertNotEqual(self.run_child(code, catalog), 0)
            left, = diagnostics.glob("server-fault-*.log")
            self.assertIn("Fatal Python error: Aborted", left.read_text())
            code = ("import fatal_diagnostics, os; fatal_diagnostics.install(); "
                    "fatal_diagnostics.release(); os._exit(0)")
            self.assertEqual(self.run_child(code, catalog), 0)
            self.assertEqual(list(diagnostics.glob("server-fault-*.log")), [left])


class CrashReportRouteTests(unittest.TestCase):
    def setUp(self):
        import server

        self.server = server
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.reporter = mock.Mock()
        self.reporter.status.return_value = {"available": True, "consent": None, "pending": 0,
                                             "lastSentAt": None, "lastError": None}
        for patcher in (mock.patch.object(server, "CRASH_REPORTS", self.reporter),
                        mock.patch.object(server, "PREFS_FILE", Path(self.temporary.name) / "prefs.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = server.LightTableServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.http.shutdown(), thread.join(3), self.http.server_close()))

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {"Content-Type": "application/json",
                   "X-LightTable-Token": self.server.INSTANCE_TOKEN}
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_status_and_consent_changes_reach_the_reporter(self):
        self.assertEqual(self.request("GET", "/api/crash-reports"),
                         (200, self.reporter.status.return_value))
        self.assertEqual(self.request("POST", "/api/prefs", {"crashReports": True}),
                         (200, {"ok": True}))
        self.reporter.preference_changed.assert_called_once_with(True)
        self.assertEqual(json.loads(self.server.PREFS_FILE.read_text())["crashReports"], True)
        self.request("POST", "/api/prefs", {"autoAdvance": False})
        self.reporter.preference_changed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
