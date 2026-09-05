"""Importing a Lightroom Classic catalog.

Every case runs against a synthetic `.lrcat` built here with sqlite3, carrying
only the tables and columns the importer reads. That keeps the fixture honest
about what the mapping depends on: a column these tests do not create is a
column `catalog_import` must not require.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import catalog as catalog_module
import catalog_import
import catalog_scan


OUTSIDE_ROOT = "/lightroom-fixture-elsewhere"

MASTER_SETTINGS = """s = {
\tCropAngle = 2.5,
\tCropBottom = 0.9,
\tCropLeft = 0.1,
\tCropRight = 0.9,
\tCropTop = 0.1,
\tExposure2012 = 0.75,
\tContrast2012 = 25,
\tHasCrop = true,
\tProcessVersion = "11.0",
\tConvertToGrayscale = true,
}"""

COPY_SETTINGS = """s = {
\tExposure2012 = -0.5,
\tProcessVersion = "11.0",
}"""

HISTORY_SETTINGS = """s = {
\tExposure2012 = 0.25,
}"""

IMAGE_XMP = """<?xml version="1.0"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Harbour at dawn</rdf:li>
   </rdf:Alt></dc:title>
   <dc:creator><rdf:Seq><rdf:li>A Photographer</rdf:li></rdf:Seq></dc:creator>
   <photoshop:City>Kyoto</photoshop:City>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

SMART_RATING_RULE = """s = {
\t{
\t\tcriteria = "rating",
\t\toperation = ">=",
\t\tvalue = 4,
\t\tvalue2 = 0,
\t},
\tcombine = "intersect",
}"""

SMART_LENS_RULE = """s = {
\t{
\t\tcriteria = "lens",
\t\toperation = "beginsWith",
\t\tvalue = "XF",
\t},
}"""

_TABLES = {
    "Adobe_variablesTable": "id_local INTEGER, name TEXT, value TEXT",
    "AgLibraryRootFolder": "id_local INTEGER, absolutePath TEXT, name TEXT",
    "AgLibraryFolder": "id_local INTEGER, rootFolder INTEGER,"
                       " pathFromRoot TEXT",
    "AgLibraryFile": "id_local INTEGER, folder INTEGER, baseName TEXT,"
                     " extension TEXT, idx_filename TEXT",
    "Adobe_images": "id_local INTEGER, rootFile INTEGER, captureTime TEXT,"
                    " rating REAL, pick REAL, colorLabels TEXT,"
                    " fileFormat TEXT, masterImage INTEGER, copyName TEXT",
    "AgLibraryKeyword": "id_local INTEGER, name TEXT, parent INTEGER",
    "AgLibraryKeywordImage": "id_local INTEGER, image INTEGER, tag INTEGER",
    "AgLibraryCollection": "id_local INTEGER, name TEXT, creationId TEXT,"
                           " parent INTEGER, content TEXT",
    "AgLibraryCollectionImage": "id_local INTEGER, collection INTEGER,"
                                " image INTEGER, positionInCollection REAL",
    "AgLibraryFolderStack": "id_local INTEGER, collapsed INTEGER, text TEXT",
    "AgLibraryFolderStackImage": "id_local INTEGER, stack INTEGER,"
                                 " image INTEGER, position INTEGER",
    "AgLibraryIPTC": "id_local INTEGER, image INTEGER, caption TEXT,"
                     " copyright TEXT",
    "AgHarvestedExifMetadata": "id_local INTEGER, image INTEGER,"
                               " gpsLatitude REAL, gpsLongitude REAL",
    "Adobe_AdditionalMetadata": "id_local INTEGER, image INTEGER, xmp TEXT",
    "Adobe_imageDevelopSettings": "id_local INTEGER, image INTEGER, text TEXT,"
                                  " croppedWidth REAL, croppedHeight REAL",
    "Adobe_libraryImageDevelopHistoryStep":
        "id_local INTEGER, image INTEGER, name TEXT, dateCreated REAL,"
        " text TEXT",
}


def build_lrcat(path: Path, root: Path, *, drop: set[str] | None = None,
                outside: str = OUTSIDE_ROOT) -> None:
    """Write a small Lightroom catalog describing the photos under `root`."""
    drop = set(drop or ())
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(path)

    def create(table: str) -> None:
        if table not in drop:
            conn.execute(f"CREATE TABLE {table} ({_TABLES[table]})")

    def insert(table: str, columns: str, rows) -> None:
        if table in drop:
            return
        marks = ",".join("?" * len(columns.split(",")))
        conn.executemany(
            f"INSERT INTO {table}({columns}) VALUES({marks})", rows)

    for table in _TABLES:
        create(table)

    insert("Adobe_variablesTable", "id_local,name,value",
           [(1, "Adobe_DBVersion", "1200000")])
    insert("AgLibraryRootFolder", "id_local,absolutePath,name",
           [(1, f"{root}/", root.name), (2, f"{outside}/", "elsewhere")])
    insert("AgLibraryFolder", "id_local,rootFolder,pathFromRoot",
           [(1, 1, ""), (2, 1, "sub/"), (3, 2, "")])
    insert("AgLibraryFile", "id_local,folder,baseName,extension,idx_filename",
           [(1001, 1, "a", "jpg", "a.jpg"),
            (1002, 2, "b", "dng", "b.dng"),
            (1003, 2, "c", "jpg", "c.jpg"),
            (1004, 3, "outside", "jpg", "outside.jpg")])
    insert("Adobe_images",
           "id_local,rootFile,captureTime,rating,pick,colorLabels,fileFormat,"
           "masterImage,copyName",
           [(101, 1001, "2024-05-01T12:00:00", 4.0, 1.0, "Blue", "JPG",
             None, None),
            (102, 1002, "2024-05-02T09:30:00", 0.0, -1.0, "", "RAW",
             None, None),
            (103, 1003, "2024-05-03T16:45:00", 2.0, 0.0, "Purple", "JPG",
             None, None),
            (104, 1003, "2024-05-03T16:45:00", 5.0, 1.0, "Chartreuse", "JPG",
             103, "Copy 1"),
            (105, 1004, "2024-05-04T08:00:00", 3.0, 0.0, "Red", "JPG",
             None, None)])
    insert("AgLibraryKeyword", "id_local,name,parent",
           [(10, "", None), (11, "Travel", 10), (12, "Japan", 11),
            (13, "Portrait", 10)])
    insert("AgLibraryKeywordImage", "id_local,image,tag",
           [(1, 101, 12), (2, 101, 13), (3, 103, 11)])
    insert("AgLibraryCollection", "id_local,name,creationId,parent,content",
           [(201, "Travel", "com.adobe.ag.library.group", None, None),
            (202, "Best of 2024", "com.adobe.ag.library.collection", 201,
             None),
            (203, "Four stars", "com.adobe.ag.library.smart_collection", None,
             SMART_RATING_RULE),
            (204, "Lens hunt", "com.adobe.ag.library.smart_collection", None,
             SMART_LENS_RULE)])
    insert("AgLibraryCollectionImage",
           "id_local,collection,image,positionInCollection",
           [(1, 202, 101, 0.0), (2, 202, 103, 1.0)])
    insert("AgLibraryFolderStack", "id_local,collapsed,text",
           [(301, 1, "Bracket")])
    insert("AgLibraryFolderStackImage", "id_local,stack,image,position",
           [(1, 301, 101, 0), (2, 301, 102, 1)])
    insert("AgLibraryIPTC", "id_local,image,caption,copyright",
           [(1, 101, "Fishing boats leaving", "(c) 2024 A Photographer")])
    insert("AgHarvestedExifMetadata", "id_local,image,gpsLatitude,"
           "gpsLongitude", [(1, 101, 35.0116, 135.7681)])
    xmp = IMAGE_XMP.encode("utf-8")
    compressed_xmp = sqlite3.Binary(
        len(xmp).to_bytes(4, "big") + zlib.compress(xmp))
    insert("Adobe_AdditionalMetadata", "id_local,image,xmp",
           [(1, 101, compressed_xmp)])
    insert("Adobe_imageDevelopSettings",
           "id_local,image,text,croppedWidth,croppedHeight",
           [(1, 101, MASTER_SETTINGS, 4000.0, 3000.0),
            (2, 104, COPY_SETTINGS, 0.0, 0.0)])
    insert("Adobe_libraryImageDevelopHistoryStep",
           "id_local,image,name,dateCreated,text",
           [(1, 101, "Import", 1.0, ""),
            (2, 101, "Exposure", 2.0, HISTORY_SETTINGS)])
    conn.commit()
    conn.close()


class ImportFixture(unittest.TestCase):
    """A scanned source, plus a catalog that describes the same photos."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)
        self.root = self.directory / "photos"
        for relpath in ("a.jpg", "sub/b.dng", "sub/c.jpg"):
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relpath.encode() * 64)
        self.catalog = catalog_module.Catalog(
            self.directory / "library.sqlite3")
        self.addCleanup(self.catalog.close)
        self.source_id = self.catalog.add_source(self.root)
        catalog_scan.scan_source(self.catalog, self.source_id)
        self.lrcat = self.directory / "Fixture.lrcat"
        build_lrcat(self.lrcat, self.root.resolve())

    # -------------------------------------------------------------- helpers

    def image(self, relpath: str, copy_ident: str | None = None) -> int:
        image_id = self.catalog.image_id_for(self.source_id, relpath,
                                             copy_ident)
        self.assertIsNotNone(image_id, f"no catalog image for {relpath}")
        return int(image_id)

    def rebuild(self, **kwargs) -> None:
        build_lrcat(self.lrcat, self.root.resolve(), **kwargs)

    def collection_named(self, name: str) -> dict:
        for item in self.catalog.collections():
            if item["name"] == name:
                return item
        self.fail(f"no collection named {name}")


class InspectTests(ImportFixture):
    def test_reports_counts_and_roots(self):
        summary = catalog_import.inspect(self.lrcat)
        self.assertEqual(summary["version"], "1200000")
        self.assertEqual(summary["images"], 5)
        self.assertEqual(summary["keywords"], 3)
        self.assertEqual(summary["collections"], 4)
        self.assertEqual(summary["stacks"], 1)
        self.assertTrue(summary["hasDevelopSettings"])
        self.assertTrue(summary["hasHistory"])
        roots = {root["path"]: root for root in summary["roots"]}
        self.assertIn(str(self.root.resolve()), roots)
        self.assertTrue(roots[str(self.root.resolve())]["exists"])
        self.assertEqual(roots[str(self.root.resolve())]["files"], 3)
        self.assertFalse(roots[OUTSIDE_ROOT]["exists"])
        self.assertEqual(roots[OUTSIDE_ROOT]["files"], 1)
        self.assertTrue(any(OUTSIDE_ROOT in text
                            for text in summary["warnings"]))

    def test_does_not_import_anything(self):
        catalog_import.inspect(self.lrcat)
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertEqual(state["rating"], 0)
        self.assertEqual(state["keywords"], [])
        self.assertEqual(self.catalog.collections(), [])

    def test_rejects_a_file_that_is_not_a_catalog(self):
        stray = self.directory / "notes.lrcat"
        stray.write_bytes(b"this is not a database")
        with self.assertRaises(catalog_import.UnsupportedCatalog):
            catalog_import.inspect(stray)

    def test_rejects_a_missing_file(self):
        with self.assertRaises(catalog_import.UnsupportedCatalog):
            catalog_import.inspect(self.directory / "absent.lrcat")


class FullImportTests(ImportFixture):
    def setUp(self) -> None:
        super().setUp()
        self.result = catalog_import.import_catalog(self.catalog, self.lrcat)

    def test_counts(self):
        self.assertEqual(self.result["images"], 5)
        self.assertEqual(self.result["matched"], 4)
        self.assertEqual(self.result["unmatched"], 1)
        self.assertEqual(self.result["keywords"], 3)
        self.assertEqual(self.result["collections"], 4)
        self.assertEqual(self.result["stacks"], 1)
        self.assertEqual(self.result["history"], 0)
        self.assertFalse(self.result["cancelled"])

    def test_ratings_flags_and_labels(self):
        first = self.catalog.state_for(self.image("a.jpg"))
        self.assertEqual(first["rating"], 4)
        self.assertEqual(first["status"], "approved")
        self.assertEqual(first["label"], "blue")
        rejected = self.catalog.state_for(self.image("sub/b.dng"))
        self.assertEqual(rejected["status"], "skipped")
        self.assertEqual(rejected["rating"], 0)
        self.assertEqual(rejected["label"], "none")
        third = self.catalog.state_for(self.image("sub/c.jpg"))
        self.assertEqual(third["rating"], 2)
        self.assertEqual(third["status"], "pending")
        self.assertEqual(third["label"], "purple")

    def test_custom_label_is_reported_not_guessed(self):
        copy = self.catalog.state_for(self.image("sub/c.jpg", "lr-104"))
        self.assertEqual(copy["label"], "none")
        self.assertIn("custom colour labels", self.result["skipped"])
        self.assertTrue(any("Chartreuse" in text
                            for text in self.result["warnings"]))

    def test_keyword_hierarchy(self):
        self.assertEqual(self.catalog.keywords_for(self.image("a.jpg")),
                         ["Portrait", "Travel > Japan"])
        self.assertEqual(self.catalog.keywords_for(self.image("sub/c.jpg")),
                         ["Travel"])
        paths = {row["path"] for row in self.catalog.keyword_tree()}
        self.assertEqual(paths, {"Travel", "Travel > Japan", "Portrait"})

    def test_collections_and_sets(self):
        parent = self.collection_named("Travel")
        child = self.collection_named("Best of 2024")
        self.assertEqual(child["parentId"], parent["id"])
        self.assertEqual(child["type"], "regular")
        self.assertEqual(child["count"], 2)
        members = {row["image_id"] for row in self.catalog.connection.execute(
            "SELECT image_id FROM collection_images WHERE collection_id=?",
            (child["id"],)).fetchall()}
        self.assertEqual(members, {self.image("a.jpg"),
                                   self.image("sub/c.jpg")})

    def test_smart_collection_rules_that_convert(self):
        smart = self.collection_named("Four stars")
        self.assertEqual(smart["type"], "smart")
        self.assertEqual(smart["rules"], {"ratingMin": 4})
        found = self.catalog.query({"scope": "collection",
                                    "collectionId": smart["id"]})
        # The four-star rule also catches the five-star virtual copy, which is
        # the point of a smart collection: it is a query, not a member list.
        self.assertEqual({item["id"] for item in found["items"]},
                         {self.image("a.jpg"),
                          self.image("sub/c.jpg", "lr-104")})

    def test_smart_collection_rules_that_do_not(self):
        empty = self.collection_named("Lens hunt")
        self.assertEqual(empty["type"], "regular")
        self.assertEqual(empty["count"], 0)
        self.assertIn("smart collection rules", self.result["skipped"])
        self.assertTrue(any("Lens hunt" in text
                            for text in self.result["warnings"]))

    def test_stacks(self):
        rows = self.catalog.connection.execute(
            "SELECT s.name, si.image_id, si.position FROM stacks s"
            " JOIN stack_images si ON si.stack_id=s.id"
            " ORDER BY si.position").fetchall()
        self.assertEqual([row["name"] for row in rows], ["Bracket", "Bracket"])
        self.assertEqual([row["image_id"] for row in rows],
                         [self.image("a.jpg"), self.image("sub/b.dng")])

    def test_iptc_gps_and_xmp(self):
        fields = self.catalog.iptc_for(self.image("a.jpg"))
        self.assertEqual(fields["caption"], "Fishing boats leaving")
        self.assertEqual(fields["copyright"], "(c) 2024 A Photographer")
        self.assertAlmostEqual(fields["gps_lat"], 35.0116, places=4)
        self.assertAlmostEqual(fields["gps_lon"], 135.7681, places=4)
        if catalog_import.xmp_sidecar is not None:
            self.assertEqual(fields["title"], "Harbour at dawn")
            self.assertEqual(fields["creator"], "A Photographer")
            self.assertEqual(fields["city"], "Kyoto")

    def test_capture_time_fills_an_empty_column(self):
        row = self.catalog.connection.execute(
            "SELECT f.capture_time FROM files f JOIN images i"
            " ON i.file_id=f.id WHERE i.id=?",
            (self.image("a.jpg"),)).fetchone()
        self.assertEqual(row["capture_time"], "2024-05-01T12:00:00")

    def test_develop_settings_crop_and_film_off(self):
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertAlmostEqual(state["grade"]["exposure"], 0.75, places=5)
        self.assertAlmostEqual(state["grade"]["contrast"], 0.25, places=5)
        self.assertEqual(state["crop"], {"x": 0.1, "y": 0.1,
                                         "w": 0.8, "h": 0.8})
        self.assertAlmostEqual(state["optics"]["rotate"], 2.5, places=5)
        self.assertIs(state["params"]["profile_enabled"], False)

    def test_unsupported_develop_operations_are_named(self):
        self.assertIn("black and white mixer", self.result["skipped"])

    def test_an_image_without_settings_keeps_its_params(self):
        state = self.catalog.state_for(self.image("sub/b.dng"))
        self.assertIsNone(state["params"])
        self.assertIsNone(state["grade"])

    def test_roots_report_matched_and_unmatched(self):
        roots = {root["path"]: root for root in self.result["roots"]}
        inside = roots[str(self.root.resolve())]
        self.assertEqual(inside["matched"], 3)
        self.assertEqual(inside["unmatched"], 0)
        self.assertEqual(inside["sourceId"], self.source_id)
        outside = roots[OUTSIDE_ROOT]
        self.assertIsNone(outside["sourceId"])
        self.assertEqual(outside["unmatched"], 1)


class UnmatchedTests(ImportFixture):
    def test_a_root_outside_every_source_is_counted(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["skipped"]["files outside every source"], 1)
        self.assertTrue(any(OUTSIDE_ROOT in text
                            for text in result["warnings"]))
        self.assertEqual([source["id"] for source in self.catalog.sources()],
                         [self.source_id])

    def test_a_file_the_scan_has_not_seen_is_counted(self):
        self.catalog.connection.execute("SELECT 1")
        with self.catalog.write() as conn:
            conn.execute("DELETE FROM files WHERE relpath='sub/b.dng'")
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["unmatched"], 2)
        self.assertEqual(result["skipped"]["files not yet scanned"], 1)

    def test_a_moved_root_can_be_repointed(self):
        moved = self.directory / "moved.lrcat"
        build_lrcat(moved, Path("/lightroom-fixture-old-home"))
        result = catalog_import.import_catalog(
            self.catalog, moved,
            root_map={"/lightroom-fixture-old-home": str(self.root.resolve())})
        self.assertEqual(result["matched"], 4)
        self.assertEqual(
            self.catalog.state_for(self.image("a.jpg"))["rating"], 4)


class ConflictPolicyTests(ImportFixture):
    def setUp(self) -> None:
        super().setUp()
        self.existing = self.image("a.jpg")
        self.catalog.save_state(self.existing,
                                {"grade": {"exposure": -1.25}, "rating": 1})

    def test_skip_leaves_an_edited_image_alone(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        state = self.catalog.state_for(self.existing)
        self.assertAlmostEqual(state["grade"]["exposure"], -1.25, places=5)
        self.assertEqual(state["rating"], 1)
        self.assertEqual(state["label"], "none")
        self.assertIsNone(state["crop"])
        self.assertEqual(result["skipped"]["images already edited"], 1)
        # An unedited image in the same catalog still arrives.
        self.assertEqual(
            self.catalog.state_for(self.image("sub/c.jpg"))["rating"], 2)

    def test_overwrite_replaces_the_edit(self):
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      options={"conflict": "overwrite"})
        state = self.catalog.state_for(self.existing)
        self.assertAlmostEqual(state["grade"]["exposure"], 0.75, places=5)
        self.assertEqual(state["rating"], 4)
        self.assertEqual(state["label"], "blue")
        self.assertEqual(state["crop"], {"x": 0.1, "y": 0.1,
                                         "w": 0.8, "h": 0.8})

    def test_merge_keeps_the_edit_and_takes_the_metadata(self):
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      options={"conflict": "merge"})
        state = self.catalog.state_for(self.existing)
        self.assertAlmostEqual(state["grade"]["exposure"], -1.25, places=5)
        self.assertEqual(state["rating"], 4)
        self.assertEqual(state["label"], "blue")
        self.assertEqual(self.catalog.keywords_for(self.existing),
                         ["Portrait", "Travel > Japan"])

    def test_skipped_images_still_join_their_collections(self):
        catalog_import.import_catalog(self.catalog, self.lrcat)
        child = self.collection_named("Best of 2024")
        self.assertEqual(child["count"], 2)


class SchemaDriftTests(ImportFixture):
    def test_a_missing_stack_table_is_a_warning_not_a_crash(self):
        self.rebuild(drop={"AgLibraryFolderStack"})
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["stacks"], 0)
        self.assertEqual(result["skipped"]["stacks"], 1)
        self.assertTrue(any("stack" in text for text in result["warnings"]))
        self.assertEqual(result["matched"], 4)
        self.assertEqual(result["collections"], 4)
        self.assertEqual(
            self.catalog.state_for(self.image("a.jpg"))["rating"], 4)
        self.assertEqual(self.catalog.keywords_for(self.image("sub/c.jpg")),
                         ["Travel"])

    def test_a_missing_keyword_table_is_a_warning_not_a_crash(self):
        self.rebuild(drop={"AgLibraryKeywordImage"})
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["keywords"], 0)
        self.assertEqual(result["skipped"]["keywords"], 1)
        self.assertEqual(result["matched"], 4)
        self.assertEqual(self.catalog.keywords_for(self.image("a.jpg")), [])

    def test_a_missing_develop_table_is_a_warning_not_a_crash(self):
        self.rebuild(drop={"Adobe_imageDevelopSettings"})
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["skipped"]["develop settings"], 1)
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertIsNone(state["grade"])
        self.assertEqual(state["rating"], 4)

    def test_optional_image_columns_may_be_absent(self):
        conn = sqlite3.connect(self.lrcat)
        try:
            conn.execute("CREATE TABLE trimmed AS SELECT id_local, rootFile,"
                         " rating FROM Adobe_images")
            conn.execute("DROP TABLE Adobe_images")
            conn.execute("ALTER TABLE trimmed RENAME TO Adobe_images")
            conn.commit()
        finally:
            conn.close()
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        # Without masterImage there is no virtual copy, so one row fewer.
        self.assertEqual(result["matched"], 4)
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertEqual(state["rating"], 4)
        self.assertEqual(state["status"], "pending")


class VirtualCopyTests(ImportFixture):
    def test_a_copy_gets_its_own_image_and_state(self):
        catalog_import.import_catalog(self.catalog, self.lrcat)
        master = self.image("sub/c.jpg")
        copy = self.image("sub/c.jpg", "lr-104")
        self.assertNotEqual(master, copy)
        row = self.catalog.image_row(copy)
        self.assertEqual(row["virtual"], 1)
        self.assertEqual(row["display_name"], "Copy 1")
        self.assertEqual(row["relpath"], "sub/c.jpg")
        self.assertEqual(self.catalog.state_for(master)["rating"], 2)
        self.assertEqual(self.catalog.state_for(copy)["rating"], 5)
        self.assertAlmostEqual(
            self.catalog.state_for(copy)["grade"]["exposure"], -0.5, places=5)
        self.assertIsNone(self.catalog.state_for(master)["grade"])

    def test_importing_twice_does_not_duplicate_the_copy(self):
        catalog_import.import_catalog(self.catalog, self.lrcat)
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      options={"conflict": "overwrite"})
        rows = self.catalog.connection.execute(
            "SELECT COUNT(*) AS n FROM images WHERE copy_ident='lr-104'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)


class ReadOnlyTests(ImportFixture):
    def test_the_source_catalog_is_untouched(self):
        before = self.lrcat.read_bytes()
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      options={"history": True})
        self.assertEqual(self.lrcat.read_bytes(), before)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(
                self.lrcat.with_name(self.lrcat.name + suffix).exists(),
                f"the importer created a {suffix} file beside the catalog")

    def test_a_write_ahead_log_is_read_with_the_catalog(self):
        conn = sqlite3.connect(self.lrcat)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE Adobe_images SET rating=5 WHERE id_local=101")
        conn.commit()
        try:
            self.assertTrue(
                self.lrcat.with_name(self.lrcat.name + "-wal").exists(),
                "the fixture did not produce a write-ahead log")
            catalog_import.import_catalog(self.catalog, self.lrcat)
        finally:
            conn.close()
        self.assertEqual(
            self.catalog.state_for(self.image("a.jpg"))["rating"], 5)

    def test_masks_and_ai_data_are_reported_as_skipped(self):
        (self.directory / "Fixture.lrcat-data").mkdir()
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertIn("masks and AI data", result["skipped"])
        self.assertTrue(any("masks and AI data" in text
                            for text in result["warnings"]))


class HistoryTests(ImportFixture):
    def test_history_is_absent_by_default(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        self.assertEqual(result["history"], 0)
        self.assertEqual(self.catalog.history_for(self.image("a.jpg")), [])

    def test_history_arrives_when_asked_for(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat,
                                               options={"history": True})
        self.assertEqual(result["history"], 2)
        steps = self.catalog.history_for(self.image("a.jpg"))
        self.assertEqual([step["label"] for step in steps],
                         ["Exposure", "Import"])
        self.assertEqual({step["origin"] for step in steps}, {"import"})
        newest = self.catalog.history_state(steps[0]["id"])
        self.assertTrue(newest["imported"])
        self.assertAlmostEqual(newest["grade"]["exposure"], 0.25, places=5)


class ProgressAndCancelTests(ImportFixture):
    def test_progress_reports_stages(self):
        seen = []
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      progress=seen.append)
        stages = {event["stage"] for event in seen}
        self.assertLessEqual({"files", "images", "collections", "stacks"},
                             stages)
        for event in seen:
            self.assertLessEqual(event["done"], event["total"])

    def test_cancelling_stops_the_import(self):
        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        result = catalog_import.import_catalog(self.catalog, self.lrcat,
                                               should_cancel=should_cancel)
        self.assertTrue(result["cancelled"])
        self.assertLess(result["matched"], result["images"])
        self.assertEqual(result["collections"], 0)
        self.assertEqual(result["stacks"], 0)


class OptionTests(ImportFixture):
    def test_categories_can_be_turned_off(self):
        result = catalog_import.import_catalog(
            self.catalog, self.lrcat,
            options={"keywords": False, "collections": False,
                     "stacks": False, "develop": False})
        self.assertEqual(result["keywords"], 0)
        self.assertEqual(result["collections"], 0)
        self.assertEqual(result["stacks"], 0)
        self.assertEqual(self.catalog.collections(), [])
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertEqual(state["keywords"], [])
        self.assertIsNone(state["grade"])
        self.assertEqual(state["rating"], 4)

    def test_film_can_be_left_on(self):
        catalog_import.import_catalog(self.catalog, self.lrcat,
                                      options={"filmOff": False})
        state = self.catalog.state_for(self.image("a.jpg"))
        self.assertIsNone(state["params"])
        self.assertAlmostEqual(state["grade"]["exposure"], 0.75, places=5)

    def test_an_unknown_conflict_policy_falls_back_to_skip(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat,
                                               options={"conflict": "burn"})
        self.assertEqual(result["options"]["conflict"], "skip")


class VideoTests(ImportFixture):
    """Basic adjustments on a clip are named, not half-applied."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / "clip.mp4").write_bytes(b"clip" * 64)
        catalog_scan.scan_source(self.catalog, self.source_id)
        conn = sqlite3.connect(self.lrcat)
        try:
            conn.execute(
                "INSERT INTO AgLibraryFile(id_local,folder,baseName,extension,"
                "idx_filename) VALUES(1005,1,'clip','mp4','clip.mp4')")
            conn.execute(
                "INSERT INTO Adobe_images(id_local,rootFile,captureTime,"
                "rating,pick,colorLabels,fileFormat,masterImage,copyName)"
                " VALUES(106,1005,'2024-05-05T10:00:00',3.0,0.0,'','VIDEO',"
                "NULL,NULL)")
            conn.execute(
                "INSERT INTO Adobe_imageDevelopSettings(id_local,image,text,"
                "croppedWidth,croppedHeight) VALUES(3,106,?,0.0,0.0)",
                (MASTER_SETTINGS,))
            conn.commit()
        finally:
            conn.close()

    def test_video_edits_are_skipped_but_the_metadata_arrives(self):
        result = catalog_import.import_catalog(self.catalog, self.lrcat)
        clip = self.image("clip.mp4")
        state = self.catalog.state_for(clip)
        self.assertEqual(state["rating"], 3)
        self.assertIsNone(state["grade"])
        self.assertIsNone(state["params"])
        self.assertEqual(result["skipped"]["video edits"], 1)


class DevelopMapperTests(unittest.TestCase):
    """The crs mapper is reached through whichever entry point exists."""

    def test_uses_the_shared_mapper_when_it_exists(self):
        import preset_io

        self.assertTrue(callable(getattr(preset_io, "map_crs_settings", None)),
                        "map_crs_settings is the expected entry point")
        patch = catalog_import._develop_from_lua(MASTER_SETTINGS)
        self.assertAlmostEqual(patch["grade"]["exposure"], 0.75, places=5)

    def test_falls_back_to_the_preset_importer(self):
        import preset_io

        seen = {}

        def import_lightroom(content, filename):
            seen["filename"] = filename
            return {"grade": {"exposure": 0.5},
                    "conversion": {"ignored": ["geometry or crop",
                                               "local masks"]}}

        with mock.patch.object(preset_io, "map_crs_settings", None), \
                mock.patch.object(preset_io, "import_lightroom",
                                  import_lightroom):
            patch = catalog_import._develop_from_lua(MASTER_SETTINGS)
        self.assertEqual(seen["filename"], "develop.xmp")
        self.assertEqual(patch["grade"], {"exposure": 0.5})
        self.assertIn("local masks", patch["ignored"])
        # The crop is applied, so it is not also reported as skipped geometry.
        self.assertNotIn("geometry or crop", patch["ignored"])
        self.assertEqual(patch["crop"], {"x": 0.1, "y": 0.1,
                                         "w": 0.8, "h": 0.8})

    def test_settings_that_do_not_convert_are_reported(self):
        self.assertIsNone(catalog_import._develop_from_lua(""))
        self.assertIsNone(catalog_import._develop_from_lua("s = {}"))

    def test_a_straighten_beyond_range_is_named_not_clipped(self):
        patch = catalog_import._develop_from_lua(
            's = {\n\tCropAngle = 30,\n\tExposure2012 = 0.1,\n}')
        self.assertEqual(patch["optics"], {})
        self.assertIn("crop angle beyond supported range", patch["ignored"])


if __name__ == "__main__":
    unittest.main()
