# SPDX-License-Identifier: GPL-3.0-only
"""Safety, X11 protocol and real TIFF precision checks for native acceptance."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("linux_desktop_acceptance", ROOT / "scripts/linux/desktop-acceptance.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class LinuxAcceptanceTests(unittest.TestCase):
    def test_isolated_data_discards_personal_catalog_code_and_mode_overrides(self):
        root = Path("/private/acceptance")
        environment = smoke.isolated_environment(root, {
            "LIGHTTABLE_CATALOG_FILE": "/personal/catalog", "LIGHTTABLE_PROJECT_DIR": "/personal/source",
            "LIGHTTABLE_SAFE_MODE": "1", "LIGHTTABLE_HEADLESS": "1", "PYTHONPATH": "/personal/python",
            "XDG_CONFIG_HOME": "/personal/preferences", "DISPLAY": ":123",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private-test-bus", "HOME": "/unchanged/home"})
        for removed in ("LIGHTTABLE_PROJECT_DIR", "LIGHTTABLE_SAFE_MODE", "LIGHTTABLE_HEADLESS", "PYTHONPATH"):
            self.assertNotIn(removed, environment)
        self.assertEqual(environment["HOME"], "/unchanged/home")
        self.assertEqual(environment["DISPLAY"], ":123")
        for key in ("LIGHTTABLE_CATALOG_FILE", "LIGHTTABLE_DIR", "LIGHTTABLE_INSTANCE_DIR", "XDG_CONFIG_HOME"):
            self.assertTrue(Path(environment[key]).is_relative_to(root), key)

    def test_owned_window_rejects_missing_duplicate_other_pid_and_wrong_title(self):
        target = {"id": 10, "pid": 123, "title": smoke.TITLE}
        other = {"id": 20, "pid": 999, "title": smoke.TITLE}
        helper = {"id": 30, "pid": 123, "title": "LightTable helper"}
        self.assertEqual(smoke.owned_window([target, other, helper], 123), 10)
        for windows in ([], [other, helper], [target, {**target, "id": 40}]):
            with self.subTest(windows=windows), self.assertRaisesRegex(RuntimeError, "exactly one X11 window"):
                smoke.owned_window(windows, 123)

    def test_close_event_uses_native_long_layout_and_window_protocol_delivery(self):
        event = smoke.close_event(1234, 5678, 90, 91)
        self.assertEqual(event.client.type, 33)
        self.assertEqual(event.client.display, 1234)
        self.assertEqual(event.client.window, 5678)
        self.assertEqual(event.client.message_type, 90)
        self.assertEqual(event.client.format, 32)
        self.assertEqual(list(event.client.data), [91, 0, 0, 0, 0])
        self.assertEqual(ctypes.sizeof(smoke.XEvent), 24 * ctypes.sizeof(ctypes.c_long))

    def test_x11_requires_delete_protocol_and_rechecks_identity_before_delivery(self):
        for protocols, final_pid, final_title, permitted in (
                ((91,), (123,), smoke.TITLE.encode(), True),
                ((), (123,), smoke.TITLE.encode(), False),
                ((91,), (999,), smoke.TITLE.encode(), False),
                ((91,), (123,), b"another window", False)):
            with self.subTest(protocols=protocols, pid=final_pid, title=final_title):
                display = smoke.X11.__new__(smoke.X11)
                display.display, display.lib = 1234, Mock()
                display.lib.XDefaultRootWindow.return_value = 10
                display.lib.XQueryTree.return_value = 0
                display.lib.XSendEvent.return_value = 1
                display.atom = Mock(side_effect=lambda name: {"UTF8_STRING": 89, "WM_PROTOCOLS": 90, "WM_DELETE_WINDOW": 91}[name])
                display.property = Mock(side_effect=[(123,), smoke.TITLE.encode(), protocols, final_pid, final_title])
                if permitted:
                    receipt = display.delete_window(123, time.monotonic() + 5)
                    self.assertEqual(receipt["window_id"], 10)
                    arguments = display.lib.XSendEvent.call_args.args
                    self.assertEqual(arguments[:4], (1234, 10, 0, 0))
                    event = ctypes.cast(arguments[4], ctypes.POINTER(smoke.XEvent)).contents
                    self.assertEqual((event.client.window, event.client.message_type, event.client.data[0]), (10, 90, 91))
                else:
                    with self.assertRaises(RuntimeError):
                        display.delete_window(123, time.monotonic() + 5)
                    display.lib.XSendEvent.assert_not_called()

    def test_a_delivered_ui_command_is_not_repeated_after_response_timeout(self):
        api = Mock()
        api.request.side_effect = HTTPError("http://127.0.0.1", 504, "Timeout", {}, None)
        smoke.send_ui(api, "goto", {"name": "smoke.tif"})
        self.assertEqual(api.request.call_count, 1)
        api.request.side_effect = HTTPError("http://127.0.0.1", 403, "Disabled", {}, None)
        with self.assertRaises(HTTPError):
            smoke.send_ui(api, "goto")

    def test_goto_waits_for_fresh_native_library_and_is_sent_once(self):
        process, api = Mock(), Mock()
        source = ROOT / "tests/smoke.tif"
        process.poll.return_value = None
        empty = {"client": "native", "age": 0, "visibleCount": 0}
        visible = {**empty, "visibleCount": 1}
        rendered = {**visible, "current": "1:smoke.tif", "render": {"name": "1:smoke.tif", "state": "ready"}}
        states = iter((empty, {**visible, "age": 30}, {**visible, "client": None}, visible, rendered))
        observed, commands = [], []
        def request(route, body=None):
            if route == "/api/ui/state":
                observed.append(next(states))
                return observed[-1]
            if route == "/api/catalog/query":
                self.assertEqual(body, {"limit": 10})
                return {"items": [{"name": "1:smoke.tif", "relpath": "smoke.tif", "sourcePath": str(source.parent)},
                                  {"name": "2:smoke.tif", "relpath": "smoke.tif", "sourcePath": str(ROOT)}]}
            self.assertEqual(route, "/api/ui/command")
            self.assertEqual(observed[-1], visible)
            commands.append(body)
            return {"ok": True}
        api.request.side_effect = request
        with patch.object(smoke.time, "sleep"):
            self.assertEqual(smoke.render_photo(process, api, time.monotonic() + 5, source), "1:smoke.tif")
        self.assertEqual(commands, [{"command": "goto", "args": {"name": "1:smoke.tif"}, "timeout": 3}])

    def test_photo_selection_uses_real_catalog_query_records_and_excludes_other_sources_and_copies(self):
        import catalog
        import catalog_scan
        import numpy as np
        import tifffile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "photos/smoke.tif"
            cat = catalog.Catalog(root / "library.sqlite3")
            try:
                for folder in ("photos", "other"):
                    directory = root / folder
                    directory.mkdir()
                    tifffile.imwrite(directory / source.name, np.zeros((2, 2, 3), dtype=np.uint16),
                                     photometric="rgb", metadata=None)
                    source_id = cat.add_source(directory)
                    catalog_scan.scan_source(cat, source_id, read_metadata_for_new=False)
                    if folder == "photos":
                        expected = catalog.qualified_name(source_id, source.name)
                        cat.add_virtual_copy(cat.image_id_for(source_id, source.name), "copy-1", "Alternate")
                process, api = Mock(), Mock()
                process.poll.return_value = None
                rendered = {"client": "native", "age": 0, "visibleCount": 1, "current": expected,
                            "render": {"name": expected, "state": "ready"}}
                def request(route, body=None):
                    if route == "/api/ui/state":
                        return rendered
                    if route == "/api/catalog/query":
                        self.assertEqual(body, {"limit": 10})
                        page = cat.query(body)
                        self.assertEqual(len(page["items"]), 3)
                        return page
                    self.assertEqual(route, "/api/ui/command")
                    self.assertEqual(body, {"command": "goto", "args": {"name": expected}, "timeout": 3})
                    return {"ok": True}
                api.request.side_effect = request
                self.assertEqual(smoke.render_photo(process, api, time.monotonic() + 5, source), expected)
            finally:
                cat.close()

    def test_pending_edits_do_not_pass_and_film_must_be_explicitly_disabled(self):
        valid = {"grade": {"exposure": 0.5}, "rating": 4, "params": {"profile_enabled": False}}
        self.assertTrue(smoke.edits_saved(valid))
        for pending in (None, {}, {**valid, "grade": None}, {**valid, "grade": []},
                        {**valid, "rating": 3}, {**valid, "params": None},
                        {**valid, "params": {}}, {**valid, "params": {"profile_enabled": True}}):
            with self.subTest(pending=pending):
                self.assertFalse(smoke.edits_saved(pending))

    def test_bundle_requires_expected_clean_source_and_contained_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary).resolve()
            for name in ("bin/lighttable-desktop", "bin/lighttable-desktop-shell", "Python/bin/python3",
                         "Resources/LightTable/server.py", "Resources/LightTable/render_cli.py"):
                path = bundle / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            manifest = {"source_revision": "a" * 40, "source_dirty": False, "platform": "linux", "architecture": "x86_64"}
            path = bundle / "build-manifest.json"
            with patch.object(smoke.sys, "prefix", str(bundle / "Python")), patch.object(smoke.platform, "machine", return_value="x86_64"):
                path.write_text(json.dumps(manifest))
                self.assertEqual(smoke.validate_bundle(bundle, "a" * 40), manifest)
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    smoke.validate_bundle(bundle, "b" * 40)
                path.write_text(json.dumps({**manifest, "source_dirty": True}))
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    smoke.validate_bundle(bundle, "a" * 40)
                path.write_text(json.dumps(manifest))
                with tempfile.TemporaryDirectory() as outside:
                    external = Path(outside) / "render_cli.py"
                    external.touch()
                    bundled = bundle / "Resources/LightTable/render_cli.py"
                    bundled.unlink()
                    bundled.symlink_to(external)
                    with self.assertRaisesRegex(RuntimeError, "Incomplete bundle"):
                        smoke.validate_bundle(bundle, "a" * 40)

    def test_snapshot_allowlist_and_redaction_never_include_registration_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "instances").mkdir()
            (root / "instances/8321.json").write_text(json.dumps({
                "pid": 123, "port": 8321, "folder": str(root / "photos"), "token": "PRIVATE_TOKEN",
                "extra": {"token": "ANOTHER_SECRET"}}))
            text = json.dumps(smoke.startup_snapshot(root))
            self.assertNotIn("TOKEN", text)
            self.assertNotIn("SECRET", text)
            self.assertIn('"pid": 123', text)
            self.assertEqual(smoke.redact("failed PRIVATE_TOKEN", {"PRIVATE_TOKEN"}), "failed [redacted]")

    def test_real_tiff_export_requires_rgb16_precision_shape_and_icc(self):
        import numpy as np
        import tifffile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.tif"
            ramp = np.tile(np.linspace(0, 65535, smoke.SHAPE[1], dtype=np.uint16)[None, :, None], (smoke.SHAPE[0], 1, 3))
            icc = b"test ICC payload"
            tags = [(34675, "B", len(icc), icc, False)]
            tifffile.imwrite(path, ramp, photometric="rgb", extratags=tags)
            proof = smoke.verify_export(path)
            self.assertEqual(proof["red_levels"], 1024)
            self.assertEqual(proof["dtype"], "uint16")
            for pixels, profiles in ((ramp, []), ((ramp // 257).astype(np.uint8), tags),
                                     ((ramp // 257) * 257, tags), (ramp[:1], tags)):
                with self.subTest(dtype=pixels.dtype, shape=pixels.shape, profile=bool(profiles)):
                    tifffile.imwrite(path, pixels, photometric="rgb", extratags=profiles)
                    with self.assertRaises(RuntimeError):
                        smoke.verify_export(path)


if __name__ == "__main__":
    unittest.main()
