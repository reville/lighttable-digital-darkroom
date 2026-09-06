from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefs = self.root / "prefs.json"
        self.presets = self.root / "presets.json"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        for patch in (
            mock.patch.object(server, "PREFS_FILE", self.prefs),
            mock.patch.object(server, "PRESETS_FILE", self.presets),
            mock.patch.object(server, "CACHE", self.cache),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write_prefs(self, value: dict) -> None:
        self.prefs.write_text(json.dumps(value))

    def test_new_photos_start_with_film_disabled_without_preference(self):
        params, _ = server.effective_new_photo_defaults()
        self.assertFalse(params["profile_enabled"])

    def test_user_can_explicitly_enable_film_for_new_photos(self):
        self.write_prefs({"newPhotoDefaults": {"filmEnabled": True}})
        params, _ = server.effective_new_photo_defaults()
        self.assertTrue(params["profile_enabled"])

    def test_default_preset_does_not_implicitly_enable_film(self):
        self.presets.write_text(json.dumps([{
            "name": "Film look", "params": {"profile_enabled": True},
        }]))
        self.write_prefs({"newPhotoDefaults": {"preset": "Film look"}})
        params, _ = server.effective_new_photo_defaults()
        self.assertFalse(params["profile_enabled"])

    def test_new_photo_defaults_layer_preset_and_explicit_choices(self):
        self.presets.write_text(json.dumps([{
            "name": "Quiet portrait",
            "params": {"stock": "kodak_gold_200", "profile_enabled": True},
            "grade": {"exposure": 0.4, "contrast": 0.2},
        }]))
        self.write_prefs({"newPhotoDefaults": {
            "preset": "Quiet portrait", "filmEnabled": False,
            "workflow": "creative", "developProfile": "linear",
        }})
        params, grade = server.effective_new_photo_defaults()
        self.assertEqual(params["stock"], "kodak_gold_200")
        self.assertFalse(params["profile_enabled"])
        self.assertEqual(params["workflow_mode"], "creative")
        self.assertEqual(params["developProfile"], "linear")
        self.assertEqual(grade["exposure"], 0.4)

    def test_camera_defaults_can_match_serial_and_iso_with_model_fallback(self):
        metadata = {"Make": "Example", "Model": "One",
                    "BodySerialNumber": "A12", "ISO": "800"}
        model_key = "example|one"
        serial_key = f"{model_key}|serial:a12"
        iso_key = f"{serial_key}|iso:800"
        self.write_prefs({
            "rawDefaultMatch": "iso",
            "rawCameraDefaults": {
                model_key: {"raw_profile": "camera"},
                iso_key: {"raw_profile": "detail"},
            },
        })
        with mock.patch.object(server, "exif_for", return_value=metadata):
            result = server.raw_camera_default("capture.dng")
            self.assertEqual(result["key"], iso_key)
            self.assertEqual(result["settings"]["raw_profile"], "detail")
            self.assertEqual(result["serial"], "A12")
            self.assertEqual(result["iso"], "800")
            self.write_prefs({
                "rawDefaultMatch": "serial",
                "rawCameraDefaults": {model_key: {"raw_profile": "camera"}},
            })
            fallback = server.raw_camera_default("capture.dng")
            self.assertEqual(fallback["key"], model_key)
            self.assertEqual(fallback["settings"]["raw_profile"], "camera")

    def test_cache_budget_scales_and_purge_stays_inside_cache_root(self):
        self.write_prefs({"cacheBudgetGB": 4})
        generated = self.cache / "render" / "preview.jpg"
        generated.parent.mkdir()
        generated.write_bytes(b"generated")
        outside = self.root / "original.jpg"
        outside.write_bytes(b"original")
        status = server.cache_status()
        self.assertEqual(status["budgetBytes"], 4 * 1024 ** 3)
        self.assertEqual(status["usedBytes"], len(b"generated"))
        result = server.purge_generated_cache()
        self.assertEqual(result["removed"], 1)
        self.assertFalse(generated.exists())
        self.assertEqual(outside.read_bytes(), b"original")

    def test_backup_preferences_have_safe_defaults_and_never_disables_auto(self):
        catalog = mock.Mock()
        catalog.path = self.root / "library.sqlite3"
        self.write_prefs({"backupFrequency": "never"})
        self.assertIsNone(server.automatic_backup_max_age())
        self.assertEqual(server.configured_backup_directory(catalog),
                         self.root / "Backups")
        custom = self.root / "verified-backups"
        self.write_prefs({"backupFrequency": "weekly",
                          "backupDirectory": str(custom)})
        self.assertEqual(server.automatic_backup_max_age(), 7 * 24 * 60 * 60)
        self.assertEqual(server.configured_backup_directory(catalog), custom)

    def test_mirror_preference_disables_delayed_portable_writes_but_not_xmp(self):
        cat = mock.Mock()
        cat.sources.return_value = [{"id": 1, "available": True}]
        timer = mock.Mock()
        callbacks = []
        def capture_timer(delay, callback):
            callbacks.append(callback)
            return timer
        with mock.patch.object(server, "CATALOG", cat), \
                mock.patch.object(server, "CATALOG_MIRROR", True), \
                mock.patch.object(server, "_MIRROR_TIMER", None), \
                mock.patch.object(server.threading, "Timer", side_effect=capture_timer), \
                mock.patch.object(server.catalog_scan, "mirror_state_file") as mirror, \
                mock.patch.object(server, "write_pending_sidecars") as sidecars:
            self.write_prefs({"catalogMirror": True})
            server._queue_mirror()
            self.assertEqual(len(callbacks), 1)
            self.write_prefs({"catalogMirror": False, "writeSidecars": True})
            callbacks[0]()
            mirror.assert_not_called()
            sidecars.assert_called_once()
            self.assertFalse(server.catalog_mirror_enabled())
            server._queue_mirror()
            self.assertEqual(len(callbacks), 2)
            self.write_prefs({"catalogMirror": False, "writeSidecars": False})
            server._queue_mirror()
            self.assertEqual(len(callbacks), 2)
            timer.cancel.assert_called()

    def test_launch_configuration_can_disable_mirrors_even_with_preference_on(self):
        self.write_prefs({"catalogMirror": True})
        with mock.patch.object(server, "CATALOG_MIRROR", False):
            self.assertFalse(server.catalog_mirror_enabled())

    def test_settings_surface_and_durable_migration_are_shipped(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text()
        app = (root / "web" / "app.js").read_text()
        settings = (root / "web" / "settings.js").read_text()
        self.assertIn('id="settingsDialog"', html)
        self.assertIn('data-settings-tab="performance"', html)
        self.assertIn("localStorage.removeItem('lt.keyScheme')", app)
        self.assertIn("api('/api/cache/purge'", settings)
        self.assertIn("api('/api/sidecars/write'", settings)
        self.assertIn(
            "byId('newPhotoFilmEnabled').checked = "
            "defaults.filmEnabled === true",
            settings)
        self.assertNotIn('id="newPhotoFilmEnabledSidebar"', html)
        self.assertNotIn("FILM_DEFAULT_CHECKBOX_IDS", settings)
        self.assertIn(
            "for (const id of ['newPhotoFilmEnabled', 'newPhotoWorkflow'",
            settings)
        before_settings = html.split('id="settingsDialog"', 1)[0]
        self.assertNotIn('class="shortcut-details"', before_settings)
        self.assertIn('class="shortcut-details"', html)


if __name__ == "__main__":
    unittest.main()
