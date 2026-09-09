"""Safety and evidence checks for the Windows native acceptance runner."""
from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("windows_desktop_smoke", ROOT / "scripts/windows/desktop-smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class DesktopSmokeSafetyTests(unittest.TestCase):
    def test_environment_removes_inherited_overrides_and_isolates_native_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = smoke.smoke_environment(root, {
                "LIGHTTABLE_DIR": "/personal/photos", "lighttable_catalog_file": "/personal/catalog",
                "LIGHTTABLE_HEADLESS": "1", "LIGHTTABLE_SAFE_MODE": "1",
                "LIGHTTABLE_PROJECT_DIR": "/unpackaged/source", "PYTHONPATH": "/developer/modules",
                "WEBVIEW2_USER_DATA_FOLDER": "/personal/browser", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=9222",
                "Path": "system binaries", "LOCALAPPDATA": "/actual/windows/known-folder",
            })
            for removed in ("LIGHTTABLE_HEADLESS", "LIGHTTABLE_SAFE_MODE", "LIGHTTABLE_PROJECT_DIR",
                            "lighttable_catalog_file", "PYTHONPATH", "WEBVIEW2_USER_DATA_FOLDER",
                            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"):
                self.assertNotIn(removed, environment)
            self.assertEqual(environment["LOCALAPPDATA"], "/actual/windows/known-folder")
            self.assertEqual(environment["Path"], "system binaries")
            for key, value in environment.items():
                if key.startswith("LIGHTTABLE_") and key.endswith(("_DIR", "_FILE", "_ROOT", "_LOG")):
                    self.assertTrue(Path(value).is_relative_to(root), key)
            self.assertEqual(Path(environment["LIGHTTABLE_PREFS_FILE"]).parent,
                             Path(environment["LIGHTTABLE_SUPPORT_DIR"]))

    def test_native_probe_rejects_one_leaked_path_or_wrong_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, bundle = Path(temporary) / "private", Path(temporary) / "bundle"
            report = {key: str(root / key) for key in smoke.MUTABLE_NATIVE_PATHS}
            report.update(python=str(bundle / "Python/python.exe"), project=str(bundle / "Resources/LightTable"))
            smoke.validate_paths(report, root, bundle)
            for key in (*smoke.MUTABLE_NATIVE_PATHS, "python", "project"):
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    smoke.validate_paths({**report, key: str(Path(temporary) / "user-owned")}, root, bundle)
            with self.assertRaises(RuntimeError):
                smoke.validate_paths({}, root, bundle)

    def test_direct_installs_are_rejected_before_shared_updater_preferences_can_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            for relative in ("LightTable.exe", "Python/python.exe", "Resources/LightTable/server.py"):
                path = bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            smoke.validate_bundle(bundle)
            for channel in ("direct", "winget", "scoop", "chocolatey"):
                (bundle / "install-channel.txt").write_text(channel)
                with self.subTest(channel=channel), self.assertRaisesRegex(RuntimeError, "portable ZIP"):
                    smoke.validate_bundle(bundle)
            (bundle / "install-channel.txt").write_text("\ufeffportable\n", encoding="utf-8")
            smoke.validate_bundle(bundle)

    def test_timeout_does_not_resubmit_a_delivered_native_command(self):
        api = Mock()
        api.request.side_effect = HTTPError("http://127.0.0.1", 504, "UI timed out", {}, None)
        smoke.send_ui_command(api, "goto", {"name": "smoke.tif"})
        self.assertEqual(api.request.call_count, 1)
        api.request.side_effect = HTTPError("http://127.0.0.1", 403, "Disabled", {}, None)
        with self.assertRaises(HTTPError):
            smoke.send_ui_command(api, "goto", {"name": "smoke.tif"})

    def test_goto_is_sent_once_only_after_the_live_window_has_loaded_the_test_photo(self):
        desktop, api = Mock(), Mock()
        desktop.running.return_value = True
        empty = {"client": "native", "age": 0, "visibleCount": 0}
        visible = {**empty, "visibleCount": 1}
        rendered = {**visible, "current": "smoke.tif", "render": {"name": "smoke.tif", "state": "ready"}}
        states = iter((empty, empty, {**visible, "age": 11}, {**visible, "client": None}, visible, rendered))
        observed, commands = [], []
        def request(route, body=None):
            if route == "/api/ui/state":
                observed.append(next(states))
                return observed[-1]
            if route == "/api/images":
                return {"images": [{"name": "smoke.tif"}]}
            self.assertEqual(route, "/api/ui/command")
            self.assertEqual(observed[-1], visible)
            commands.append(body)
            return {"ok": True}
        api.request.side_effect = request
        with patch.object(smoke.time, "sleep"):
            name, state = smoke.render_photo(desktop, api, time.monotonic() + 5, "smoke.tif")
        self.assertEqual(name, "smoke.tif")
        self.assertEqual(state, rendered)
        self.assertEqual(commands, [{"command": "goto", "args": {"name": "smoke.tif"}, "timeout": 3}])

    def test_http_errors_keep_status_and_append_only_a_bounded_json_error_string(self):
        examples = (
            (json.dumps({"error": "Photo is not loaded", "token": "SECRET_TOKEN",
                         "other": {"error": "SECRET_NESTED"}}).encode(), "Photo is not loaded"),
            (json.dumps({"error": "x" * 500 + "SECRET_TRUNCATED"}).encode(), "x" * 500),
            (json.dumps({"error": {"token": "SECRET_TOKEN"}}).encode(), None),
            (json.dumps({"message": "SECRET_OTHER"}).encode(), None),
            (b'<html>SECRET_RAW_BODY</html>', None),
        )
        for body, detail in examples:
            with self.subTest(detail=detail):
                api = smoke.API({"port": 12345, "token": "SECRET_REQUEST_TOKEN"}, time.monotonic() + 5)
                response = io.BytesIO(body)
                error = HTTPError("http://127.0.0.1:12345/api/ui/command", 409, "Conflict", {}, response)
                api.opener = Mock()
                api.opener.open.side_effect = error
                with self.assertRaises(HTTPError) as raised:
                    api.request("/api/ui/command", {"command": "goto"})
                self.assertIs(raised.exception, error)
                self.assertEqual(error.code, 409)
                self.assertEqual(error.msg, "Conflict" + (f": {detail}" if detail else ""))
                self.assertNotIn("SECRET", str(error))
                self.assertTrue(response.closed)
                api.opener.open.assert_called_once()

    def test_normal_quit_requires_one_visible_owned_window_with_the_exact_app_title(self):
        desktop = smoke.WindowsDesktop.__new__(smoke.WindowsDesktop)
        desktop.pid, desktop.process = 123, 1234
        desktop.user, desktop.kernel = Mock(), Mock()
        desktop.kernel.WaitForSingleObject.return_value = 0
        title = "LightTable — photos"
        fixtures = {10: (123, False, title, "HiddenClass"),
                    20: (123, True, title, "MainClass"),
                    30: (999, True, title, "UnownedClass"),
                    40: (123, True, "Native helper", "HelperClass")}
        windows = dict(fixtures)
        def enumerate_windows(callback, argument):
            for window in windows:
                self.assertTrue(callback(window, argument))
            return 1
        def owner(window, output):
            ctypes.cast(output, ctypes.POINTER(smoke.wintypes.DWORD))[0] = windows[window][0]
            return 1
        desktop.user.EnumWindows.side_effect = enumerate_windows
        desktop.user.GetWindowThreadProcessId.side_effect = owner
        desktop.user.IsWindowVisible.side_effect = lambda window: windows[window][1]
        def read_text(window, output, count, field):
            output.value = windows[window][field][:count - 1]
            return len(output.value)
        desktop.user.GetWindowTextW.side_effect = lambda window, output, count: read_text(window, output, count, 2)
        desktop.user.GetClassNameW.side_effect = lambda window, output, count: read_text(window, output, count, 3)
        desktop.user.PostMessageW.return_value = 1
        with patch.object(smoke.ctypes, "WINFUNCTYPE", return_value=lambda function: function, create=True):
            desktop.quit(2)
            desktop.user.PostMessageW.assert_called_once_with(20, 0x10, 0, 0)
            desktop.kernel.WaitForSingleObject.assert_called_once_with(1234, 2000)
            self.assertEqual([call.args[0] for call in desktop.user.GetWindowTextW.call_args_list], [20, 40])
            desktop.user.GetClassNameW.assert_not_called()
            for replacement in ({key: value for key, value in fixtures.items() if key != 20},
                                {**fixtures, 50: (123, True, title, "SecondMainClass")}):
                windows = replacement
                desktop.user.PostMessageW.reset_mock()
                desktop.kernel.WaitForSingleObject.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "exactly one visible") as raised:
                    desktop.quit(2)
                self.assertIn("Native helper", str(raised.exception))
                self.assertIn("HelperClass", str(raised.exception))
                self.assertNotIn("HiddenClass", str(raised.exception))
                self.assertNotIn("UnownedClass", str(raised.exception))
                desktop.user.PostMessageW.assert_not_called()
                desktop.kernel.WaitForSingleObject.assert_not_called()
            windows = {20: fixtures[20]}
            desktop.user.PostMessageW.return_value = 0
            with patch.object(smoke.ctypes, "get_last_error", return_value=5, create=True), \
                    patch.object(smoke.ctypes, "WinError", return_value=OSError("Posting close failed"), create=True):
                with self.assertRaisesRegex(OSError, "Posting close failed"):
                    desktop.quit(2)
            desktop.kernel.WaitForSingleObject.assert_not_called()

    def test_expected_edits_wait_for_a_grade_and_require_both_saved_values(self):
        pending_or_wrong = (
            None, {}, {"rating": 4}, {"grade": None, "rating": 4},
            {"grade": [], "rating": 4}, {"grade": "pending", "rating": 4},
            {"grade": 1, "rating": 4}, {"grade": {}, "rating": 4},
            {"grade": {"exposure": 0.5}}, {"grade": {"exposure": 0.5}, "rating": None},
            {"grade": {"exposure": 0.0}, "rating": 4},
            {"grade": {"exposure": 0.5}, "rating": 3},
            {"grade": {"exposure": "0.5"}, "rating": 4},
            {"grade": {"exposure": 0.5}, "rating": "4"},
        )
        for saved in pending_or_wrong:
            with self.subTest(saved=saved):
                self.assertFalse(smoke.expected_edits_saved(saved))
        self.assertTrue(smoke.expected_edits_saved({"grade": {"exposure": 0.5}, "rating": 4}))

    def test_dead_or_timed_out_window_cannot_produce_a_success_receipt(self):
        desktop = Mock()
        desktop.running.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Native app exited"):
            smoke.wait_for(desktop, time.monotonic() + 1, "render", lambda: {"ok": True})
        desktop.running.return_value = True
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            smoke.wait_for(desktop, time.monotonic() - 1, "render", lambda: {"ok": True})

    def test_registration_waits_for_http_readiness_but_wrong_identity_is_fatal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "instances").mkdir()
            (root / "photos").mkdir()
            (root / "catalog").mkdir()
            (root / "catalog/library.sqlite3").touch()
            instance = {"pid": 123, "port": 45678, "token": "fixture", "folder": str(root / "photos")}
            (root / "instances/45678.json").write_text(json.dumps(instance))
            health = {"ok": True, "pid": 123, "catalog": str(root / "catalog/library.sqlite3"),
                      "folder": str(root / "photos"), "headless": False, "safeMode": False}
            desktop = Mock()
            desktop.running.return_value = desktop.owns_pid.return_value = True
            api = Mock()
            api.request.side_effect = [TimeoutError("serve loop has not started"), health]
            with patch.object(smoke, "API", return_value=api), patch.object(smoke.time, "sleep"):
                connected, observed = smoke.connect(desktop, root, time.monotonic() + 5)
                self.assertIs(connected, api)
                self.assertEqual(observed, health)
                self.assertEqual(api.request.call_count, 2)
                for mismatch in ({"pid": 999}, {"catalog": str(root / "other.sqlite3")},
                                 {"folder": str(root / "other-photos")}, {"headless": True},
                                 {"safeMode": True}):
                    api.reset_mock()
                    api.request.side_effect = None
                    api.request.return_value = {**health, **mismatch}
                    with self.subTest(mismatch=mismatch), self.assertRaisesRegex(RuntimeError, "identity"):
                        smoke.connect(desktop, root, time.monotonic() + 5)
                    self.assertEqual(api.request.call_count, 1, "Wrong server identity must never be retried")

    def test_existing_identity_rejects_other_or_unavailable_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected, alias, other = root / "catalog", root / "alias", root / "other"
            expected.touch()
            other.touch()
            os.link(expected, alias)
            self.assertTrue(smoke.same_existing_path(alias, expected))
            for value in (other, root / "missing", "", None, {}, "catalog"):
                with self.subTest(value=value):
                    self.assertFalse(smoke.same_existing_path(value, expected))
            expected.unlink()
            self.assertFalse(smoke.same_existing_path(alias, expected))

    def test_diagnostics_allowlist_omits_tokens_and_keeps_identity_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "instances").mkdir()
            (root / "photos").mkdir()
            (root / "catalog").touch()
            (root / "startup.json").write_text(json.dumps({"phase": "ready", "pid": 123,
                "token": "SECRET", "detail": "SECRET", "extra": {"token": "SECRET"}}))
            (root / "instances/45678.json").write_text(json.dumps({"pid": 123, "port": 45678,
                "folder": str(root / "photos"), "token": "SECRET", "models": {"token": "SECRET"}}))
            desktop = Mock()
            desktop.owns_pid.return_value = True
            result = smoke.startup_diagnostics(root, desktop)
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(result["records"][0]["phase"], "ready")
            instance = result["records"][1]
            self.assertEqual(instance["folder"], str(root / "photos"))
            self.assertTrue(instance["folder_matches"])
            self.assertTrue(instance["owned_process"])
            self.assertFalse(instance["catalog_matches"])

    def test_failure_capture_never_grabs_an_unowned_window_or_the_desktop(self):
        desktop, grab = Mock(), MagicMock()
        desktop.pid = 123
        desktop.running.return_value = True
        desktop.user.GetForegroundWindow.return_value = 456
        destination = Path("window.png")
        def owner(pid):
            def write_owner(window, output):
                self.assertEqual(window, 456)
                ctypes.cast(output, ctypes.POINTER(smoke.wintypes.DWORD))[0] = pid
                return 1
            desktop.user.GetWindowThreadProcessId.side_effect = write_owner
        with patch.object(smoke.os, "name", "nt"), patch.dict(sys.modules, {"PIL": Mock(ImageGrab=grab)}):
            owner(999)
            self.assertFalse(smoke.capture_failure_window(desktop, destination)["available"])
            grab.grab.assert_not_called()
            owner(123)
            self.assertTrue(smoke.capture_failure_window(desktop, destination)["available"])
            grab.grab.assert_called_once_with(window=456)
            grab.grab.return_value.__enter__.return_value.save.assert_called_once_with(destination)
            grab.grab.side_effect = OSError("capture unavailable")
            self.assertFalse(smoke.capture_failure_window(desktop, destination)["available"])

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows extended path resolution")
    def test_connect_accepts_normal_and_extended_spellings_of_the_same_windows_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "instances").mkdir()
            (root / "photos").mkdir()
            (root / "catalog").mkdir()
            (root / "catalog/library.sqlite3").touch()
            def extended(path):
                value = str(path)
                return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
            folder, catalog = root / "photos", root / "catalog/library.sqlite3"
            self.assertNotEqual(folder.resolve(), Path(extended(folder)).resolve())
            desktop, api = Mock(), Mock()
            desktop.running.return_value = desktop.owns_pid.return_value = True
            for report_folder, report_catalog in ((extended(folder), str(catalog)),
                                                   (str(folder), extended(catalog)),
                                                   (extended(folder), extended(catalog))):
                with self.subTest(folder=report_folder, catalog=report_catalog):
                    instance = {"pid": 123, "port": 45678, "token": "fixture", "folder": report_folder}
                    (root / "instances/45678.json").write_text(json.dumps(instance))
                    health = {"ok": True, "pid": 123, "folder": report_folder, "catalog": report_catalog,
                              "headless": False, "safeMode": False}
                    api.request.return_value = health
                    with patch.object(smoke, "API", return_value=api):
                        connected, observed = smoke.connect(desktop, root, time.monotonic() + 2)
                    self.assertIs(connected, api)
                    self.assertEqual(observed, health)


@unittest.skipUnless(importlib.util.find_spec("tifffile") and importlib.util.find_spec("numpy"),
                     "Requires the packaged TIFF/numpy dependencies")
class DesktopExportEvidenceTests(unittest.TestCase):
    def test_rgb16_icc_and_actual_precision_are_required(self):
        import numpy as np
        import tifffile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.tif"
            pixels = np.tile(np.arange(1024, dtype=np.uint16)[None, :, None], (2, 1, 3))
            profile = b"ICC fixture"
            tags = [(34675, 7, len(profile), profile, False)]
            tifffile.imwrite(path, pixels, photometric="rgb", metadata=None, extratags=tags)
            report = smoke.verify_export(path, require_precision=True)
            self.assertEqual(report["shape"], [2, 1024, 3])
            self.assertEqual(report["red_levels"], 1024)
            self.assertEqual(report["icc_bytes"], len(profile))
            self.assertEqual(len(report["sha256"]), 64)
            # Rewriting immediately also checks that verification releases handles.
            for data, extra, message in ((pixels.astype(np.uint8), tags, "RGB16"),
                                          (pixels, [], "ICC"),
                                          (pixels % 256, tags, "precision")):
                tifffile.imwrite(path, data, photometric="rgb", metadata=None, extratags=extra)
                with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                    smoke.verify_export(path, require_precision=True)


class DesktopDirectoryCleanupTests(unittest.TestCase):
    def test_permission_retry_stops_at_ten_seconds_and_other_errors_are_not_retried(self):
        temporary = Mock()
        locked = PermissionError("Private file remains locked")
        temporary.cleanup.side_effect = locked
        with patch.object(smoke.time, "monotonic", side_effect=[0, 0.1, 10]), patch.object(smoke.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as raised:
                smoke.cleanup_temporary_directory(temporary)
        self.assertIs(raised.exception, locked)
        self.assertEqual(temporary.cleanup.call_count, 2)
        sleep.assert_called_once_with(0.2)
        temporary.reset_mock()
        temporary.cleanup.side_effect = OSError("Unexpected filesystem failure")
        with self.assertRaises(OSError):
            smoke.cleanup_temporary_directory(temporary)
        temporary.cleanup.assert_called_once_with()

    def test_cleanup_failure_records_failure_and_preserves_any_original_native_exception(self):
        with tempfile.TemporaryDirectory() as output:
            for original in (None, RuntimeError("Native render timed out")):
                report_dir = Path(output) / ("native-failed" if original else "native-passed")
                report_dir.mkdir()
                report, root = {"ok": True}, None
                cleanup_error = PermissionError("Private WebView2 file remains locked")
                def fail_cleanup(temporary):
                    self.assertFalse((report_dir / "report.json").exists())
                    emit.assert_not_called()
                    raise cleanup_error
                try:
                    with patch.object(smoke, "cleanup_temporary_directory", side_effect=fail_cleanup), patch.object(smoke, "print") as emit:
                        with self.assertRaises(type(original or cleanup_error)) as raised:
                            with smoke.smoke_directory(report, report_dir) as root:
                                (root / "server.log").write_text("private log fixture")
                                if original:
                                    raise original
                        self.assertIs(raised.exception, original or cleanup_error)
                        printed = json.loads(emit.call_args.args[0])
                    saved = json.loads((report_dir / "report.json").read_text())
                    self.assertEqual(saved, printed)
                    self.assertFalse(saved["ok"])
                    self.assertEqual(saved["cleanup_error"], str(cleanup_error))
                    self.assertEqual(saved["retained_directory"], str(root))
                    self.assertEqual((report_dir / "server.log").read_text(), "private log fixture")
                    if original:
                        self.assertEqual(saved["error"], str(original))
                finally:
                    if root:
                        shutil.rmtree(root)

    def test_success_receipt_is_written_only_after_private_directory_is_removed(self):
        with tempfile.TemporaryDirectory() as output:
            report_dir = Path(output)
            with patch.object(smoke, "print") as emit:
                with smoke.smoke_directory({"ok": True}, report_dir) as root:
                    (root / "server.log").write_text("private log fixture")
                    self.assertFalse((report_dir / "report.json").exists())
                    emit.assert_not_called()
                self.assertFalse(root.exists())
                self.assertTrue(json.loads(emit.call_args.args[0])["ok"])
            self.assertTrue(json.loads((report_dir / "report.json").read_text())["ok"])
            self.assertEqual((report_dir / "server.log").read_text(), "private log fixture")

    def test_report_write_failure_cannot_mask_a_native_error_or_print_success(self):
        with tempfile.TemporaryDirectory() as output:
            for original in (None, RuntimeError("Native render timed out")):
                report_error = PermissionError("Cannot write evidence")
                with patch.object(smoke.Path, "write_text", side_effect=report_error), patch.object(smoke, "print") as emit:
                    with self.assertRaises(type(original or report_error)) as raised:
                        with smoke.smoke_directory({"ok": True}, Path(output)) as root:
                            if original:
                                raise original
                    self.assertIs(raised.exception, original or report_error)
                    self.assertFalse(root.exists())
                    printed = json.loads(emit.call_args.args[0])
                    self.assertFalse(printed["ok"])
                    self.assertEqual(printed["report_error"], str(report_error))
                    if original:
                        self.assertEqual(printed["error"], str(original))

    @unittest.skipUnless(os.name == "nt", "Requires Windows file deletion sharing semantics")
    def test_cleanup_retries_a_real_windows_held_file_until_its_handle_closes(self):
        with tempfile.TemporaryDirectory(prefix="lighttable-lock-smoke-") as directory:
            temporary = tempfile.TemporaryDirectory(dir=directory, delete=False)
            root = Path(temporary.name)
            path = root / "BrowserMetrics.pma"
            held = path.open("wb")
            release = threading.Timer(0.3, held.close)
            original_cleanup = temporary.cleanup
            def attempt_cleanup():
                try:
                    original_cleanup()
                except PermissionError:
                    if release.ident is None:
                        release.start()
                    raise
            try:
                with self.assertRaises(PermissionError):
                    path.unlink()
                with patch.object(temporary, "cleanup", side_effect=attempt_cleanup) as attempts:
                    smoke.cleanup_temporary_directory(temporary)
                    self.assertGreaterEqual(attempts.call_count, 2)
                self.assertTrue(held.closed)
                self.assertFalse(root.exists())
            finally:
                release.cancel()
                if release.ident is not None:
                    release.join(timeout=2)
                held.close()
                temporary.cleanup()


class DesktopStackDiagnosticTests(unittest.TestCase):
    def test_export_snapshot_keeps_only_counters_and_bounded_state(self):
        snapshot = smoke.export_status_snapshot({"total": 1, "done": 0, "completed": 0,
            "running": True, "cancel_requested": False, "state": "waiting", "phase": "x" * 120,
            "token": "SECRET", "log": ["SECRET"], "destination": "SECRET",
            "errors": [{"token": "SECRET"}], "warnings": [], "skipped": {"token": "SECRET"}})
        self.assertEqual(snapshot, {"total": 1, "done": 0, "completed": 0,
            "running": True, "cancel_requested": False, "state": "waiting", "phase": "x" * 100,
            "errors_count": 1, "warnings_count": 0})
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_job_pid_query_is_scoped_and_rejects_an_incomplete_list(self):
        desktop = smoke.WindowsDesktop.__new__(smoke.WindowsDesktop)
        desktop.job, desktop.kernel = 1234, Mock()
        def query(job, info_class, output, size, length):
            self.assertEqual((job, info_class), (1234, 3))
            listing = output._obj
            listing.assigned = listing.count = 3
            listing.pids[:3] = [11, 22, 11]
            return 1
        desktop.kernel.QueryInformationJobObject.side_effect = query
        self.assertEqual(desktop.job_process_ids(), [11, 22])
        def incomplete(*args):
            query(*args)
            args[2]._obj.assigned = 257
            return 1
        desktop.kernel.QueryInformationJobObject.side_effect = incomplete
        with self.assertRaisesRegex(RuntimeError, "diagnostic bound"):
            desktop.job_process_ids()

    def test_only_owned_matching_python_processes_are_held_and_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            python, other = Path(directory) / "python.exe", Path(directory) / "other.exe"
            python.touch()
            other.touch()
            desktop = smoke.WindowsDesktop.__new__(smoke.WindowsDesktop)
            desktop.job, desktop.kernel = 1234, Mock()
            desktop.job_process_ids = Mock(return_value=list(range(1, 11)))
            desktop.kernel.OpenProcess.side_effect = lambda rights, inherit, pid: pid if pid != 5 else None
            def membership(handle, job, output):
                self.assertEqual(job, desktop.job)
                ctypes.cast(output, ctypes.POINTER(smoke.wintypes.BOOL))[0] = handle != 2
                return 1
            def executable(handle, flags, output, length):
                self.assertEqual(flags, 0)
                output.value = str(other if handle == 3 else python)
                return handle != 4
            desktop.kernel.IsProcessInJob.side_effect = membership
            desktop.kernel.QueryFullProcessImageNameW.side_effect = executable
            with desktop.owned_python_pids(python) as pids:
                self.assertEqual(pids, [1, 6, 7, 8])
                self.assertEqual([call.args[0] for call in desktop.kernel.CloseHandle.call_args_list], [2, 3, 4])
            self.assertEqual([call.args[0] for call in desktop.kernel.CloseHandle.call_args_list], [2, 3, 4, 1, 6, 7, 8])
            self.assertNotIn(2, [call.args[0] for call in desktop.kernel.QueryFullProcessImageNameW.call_args_list])

    def test_absent_dumper_and_no_matching_processes_do_not_run_a_profiler(self):
        desktop = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(smoke.subprocess, "run") as run:
            root = Path(directory)
            self.assertIsNone(smoke.capture_python_stacks(desktop, root, None, root))
            desktop.owned_python_pids.assert_not_called()
            dumper = root / "py-spy.exe"
            dumper.touch()
            context = MagicMock()
            context.__enter__.return_value = []
            desktop.owned_python_pids.return_value = context
            self.assertEqual(smoke.capture_python_stacks(desktop, root, dumper, root), {"processes": []})
            run.assert_not_called()

    def test_stack_dumps_are_capped_timed_and_never_request_locals(self):
        with tempfile.TemporaryDirectory() as directory:
            root, desktop = Path(directory), Mock()
            dumper = root / "py-spy.exe"
            dumper.touch()
            context = MagicMock()
            context.__enter__.return_value = [11, 22, 33, 44, 55]
            desktop.owned_python_pids.return_value = context
            outcomes = [subprocess.TimeoutExpired("py-spy", 5, output=b"partial", stderr=b"waiting"),
                        subprocess.CompletedProcess([], 0, stdout=b"s" * 70000, stderr=b"e" * 70000),
                        PermissionError("SECRET diagnostic detail"),
                        subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"unavailable")]
            with patch.object(smoke.subprocess, "run", side_effect=outcomes) as run:
                summary = smoke.capture_python_stacks(desktop, root, dumper, root)
            desktop.owned_python_pids.assert_called_once_with(root / "Python/python.exe")
            self.assertEqual(run.call_count, 4)
            for call, pid in zip(run.call_args_list, (11, 22, 33, 44)):
                self.assertEqual(call.args[0], [str(dumper), "dump", "--pid", str(pid)])
                self.assertEqual(call.kwargs["timeout"], 5)
                self.assertNotIn("env", call.kwargs)
            self.assertTrue(summary["processes"][0]["timed_out"])
            self.assertTrue(summary["processes"][1]["stdout"]["truncated"])
            self.assertEqual(summary["processes"][2]["error"], "PermissionError")
            self.assertNotIn("SECRET", json.dumps(summary))
            self.assertEqual((root / "python-11-stdout.txt").read_bytes(), b"partial")
            self.assertEqual((root / "python-22-stdout.txt").stat().st_size, 65536)
            self.assertEqual((root / "python-22-stderr.txt").stat().st_size, 65536)
            self.assertTrue(context.__exit__.called)

    def test_diagnostic_failure_does_not_mask_the_original_native_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root, desktop = Path(directory), Mock()
            dumper = root / "py-spy.exe"
            dumper.touch()
            desktop.owned_python_pids.side_effect = OSError("SECRET")
            original = RuntimeError("Export timed out")
            with self.assertRaises(RuntimeError) as raised:
                try:
                    raise original
                except RuntimeError:
                    summary = smoke.capture_python_stacks(desktop, root, dumper, root)
                    raise
            self.assertIs(raised.exception, original)
            self.assertEqual(summary, {"processes": [], "error": "OSError"})


@unittest.skipUnless(os.name == "nt", "Requires the Windows job APIs")
class WindowsJobCleanupTests(unittest.TestCase):
    def test_cleanup_owns_only_its_process_tree_even_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child.py"
            child.write_text("import time\ntime.sleep(30)\n")
            parent = root / "parent.py"
            parent.write_text("import json,subprocess,sys\nfrom pathlib import Path\n"
                              "p=subprocess.Popen([sys.executable,sys.argv[1]])\n"
                              "Path(sys.argv[2]).write_text(json.dumps({'pid':p.pid}))\n")
            unrelated = subprocess.Popen([sys.executable, str(child)])
            desktop, child_handle = None, None
            try:
                receipt = root / "child.json"
                desktop = smoke.WindowsDesktop(Path(sys.executable), dict(os.environ), root,
                                                arguments=[parent, child, receipt])
                deadline = time.monotonic() + 5
                while not receipt.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                pid = json.loads(receipt.read_text())["pid"]
                self.assertTrue(desktop.owns_pid(pid))
                self.assertFalse(desktop.owns_pid(unrelated.pid))
                with desktop.owned_python_pids(Path(sys.executable)) as matching:
                    self.assertIn(pid, matching)
                    self.assertNotIn(unrelated.pid, matching)
                child_handle = desktop.kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(child_handle)
                desktop.close()
                # TerminateJobObject starts asynchronous kernel teardown; wait on
                # the retained child handle instead of racing its final signal.
                self.assertEqual(desktop.kernel.WaitForSingleObject(child_handle, 5000), 0,
                                 "Cleanup left its render-server fixture running")
                self.assertIsNone(unrelated.poll(), "Cleanup stopped an unrelated process")
            finally:
                if desktop:
                    desktop.close()
                    if child_handle:
                        desktop.kernel.CloseHandle(child_handle)
                unrelated.terminate()
                unrelated.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
