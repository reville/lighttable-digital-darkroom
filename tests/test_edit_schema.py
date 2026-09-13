# SPDX-License-Identifier: GPL-3.0-only
"""Saved Whites values keep their appearance across the sign correction."""
import json
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest import mock

import numpy as np

import catalog
import edit_schema
import edits
import grade
import preset_io
import server
import xmp_sidecar


LEGACY = {
    "grade": {"exposure": 0.2, "whites": 0.3, "blacks": -0.1},
    "masks": [{"id": "m1", "type": "radial", "grade": {"whites": -0.25, "texture": 0.1}},
              {"id": "m2", "type": "linear", "grade": {"exposure": 0.5}}],
    "versions": [{"id": "v1", "name": "Look", "grade": {"whites": 0.4},
                  "masks": [{"id": "m3", "grade": {"whites": 0.05}}]}],
}


class UpgradeFunctionTests(unittest.TestCase):
    def test_version_one_negates_whites_everywhere_and_nothing_else(self):
        out = edit_schema.upgrade_edit(LEGACY, 1)
        self.assertEqual(out["grade"], {"exposure": 0.2, "whites": -0.3, "blacks": -0.1})
        self.assertEqual(out["masks"][0]["grade"], {"whites": 0.25, "texture": 0.1})
        self.assertEqual(out["masks"][1]["grade"], {"exposure": 0.5})
        self.assertEqual(out["versions"][0]["grade"], {"whites": -0.4})
        self.assertEqual(out["versions"][0]["masks"][0]["grade"], {"whites": -0.05})
        # The input is never mutated: callers compare before and after.
        self.assertEqual(LEGACY["grade"]["whites"], 0.3)

    def test_current_records_are_left_alone(self):
        self.assertIs(edit_schema.upgrade_edit(LEGACY, edit_schema.EDIT_SCHEMA_VERSION), LEGACY)
        self.assertIs(edit_schema.upgrade_grade(LEGACY["grade"], 2), LEGACY["grade"])

    def test_zero_absent_and_malformed_values_survive(self):
        self.assertEqual(edit_schema.upgrade_grade({"whites": 0, "blacks": 0.2}, 1),
                         {"whites": 0, "blacks": 0.2})
        self.assertEqual(edit_schema.upgrade_grade({"exposure": 1}, 1), {"exposure": 1})
        self.assertEqual(edit_schema.upgrade_grade({"whites": "bad"}, 1), {"whites": "bad"})
        self.assertTrue(np.isnan(edit_schema.upgrade_grade({"whites": float("nan")}, 1)["whites"]))
        self.assertEqual(edit_schema.upgrade_edit("text", 1), "text")
        self.assertEqual(edit_schema.upgrade_masks([None, "x"], 1), [None, "x"])

    def test_record_marker_makes_the_upgrade_idempotent(self):
        once = edit_schema.upgrade_record(LEGACY)
        self.assertEqual(once["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(once["grade"]["whites"], -0.3)
        twice = edit_schema.upgrade_record(once)
        self.assertEqual(twice, once)
        for marker in (None, "2", True, float("inf"), 0, -3):
            self.assertEqual(edit_schema.record_version({"editSchema": marker}), 1)
        self.assertEqual(edit_schema.record_version({"editSchema": 7}), 7)

    def test_folder_state_is_upgraded_once(self):
        state = {"images": {"a.jpg": LEGACY, "b.jpg": {"rating": 3}, "c": "junk"}}
        upgraded, changed = edit_schema.upgrade_state(state)
        self.assertTrue(changed)
        self.assertEqual(upgraded["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(upgraded["images"]["a.jpg"]["grade"]["whites"], -0.3)
        self.assertEqual(upgraded["images"]["b.jpg"], {"rating": 3})
        self.assertEqual(upgraded["images"]["c"], "junk")
        again, changed = edit_schema.upgrade_state(upgraded)
        self.assertFalse(changed)
        self.assertEqual(again, upgraded)


class RenderDirectionTests(unittest.TestCase):
    """Positive Whites brightens, in the global grade and through a mask."""

    def setUp(self):
        ramp = np.linspace(0.0, 0.9, 96, dtype=np.float32)
        self.image = np.repeat(ramp[None, :, None], 3, axis=2)
        self.image = np.repeat(self.image, 16, axis=0)

    def white_point(self, image):
        return float(np.percentile(image @ np.array([0.2126, 0.7152, 0.0722]), 99.5))

    def test_global_whites_moves_the_white_point_with_its_sign(self):
        base = self.white_point(self.image)
        up = self.white_point(grade.apply(self.image, grade.clean({"whites": 0.5})))
        down = self.white_point(grade.apply(self.image, grade.clean({"whites": -0.5})))
        self.assertGreater(up, base)
        self.assertLess(down, base)
        # Mid-range pixels brighten too: the whole top of the range stretches.
        mid = grade.apply(self.image, grade.clean({"whites": 0.5}))[0, 48, 0]
        self.assertGreater(mid, self.image[0, 48, 0])

    def test_blacks_direction_is_unchanged(self):
        image = np.clip(self.image + 0.1, 0, 1)
        lifted = grade.apply(image, grade.clean({"blacks": 0.5}))[0, 0, 0]
        deepened = grade.apply(image, grade.clean({"blacks": -0.5}))[0, 0, 0]
        self.assertGreater(lifted, image[0, 0, 0])
        self.assertLess(deepened, image[0, 0, 0])

    def test_numba_and_numpy_paths_agree_on_the_new_sign(self):
        if not grade._HAS_NUMBA:
            self.skipTest("numba unavailable")
        cleaned = grade.clean({"whites": 0.6, "blacks": -0.2})
        with mock.patch.object(grade, "_HAS_NUMBA", False):
            reference = grade.apply(self.image, cleaned)
        np.testing.assert_allclose(grade.apply(self.image, cleaned), reference, atol=2e-4)

    def test_local_whites_through_a_mask_brightens(self):
        mask = edits.clean_masks([{"type": "linear", "start": [0.0, 0.5], "end": [1.0, 0.5],
                                   "feather": 0.0, "grade": {"whites": 0.8}}])
        out = edits.apply_masks(self.image.copy(), mask)
        self.assertGreater(self.white_point(out), self.white_point(self.image))


class CatalogMigrationTests(unittest.TestCase):
    def legacy_catalog(self, root: Path) -> Path:
        path = root / "library.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(catalog._SCHEMA)
            conn.execute("INSERT INTO meta VALUES('schema_version','8')")
            conn.execute("INSERT INTO sources(id,path,display_name,added_at) VALUES(1,'offline','Archive',0)")
            conn.execute("INSERT INTO files(id,source_id,relpath,filename,ext,added_at)"
                         " VALUES(10,1,'frame.jpg','frame.jpg','.jpg',0)")
            conn.execute("INSERT INTO images(id,file_id,display_name,created_at) VALUES(20,10,'frame.jpg',0)")
            conn.execute("INSERT INTO image_state(image_id,rating,grade_json,masks_json) VALUES(20,4,?,?)",
                         (json.dumps(LEGACY["grade"]), json.dumps(LEGACY["masks"])))
            conn.execute("INSERT INTO images(id,file_id,copy_ident,display_name,created_at)"
                         " VALUES(21,10,'copy','frame.jpg',0)")
            conn.execute("INSERT INTO image_state(image_id,rating) VALUES(21,0)")
            conn.execute("INSERT INTO versions(id,image_id,name,created,state_json) VALUES('v1',20,'Look','t',?)",
                         (json.dumps(LEGACY["versions"][0]),))
            blob = zlib.compress(json.dumps({"grade": {"whites": 0.7}, "masks": []}).encode(), 6)
            conn.execute("INSERT INTO history(image_id,seq,created,label,origin,state_blob)"
                         " VALUES(20,1,0,'Step','edit',?)", (blob,))
            conn.execute("INSERT INTO history(image_id,seq,created,label,origin,state_blob)"
                         " VALUES(20,2,0,'Broken','edit',?)", (b"not-zlib",))
            conn.commit()
        return path

    def test_schema_eight_edits_are_negated_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.legacy_catalog(Path(directory))
            migrated = catalog.Catalog(path)
            try:
                self.assertEqual(migrated.stats()["schema"], catalog.SCHEMA_VERSION)
                state = migrated.state_for(20)
                self.assertEqual(state["rating"], 4)
                self.assertEqual(state["grade"]["whites"], -0.3)
                self.assertEqual(state["grade"]["exposure"], 0.2)
                self.assertEqual(state["masks"][0]["grade"]["whites"], 0.25)
                self.assertEqual(state["masks"][1]["grade"], {"exposure": 0.5})
                self.assertEqual(state["versions"][0]["grade"]["whites"], -0.4)
                self.assertEqual(state["versions"][0]["masks"][0]["grade"]["whites"], -0.05)
                steps = migrated.history_for(20)
                by_label = {step["label"]: step["id"] for step in steps}
                self.assertEqual(migrated.history_state(by_label["Step"])["grade"]["whites"], -0.7)
                self.assertIsNone(migrated.history_state(by_label["Broken"]))
                self.assertIsNone(migrated.state_for(21).get("grade"))
                self.assertTrue(migrated.integrity_ok())
            finally:
                migrated.close()
            reopened = catalog.Catalog(path)
            try:
                self.assertEqual(reopened.state_for(20)["grade"]["whites"], -0.3)
                self.assertEqual(reopened.state_for(20)["versions"][0]["grade"]["whites"], -0.4)
            finally:
                reopened.close()

    def test_a_fresh_catalog_is_not_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            cat = catalog.Catalog(Path(directory) / "library.sqlite3")
            try:
                with mock.patch.object(catalog, "_upgrade_saved_edits",
                                       side_effect=AssertionError("must not run")):
                    cat.close()
                    catalog.Catalog(Path(directory) / "library.sqlite3").close()
            finally:
                cat.close()


class FolderStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.object(server, "FOLDER", self.root)
        patch.start(); self.addCleanup(patch.stop)
        server._STATE_CACHE_PATH = None

    def test_legacy_state_file_is_translated_and_rewritten_once(self):
        server.state_path().write_text(json.dumps({"images": {"a.jpg": LEGACY}}))
        state = server.load_state()
        self.assertEqual(state["images"]["a.jpg"]["grade"]["whites"], -0.3)
        self.assertEqual(state["images"]["a.jpg"]["masks"][0]["grade"]["whites"], 0.25)
        on_disk = json.loads(server.state_path().read_text())
        self.assertEqual(on_disk["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(on_disk["images"]["a.jpg"]["grade"]["whites"], -0.3)
        server._STATE_CACHE_PATH = None
        self.assertEqual(server.load_state()["images"]["a.jpg"]["grade"]["whites"], -0.3)

    def test_written_state_carries_the_marker(self):
        server.write_state({"images": {"a.jpg": {"grade": {"whites": 0.2}}}})
        on_disk = json.loads(server.state_path().read_text())
        self.assertEqual(on_disk["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        server._STATE_CACHE_PATH = None
        self.assertEqual(server.load_state()["images"]["a.jpg"]["grade"]["whites"], 0.2)


class PresetTests(unittest.TestCase):
    def test_legacy_user_preset_is_translated_and_stamped(self):
        preset = server.clean_preset({"name": "Old", "includeFilm": False,
                                      "grade": {"whites": 0.3},
                                      "masks": [{"type": "radial", "grade": {"whites": -0.2}}]})
        self.assertEqual(preset["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(preset["grade"]["whites"], -0.3)
        self.assertEqual(preset["masks"][0]["grade"]["whites"], 0.2)
        self.assertEqual(server.clean_preset(preset)["grade"]["whites"], -0.3)

    def test_legacy_look_and_community_recipe_are_translated(self):
        look = server.clean_preset({"name": "Shared", "scope": "look", "filmMode": "off",
                                    "grade": {"whites": 0.25}, "includedGrade": ["whites"]})
        self.assertEqual(look["grade"]["whites"], -0.25)
        self.assertEqual(look["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(server.clean_preset(look)["grade"]["whites"], -0.25)

    def test_bundled_presets_are_current_and_survive_export_import(self):
        import preset_library
        bundled = [p for p in preset_library.builtin_presets() if p["grade"].get("whites")]
        self.assertTrue(bundled)
        for look in bundled:
            self.assertEqual(server.clean_preset(look)["grade"]["whites"], look["grade"]["whites"])
            filename, _, content = preset_io.export_preset(preset_library.prepare_look(look), "lighttable")
            imported = preset_io._import_bytes(filename, content.encode())[0]
            self.assertEqual(server.clean_preset(imported)["grade"]["whites"], look["grade"]["whites"])

    def test_lightroom_whites_now_lands_in_the_brightening_direction(self):
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               '<rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
               ' crs:Whites2012="+40" crs:Blacks2012="-20"/></rdf:RDF></x:xmpmeta>')
        preset = preset_io.import_lightroom(xmp, "whites.xmp")
        self.assertEqual(preset["grade"]["whites"], 0.4)
        self.assertEqual(preset["grade"]["blacks"], -0.2)
        _, _, exported = preset_io.export_lightroom({"name": "x", "grade": {"whites": 0.4}})
        self.assertIn('crs:Whites2012="40.0"', exported)


class SidecarTests(unittest.TestCase):
    def native(self, document):
        parsed = xmp_sidecar.parse(document)
        return parsed, xmp_sidecar.as_edit_patch(parsed)

    def test_fresh_sidecar_round_trips_whites(self):
        document = xmp_sidecar.build_sidecar({"grade": {"whites": 0.3}, "masks": [{"id": "m"}]})
        parsed, patch = self.native(document)
        self.assertEqual(parsed["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(patch["grade"]["whites"], 0.3)

    def test_legacy_lighttable_sidecar_is_translated_and_foreign_files_are_not(self):
        legacy = xmp_sidecar.build_sidecar({"grade": {"whites": 0.3}, "masks": [{"id": "m"}]})
        legacy = legacy.replace(',&quot;editSchema&quot;:2', '')
        self.assertNotIn("editSchema", legacy)
        parsed, patch = self.native(legacy)
        self.assertEqual(parsed["editSchema"], 1)
        self.assertEqual(patch["grade"]["whites"], -0.3)
        foreign = legacy.replace("lighttable:edit=", "lighttable:other=")
        parsed, patch = self.native(foreign)
        self.assertIsNone(parsed["editSchema"])
        self.assertEqual(patch["grade"]["whites"], 0.3)

    def test_merge_translates_inherited_legacy_fields(self):
        legacy = xmp_sidecar.build_sidecar({"grade": {"whites": 0.3},
                                            "masks": [{"id": "m", "grade": {"whites": 0.1}}]})
        legacy = legacy.replace(',&quot;editSchema&quot;:2', '')
        merged = xmp_sidecar.merge_sidecar(legacy, {"grade": {"whites": 0.5}})
        parsed = xmp_sidecar.parse(merged)
        payload = json.loads(dict(parsed["crs"]) and self.payload(merged))
        self.assertEqual(payload["editSchema"], edit_schema.EDIT_SCHEMA_VERSION)
        self.assertEqual(payload["grade"]["whites"], 0.5)
        self.assertEqual(payload["masks"][0]["grade"]["whites"], -0.1)

    def payload(self, document):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(document.split("?>", 1)[1])
        key = f"{{{xmp_sidecar.NAMESPACES['lighttable']}}}edit"
        return next(node.get(key) for node in root.iter() if key in node.attrib)


if __name__ == "__main__":
    unittest.main()
