"""The two independently developed schema-6 layouts converge without data loss."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import catalog
import dam_filters


class SchemaMergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def legacy_catalog(self, variant):
        path = self.root / (variant + ".sqlite3")
        removed = ({column for _, column, _ in dam_filters.EXPOSURE_FIELDS}
                   if variant == "identity" else {"content_hash", "content_signature"})
        schema = "\n".join(line for line in catalog._SCHEMA.splitlines()
                           if not any(line.strip().startswith(column + " ") for column in removed))
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(schema)
            conn.execute("INSERT INTO meta VALUES('schema_version','6')")
            conn.execute("INSERT INTO sources(id,path,display_name,added_at) VALUES(1,'offline','Archive',0)")
            conn.execute("INSERT INTO files(id,source_id,relpath,filename,ext,added_at)"
                         " VALUES(10,1,'frame.jpg','frame.jpg','.jpg',0)")
            conn.execute("INSERT INTO images(id,file_id,display_name,created_at) VALUES(20,10,'frame.jpg',0)")
            conn.execute("INSERT INTO image_state(image_id,rating) VALUES(20,5)")
            conn.execute("INSERT INTO keywords(id,name,path) VALUES(1,'Heron','Birds > Heron')")
            conn.execute("INSERT INTO image_keywords(image_id,keyword_id) VALUES(20,1)")
            conn.execute("INSERT INTO image_search(rowid,image_id,filename,keywords) VALUES(?,20,'frame.jpg','Heron')",
                         (2 if variant == "identity" else 20,))
            if variant == "identity":
                conn.execute("UPDATE files SET content_hash='verified-digest', content_signature='verified-signature'")
            else:
                conn.execute("UPDATE files SET iso=400,focal_length=50,aperture=2.8,shutter_seconds=0.004")
            conn.commit()
        return path

    def test_both_schema_six_layouts_preserve_state_and_converge(self):
        for variant in ("identity", "dam"):
            with self.subTest(variant=variant):
                path = self.legacy_catalog(variant)
                with mock.patch.object(catalog, "_rebuild_search_index", wraps=catalog._rebuild_search_index) as rebuild:
                    migrated = catalog.Catalog(path)
                try:
                    self.assertEqual(migrated.stats()["schema"], catalog.SCHEMA_VERSION)
                    self.assertEqual(migrated.state_for(20)["rating"], 5)
                    self.assertEqual(migrated.keywords_for(20), ["Birds > Heron"])
                    self.assertEqual(tuple(migrated.connection.execute(
                        "SELECT rowid,image_id FROM image_search").fetchone()), (20, 20))
                    self.assertEqual(migrated.query({"filter": {"query": "Heron"}})["total"], 1)
                    row = migrated.connection.execute("SELECT * FROM files WHERE id=10").fetchone()
                    if variant == "identity":
                        self.assertEqual(row["content_hash"], "verified-digest")
                        self.assertEqual(row["content_signature"], "verified-signature")
                        self.assertIsNone(row["iso"])
                    else:
                        self.assertEqual(row["iso"], 400)
                        self.assertEqual(row["focal_length"], 50)
                        self.assertIsNone(row["content_hash"])
                        self.assertIsNone(row["content_signature"])
                    self.assertEqual(rebuild.call_count, int(variant == "identity"))
                    self.assertTrue(migrated.integrity_ok())
                finally:
                    migrated.close()
                with mock.patch.object(catalog, "_rebuild_search_index", side_effect=AssertionError("repeat rebuild")):
                    reopened = catalog.Catalog(path)
                    reopened.close()

    def test_failed_combined_migration_rolls_back_columns_and_schema_marker(self):
        path = self.legacy_catalog("identity")
        with mock.patch.object(catalog, "_rebuild_search_index", side_effect=OSError("interrupted migration")):
            with self.assertRaisesRegex(OSError, "interrupted migration"):
                catalog.Catalog(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "6")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            self.assertNotIn("iso", columns)
            self.assertIn("content_hash", columns)
            self.assertEqual(conn.execute("SELECT rating FROM image_state WHERE image_id=20").fetchone()[0], 5)
