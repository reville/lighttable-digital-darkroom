# SPDX-License-Identifier: GPL-3.0-only
"""Focused guards against false native-updater proof and release mutation.

These helper tests run without GTK. The actual native gate runs only against a
fully built Linux package under Xvfb/D-Bus in linux-build.yml.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("linux_updater_smoke", ROOT / "scripts/linux/updater-smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class LinuxUpdaterSmokeTests(unittest.TestCase):
    def test_isolation_discards_personal_runtime_and_catalog_overrides(self):
        personal = {"DISPLAY": ":123", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private-test-bus",
                    "LIGHTTABLE_CACHE_DIR": "/personal/cache", "LIGHTTABLE_CATALOG_FILE": "/personal/library",
                    "LIGHTTABLE_PREFS_FILE": "/personal/prefs", "LIGHTTABLE_PORT": "8080",
                    "LIGHTTABLE_HEADLESS": "1", "LIGHTTABLE_PROJECT_DIR": "/personal/code",
                    "PYTHONPATH": "/personal/code", "XDG_CONFIG_HOME": "/personal/config",
                    "XDG_CONFIG_DIRS": "/personal/extra-config", "HOME": "/unchanged/home"}
        with patch.dict(os.environ, personal, clear=True):
            environment = smoke.isolated_environment(Path("/isolated"))
        self.assertEqual(environment["XDG_CONFIG_HOME"], "/isolated/config")
        self.assertEqual(environment["LIGHTTABLE_CATALOG_FILE"], "/isolated/data/lighttable/Catalog/library.sqlite3")
        self.assertEqual(environment["HOME"], "/unchanged/home")
        self.assertEqual(environment["DISPLAY"], ":123")
        self.assertEqual(environment["DBUS_SESSION_BUS_ADDRESS"], personal["DBUS_SESSION_BUS_ADDRESS"])
        for forbidden in ("LIGHTTABLE_CACHE_DIR", "LIGHTTABLE_PREFS_FILE", "LIGHTTABLE_PROJECT_DIR",
                          "LIGHTTABLE_PORT", "LIGHTTABLE_HEADLESS", "PYTHONPATH", "XDG_CONFIG_DIRS"):
            self.assertNotIn(forbidden, environment)

    def test_render_requires_fresh_native_client_and_matching_ready_photo(self):
        ready = {"client": "native", "age": 0.5, "current": "1:updater-smoke.jpg",
                 "render": {"name": "1:updater-smoke.jpg", "state": "ready"}}
        self.assertTrue(smoke.rendered_photo(ready))
        for changed in ({"age": 20}, {"client": None}, {"current": "another.jpg"},
                        {"render": {"name": "old.jpg", "state": "ready"}},
                        {"render": {"name": "1:updater-smoke.jpg", "state": "loading"}}, {"render": None}):
            with self.subTest(changed=changed):
                self.assertFalse(smoke.rendered_photo({**ready, **changed}))

    def test_archive_changes_only_test_manifest_and_preserves_source_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            original = json.dumps({"version": "1.2.3", "source_dirty": False}).encode()
            (source / "build-manifest.json").write_bytes(original)
            executable = source / "real-shell"
            executable.write_bytes(b"actual unchanged binary payload")
            executable.chmod(0o755)
            (source / "shell").symlink_to("real-shell")
            archive = root / "update.tar.gz"
            smoke.make_archive(source, archive, {"version": "0.0.1", "source_dirty": False})
            self.assertEqual((source / "build-manifest.json").read_bytes(), original)
            with tarfile.open(archive) as stream:
                names = stream.getnames()
                self.assertEqual(names.count("LightTable/build-manifest.json"), 1)
                self.assertEqual(json.load(stream.extractfile("LightTable/build-manifest.json"))["version"], "0.0.1")
                self.assertEqual(stream.extractfile("LightTable/real-shell").read(), executable.read_bytes())
                self.assertEqual(stream.getmember("LightTable/real-shell").mode & 0o777, 0o755)
                self.assertTrue(stream.getmember("LightTable/shell").issym())
                self.assertEqual(stream.getmember("LightTable/shell").linkname, "real-shell")

    def test_rejection_gate_fails_when_apply_succeeds_or_fails_for_wrong_reason(self):
        class UpdateError(ValueError):
            pass
        update = Mock(UpdateError=UpdateError)
        updater = Mock()
        updater.apply.return_value = {"state": "installed"}
        with self.assertRaisesRegex(RuntimeError, "accepted"):
            smoke.expect_rejected(update, updater, {}, "signature")
        updater.apply.side_effect = UpdateError("signature did not verify")
        smoke.expect_rejected(update, updater, {}, "signature")
        updater.apply.side_effect = UpdateError("unrelated disk error")
        with self.assertRaisesRegex(RuntimeError, "Wrong update rejection"):
            smoke.expect_rejected(update, updater, {}, "signature")

    def test_missing_server_identity_is_never_native_process_proof(self):
        for pid in (None, 0, 1, True, "100"):
            with self.subTest(pid=pid), self.assertRaisesRegex(RuntimeError, "server process identity"):
                smoke.verify_processes(Path("/bundle"), 123, {"pid": pid})


if __name__ == "__main__":
    unittest.main()
