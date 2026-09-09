"""Safety and evidence checks for the Windows native acceptance runner."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
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
