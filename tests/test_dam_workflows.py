"""DAM rules, migration, and additive tagging against real scratch catalogs."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import catalog
import dam_filters
import keyword_workflow
import library_workflow


class DAMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cat = catalog.Catalog(self.root / "library.sqlite3")
        self.source = self.cat.add_source(self.root / "photos")
        self.ids = []
        with self.cat.write() as conn:
            for index, focal in enumerate((50, 85, None)):
                name = f"frame{index}.NEF"
                self.cat.upsert_file(conn, self.source, {
                    "relpath": name, "filename": name, "ext": ".nef", "kind": "raw",
                    "header_hash": str(index), "camera_make": "Nikon", "camera_model": "Z6",
                    "lens": "Nikkor 50mm", "iso": 400, "focal_length": focal,
                    "aperture": 2.8, "shutter_seconds": 1 / 250,
                    "capture_time": "2026-12-31T23:59:59", "mtime_iso": "2025-01-01T00:00:00",
                })
                ident = self.cat.image_id_for(self.source, name)
                self.ids.append(ident)
                self.cat._save_state(conn, ident, {"rating": 4, "keywords": [f"Original {index}"]})

    def tearDown(self):
        self.cat.close()
        self.temp.cleanup()

    def test_combined_exif_rule_is_inclusive_and_excludes_missing_values(self):
        rule = {"camera": "nikon z6", "lens": "50mm", "focalLengthMin": 50,
                "focalLengthMax": 50, "isoMin": 400, "isoMax": 400,
                "apertureMin": 2.8, "apertureMax": 2.8,
                "shutterMin": "1/250", "shutterMax": "1/250",
                "dateFrom": "2026-01-01", "dateTo": "2026-12-31", "ratingMin": 4}
        result = self.cat.query({"filter": rule})
        self.assertEqual([item["id"] for item in result["items"]], [self.ids[0]])
        self.assertEqual(result["items"][0]["shutterSeconds"], .004)
        self.assertEqual(result["items"][0]["keywords"], ["Original 0"])
        clean = library_workflow.clean_collections([{"type": "smart", "rules": rule}])[0]["rules"]
        self.assertEqual(clean["focalLengthMin"], 50)
        collection = self.cat.add_collection("2026 50mm selects", kind="smart", rules=rule)
        # Additional live restrictions narrow the saved rule, including overlapping keys.
        self.assertEqual(self.cat.query({"scope": "collection", "collectionId": collection,
                                        "filter": {"ratingMin": 5}})["total"], 0)
        self.cat.save_state(self.ids[0], {"rating": 5})
        self.assertEqual(self.cat.query({"scope": "collection", "collectionId": collection,
                                        "filter": {"ratingMin": 5}})["total"], 1)

    def test_invalid_exposure_and_date_rules_are_rejected(self):
        for rules in ({"isoMin": "nan"}, {"shutterMin": "1/0"}, {"apertureMin": -1},
                      {"isoMin": 800, "isoMax": 100}, {"dateFrom": "2026-02-30"},
                      {"dateFrom": "2027-01-01", "dateTo": "2026-12-31"}):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                self.cat.query({"filter": rules})

    def test_cli_exposure_selectors_use_the_same_query_rules(self):
        from lighttable_cli.selectors import parse_where
        spec = parse_where(['focal-length=50', 'shutter=1/250', 'iso>=200', 'aperture<=4'])
        self.assertEqual([item['id'] for item in self.cat.query(spec)['items']], [self.ids[0]])
        for bad in ('iso=0', 'shutter=1/0', 'aperture=nan'):
            with self.subTest(selector=bad), self.assertRaises(ValueError):
                parse_where([bad])

    def test_ratings_do_not_touch_text_index_and_keyword_changes_do(self):
        sql = []
        self.cat.connection.set_trace_callback(sql.append)
        self.cat.save_state(self.ids[0], {"rating": 5, "status": "approved"})
        self.assertFalse(any("image_search" in statement for statement in sql))
        self.cat.save_state(self.ids[0], {"keywords": ["Birds > Heron"]})
        self.assertEqual(self.cat.query({"filter": {"query": "Heron"}})["total"], 1)
        self.assertEqual(self.cat.query({"filter": {"keyword": "Birds"}})["total"], 1)

    def test_batch_add_preserves_individual_tags_and_undo_survives_reopen(self):
        result = keyword_workflow.change(self.cat, self.ids[:2], ["Places > Boston"], "add")
        self.assertEqual(result["count"], 2)
        for index in (0, 1):
            self.assertEqual(self.cat.keywords_for(self.ids[index]), [f"Original {index}", "Places > Boston"])
        path = self.cat.path
        self.cat.close()
        self.cat = catalog.Catalog(path)
        keyword_workflow.change(self.cat, [], [], "undo", undo_id=result["undoId"])
        self.assertEqual(self.cat.keywords_for(self.ids[0]), ["Original 0"])
        self.assertEqual(self.cat.keywords_for(self.ids[1]), ["Original 1"])

    def test_keyword_undo_refuses_to_overwrite_later_changes_atomically(self):
        result = keyword_workflow.change(self.cat, self.ids[:2], ["Boston"], "add")
        self.cat.save_state(self.ids[1], {"keywords": ["Later"]})
        with self.assertRaisesRegex(ValueError, "changed after"):
            keyword_workflow.change(self.cat, [], [], "undo", undo_id=result["undoId"])
        self.assertIn("Boston", self.cat.keywords_for(self.ids[0]))
        self.assertEqual(self.cat.keywords_for(self.ids[1]), ["Later"])

    def test_batch_remove_is_case_insensitive_and_invalid_target_rolls_back(self):
        keyword_workflow.change(self.cat, self.ids[:2], ["original 0"], "remove")
        self.assertEqual(self.cat.keywords_for(self.ids[0]), [])
        self.assertEqual(self.cat.keywords_for(self.ids[1]), ["Original 1"])
        with self.assertRaises(ValueError):
            keyword_workflow.change(self.cat, [self.ids[0], 99999], ["No"], "add")
        self.assertEqual(self.cat.keywords_for(self.ids[0]), [])

    def test_schema_five_migration_retains_edits_and_rebuilds_fts_identity(self):
        other = self.root / "old.sqlite3"
        schema = catalog._SCHEMA
        for line in schema.splitlines():
            if any(line.strip().startswith(column + ' ') for _, column, _ in dam_filters.EXPOSURE_FIELDS):
                schema = schema.replace(line + '\n', '')
        conn = sqlite3.connect(other)
        conn.executescript(schema)
        conn.execute("INSERT INTO meta VALUES('schema_version','5')")
        conn.execute("INSERT INTO sources(id,path,display_name,added_at) VALUES(1,'offline','Archive',0)")
        conn.execute("INSERT INTO files(id,source_id,relpath,filename,ext,added_at) VALUES(10,1,'a.jpg','a.jpg','.jpg',0)")
        conn.execute("INSERT INTO images(id,file_id,display_name,created_at) VALUES(20,10,'a.jpg',0)")
        conn.execute("INSERT INTO image_state(image_id,rating) VALUES(20,5)")
        conn.execute("INSERT INTO image_search(rowid,image_id,filename) VALUES(2,20,'a.jpg')")
        conn.commit(); conn.close()
        migrated = catalog.Catalog(other)
        self.assertEqual(migrated.state_for(20)["rating"], 5)
        self.assertEqual(tuple(migrated.connection.execute('SELECT rowid,image_id FROM image_search').fetchone()), (20, 20))
        self.assertEqual(migrated.query({"filter": {"query": "a.jpg"}})["total"], 1)
        self.assertTrue(migrated.integrity_ok())
        migrated.close()


if __name__ == "__main__":
    unittest.main()
