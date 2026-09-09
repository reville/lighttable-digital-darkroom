"""Safety and evidence checks for the Windows native acceptance runner."""
from __future__ import annotations

import ctypes
import importlib.util
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
