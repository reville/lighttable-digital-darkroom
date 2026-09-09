"""Real catalog fixtures exercise offline durability and untrusted recipe boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import preset_io
import preset_library as library


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.look = {k: v for k, v in library.builtin_presets()[0].items() if k != "collection"}
        self.payload = json.dumps({"format": "LightTable Preset", "version": 3,
                                   "presets": [self.look]}).encode()
        self.record = {**library.metadata(self.look), "id": self.look["id"],
            "name": self.look["name"], "filmMode": "off", "schemaVersion": 3,
            "capabilities": ["look-v1"], "previews": [],
            "file": {"url": f"/presets/files/{self.look['id']}/1.0.0.ltpreset",
                     "sha256": hashlib.sha256(self.payload).hexdigest(), "bytes": len(self.payload)}}
        self.catalog = {"schemaVersion": 1, "updatedAt": "2026-09-08", "presets": [self.record]}
        self.calls = []
        self.offline = False
        def fetch(url, limit, etag=""):
            self.calls.append((url, etag))
            if self.offline:
                raise OSError("network unavailable")
            return (json.dumps(self.catalog).encode() if url.endswith("catalog-v1.json")
                    else self.payload), '"v1"'
        self.service = library.CommunityCatalog(Path(self.temp.name), fetch=fetch)

    def test_catalog_and_recipe_survive_restart_and_network_failure(self):
        self.assertFalse(self.service.catalog()["offline"])
        downloaded = self.service.recipe(self.look["id"], "1.0.0")
        self.assertEqual(downloaded["grade"], self.look["grade"])
        self.offline = True
        restarted = library.CommunityCatalog(self.temp.name, fetch=self.service.fetch)
        cached = restarted.catalog(refresh=True)
        self.assertTrue(cached["offline"])
        self.assertTrue(cached["cached"])
        self.assertEqual(restarted.recipe(self.look["id"])["id"], self.look["id"])

    def test_daily_revalidation_and_304(self):
        self.service.catalog()
        self.service.catalog()
        self.assertEqual(len(self.calls), 1)
        with mock.patch.object(self.service, "fetch", return_value=(None, '"v1"')) as fetch:
            self.assertTrue(self.service.catalog(refresh=True)["cached"])
            self.assertEqual(fetch.call_args.args[2], '"v1"')

    def test_invalid_refresh_preserves_last_valid_catalog(self):
        self.service.catalog()
        self.catalog["schemaVersion"] = 100
        response = self.service.catalog(refresh=True)
        self.assertTrue(response["offline"])
        self.assertEqual(response["presets"][0]["id"], self.look["id"])

    def test_corrupt_download_is_not_cached(self):
        self.service.catalog()
        self.payload += b"corruption"
        with self.assertRaisesRegex(ValueError, "verified"):
            self.service.recipe(self.look["id"])
        self.assertFalse((Path(self.temp.name) / "recipes").exists())

    def test_install_requires_the_reviewed_version(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            self.service.recipe(self.look["id"], "0.9.0")

    def test_unsupported_capability_is_visible_but_not_installable(self):
        self.record["capabilities"] = ["look-v1", "future-3d-lut"]
        self.assertFalse(self.service.catalog()["presets"][0]["compatible"])
        with self.assertRaisesRegex(ValueError, "Update"):
            self.service.recipe(self.look["id"])

    def test_listing_keeps_scope_and_photo_credit(self):
        credit = {"name": "Photographer", "url": "https://example.org/photo", "license": "CC0-1.0"}
        self.record["previews"] = [{"label": "Portrait", "before": "/presets/images/a.webp",
                                    "after": "/presets/images/b.webp", "credit": credit}]
        result = self.service.catalog()["presets"][0]
        self.assertEqual(result["scope"], "look")
        self.assertEqual(result["previews"][0]["credit"], credit)

    def test_remote_urls_and_mismatched_file_paths_are_rejected(self):
        for url in ("https://attacker.example/a", "file:///etc/passwd",
                    "https://lighttable.app:444/presets/file", "/presets/%2e%2e/secret",
                    "https://lighttable.app/presets/file?redirect=1", "/presets/files/other/look/1.0.0.ltpreset"):
            with self.subTest(url=url):
                value = copy.deepcopy(self.catalog)
                value["presets"][0]["file"]["url"] = url
                with self.assertRaises(ValueError):
                    self.service.validate(value)

    def test_duplicate_ids_and_oversized_files_are_rejected(self):
        value = copy.deepcopy(self.catalog)
        value["presets"] *= 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.service.validate(value)
        self.record["file"]["bytes"] = library.MAX_RECIPE_BYTES + 1
        with self.assertRaises(ValueError):
            self.service.validate(self.catalog)


class PresetContractTests(unittest.TestCase):
    def test_bundled_looks_are_valid_distinct_and_protect_corrections(self):
        looks = library.builtin_presets()
        self.assertEqual(len(looks), 23)
        self.assertEqual(len({p["id"] for p in looks}), len(looks))
        self.assertEqual(sum(p["filmMode"] == "on" for p in looks), 13)
        for look in looks:
            patch = library.look_patch(look)
            self.assertTrue(set(patch["grade"]) <= library.CREATIVE_GRADE_KEYS)
            self.assertFalse(set(patch.get("params", {})) & {"wb_mode", "rotate", "raw_profile", "film_format"})
            self.assertFalse(set(patch) & {"crop", "masks", "heals", "optics"})

    def test_v3_native_roundtrip_and_future_version_rejection(self):
        look = library.builtin_presets()[1]
        filename, _, encoded = preset_io.export_preset(look, "lighttable")
        self.assertEqual(json.loads(encoded)["version"], 3)
        imported = preset_io._import_bytes(filename, encoded.encode())[0]
        self.assertEqual(imported["author"], look["author"])
        self.assertEqual(imported["params"], look["params"])
        self.assertEqual(imported["filmMode"], "on")
        bad = json.loads(encoded); bad["version"] = 4
        with self.assertRaisesRegex(ValueError, "Update"):
            preset_io._import_bytes(filename, json.dumps(bad).encode())

    def test_v3_cannot_smuggle_capture_or_local_edits(self):
        look = library.builtin_presets()[0]
        for key, value in (("grade", {"exposure": 2}), ("params", {"wb_mode": "daylight"}),
                           ("masks", [{"type": "radial"}]), ("grade", {"contrast": float("nan")})):
            bad = {**look, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                library.validate_look(bad)

    def test_imported_community_copy_keeps_credit_without_update_ownership(self):
        look = library.builtin_presets()[1]
        look["community"] = {"id": look["id"], "version": look["version"]}
        filename, _, encoded = preset_io.export_preset(look, "lighttable")
        imported = preset_io._import_bytes(filename, encoded.encode())[0]
        self.assertNotIn("community", imported)
        self.assertNotIn("collection", imported)
        self.assertEqual(imported["parentId"], look["id"])
        self.assertEqual(imported["author"], look["author"])

    def test_submission_strips_private_corrections_and_preserves_partial_film_scope(self):
        look = {**library.builtin_presets()[1], "params": {"stock": "kodak_portra_400"},
                "includedFilm": ["stock"], "masks": [{"private": True}],
                "grade": {"contrast": .1, "exposure": 2, "temp": .3},
                "includedGrade": ["contrast", "exposure", "temp"], "crop": {"x": .5}}
        prepared = library.prepare_look(look)
        self.assertEqual(prepared["params"], {"stock": "kodak_portra_400"})
        self.assertEqual(prepared["grade"], {"contrast": .1})
        self.assertEqual(prepared["masks"], [])
        self.assertNotIn("crop", prepared)
        library.validate_look(prepared)


class PresetPersistenceTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(server, "PRESETS_FILE", Path(self.temp.name) / "presets.json")
        patch.start(); self.addCleanup(patch.stop)

    def test_bundled_data_is_not_written_to_user_library(self):
        self.server.save_presets(self.server.load_presets())
        self.assertEqual(json.loads(self.server.PRESETS_FILE.read_text()), [])
        self.assertEqual(len(self.server.load_presets()), len(library.builtin_presets()))

    def test_install_update_and_offline_load_preserve_stable_identity(self):
        recipe = {k: v for k, v in library.builtin_presets()[0].items() if k != "collection"}
        with mock.patch.object(self.server.COMMUNITY_PRESETS, "recipe", return_value=recipe):
            first = self.server.install_community_preset({"id": recipe["id"], "version": "1.0.0"})
        recipe = copy.deepcopy(recipe); recipe["version"] = "1.0.1"
        with mock.patch.object(self.server.COMMUNITY_PRESETS, "recipe", return_value=recipe):
            second = self.server.install_community_preset({"id": recipe["id"], "version": "1.0.1"})
        self.assertEqual(first["installedId"], second["installedId"])
        users = self.server.load_user_presets()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["version"], "1.0.1")
        self.assertEqual(users[0]["community"]["version"], "1.0.1")
        self.assertEqual(len(self.server.load_presets()), len(library.builtin_presets()) + 1)

    def test_saving_variation_then_reinstalling_keeps_the_variation(self):
        recipe = {k: v for k, v in library.builtin_presets()[0].items() if k != "collection"}
        with mock.patch.object(self.server.COMMUNITY_PRESETS, "recipe", return_value=recipe):
            self.server.install_community_preset({"id": recipe["id"]})
            handler = self.server.Handler.__new__(self.server.Handler)
            handler.path = '/api/presets'; handler.headers = {}
            handler._body = lambda: {'action': 'save', 'name': recipe['name'],
                                     'grade': {'contrast': .2}, 'includedGrade': ['contrast']}
            results = []
            handler._json = lambda value, status=200: results.append((status, value))
            handler._log_request = lambda _: None
            handler.do_POST()
            self.assertEqual(results[0][0], 200)
            local = self.server.load_user_presets()[0]
            self.assertNotIn('community', local)
            self.assertFalse(local['id'].startswith('community:'))
            self.server.install_community_preset({'id': recipe['id']})
        users = self.server.load_user_presets()
        self.assertEqual(len(users), 2)
        self.assertEqual(next(p for p in users if p['id'] == local['id'])['grade']['contrast'], .2)


if __name__ == "__main__":
    unittest.main()

class SubmissionBundleTests(unittest.TestCase):
    def test_bundle_uses_isolated_licensed_samples_and_excludes_private_state(self):
        import base64
        import io
        import zipfile
        import preset_submission
        from PIL import Image
        preset = library.builtin_presets()[0]
        def render(command, **kwargs):
            env = kwargs['env']
            self.assertEqual(env['LIGHTTABLE_DIR'], str(preset_submission.SAMPLES))
            self.assertEqual(env['LIGHTTABLE_CATALOG'], '0')
            self.assertTrue(env['LIGHTTABLE_PREFS_FILE'].startswith(command[-1]))
            sources = json.loads((preset_submission.SAMPLES / 'provenance.json').read_text())
            for photo in sources:
                for stage in ('before', 'after'):
                    Image.new('RGB', (10, 10), '#678').save(
                        Path(command[-1]) / f"{Path(photo['file']).stem}-{stage}.jpg")
            return mock.Mock(returncode=0)
        with mock.patch.object(preset_submission.subprocess, 'run', side_effect=render):
            result = preset_submission.build_bundle(preset)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result['content']))) as bundle:
            self.assertEqual(len(bundle.namelist()), 9)
            self.assertEqual(sum(n.endswith('.jpg') for n in bundle.namelist()), 6)
            self.assertNotIn('private', bundle.read('README.txt').decode())
            recipe = next(n for n in bundle.namelist() if n.endswith('.ltpreset'))
            self.assertEqual(json.loads(bundle.read(recipe))['presets'][0]['scope'], 'look')

    def test_failed_render_releases_the_submission_lock(self):
        import preset_submission
        with mock.patch.object(preset_submission.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError, 'could not be rendered'):
                preset_submission.build_bundle(library.builtin_presets()[0])
        self.assertFalse(preset_submission._RENDER_LOCK.locked())
