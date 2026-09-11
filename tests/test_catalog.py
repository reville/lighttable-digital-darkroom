# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import catalog as catalog_module
import catalog_scan
import durable_io


def make_catalog(directory: Path) -> catalog_module.Catalog:
    return catalog_module.Catalog(directory / "library.sqlite3")


def write_photo(root: Path, relpath: str, payload: bytes = b"") -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload or relpath.encode() * 64)
    return path


class SchemaTests(unittest.TestCase):
    def test_creates_schema_and_reports_version(self):
        with tempfile.TemporaryDirectory() as directory:
            cat = make_catalog(Path(directory))
            self.assertTrue(cat.integrity_ok())
            self.assertEqual(cat.stats()["schema"],
                             catalog_module.SCHEMA_VERSION)

    def test_reopening_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.sqlite3"
            first = catalog_module.Catalog(path)
            source = first.add_source(directory)
            first.close()
            second = catalog_module.Catalog(path)
            self.assertEqual([s["id"] for s in second.sources()], [source])

    def test_integrity_check_rejects_broken_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "library.sqlite3"
            photos = root / "photos"
            photos.mkdir()
            write_photo(photos, "a.jpg")
            cat = catalog_module.Catalog(path)
            source = cat.add_source(photos)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            cat.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("UPDATE files SET source_id=99999")
            connection.commit()
            connection.close()

            reopened = catalog_module.Catalog(path)
            self.assertFalse(reopened.integrity_ok())
            self.assertFalse(catalog_module._catalog_file_ok(path))
            reopened.close()

    def test_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.sqlite3"
            cat = catalog_module.Catalog(path)
            with cat.write() as conn:
                conn.execute("UPDATE meta SET value='999'"
                             " WHERE key='schema_version'")
            cat.close()
            with self.assertRaises(catalog_module.CatalogVersionError):
                catalog_module.Catalog(path)

    def test_migration_is_backed_up_and_commits_the_new_column_with_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.sqlite3"
            schema_v1 = catalog_module._SCHEMA.replace(
                "    active        INTEGER NOT NULL DEFAULT 1,\n", "")
            connection = sqlite3.connect(path)
            connection.executescript(schema_v1)
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', '1')")
            connection.execute(
                "INSERT INTO sources(path, display_name, favorite, available,"
                " added_at) VALUES(?,?,?,?,?)",
                (directory, "Photos", 0, 1, 0),
            )
            connection.commit()
            connection.close()

            cat = catalog_module.Catalog(path)
            columns = {row[1] for row in
                       cat.connection.execute("PRAGMA table_info(sources)")}

            self.assertIn("active", columns)
            self.assertEqual(cat.stats()["schema"],
                             catalog_module.SCHEMA_VERSION)
            tables = {row[0] for row in cat.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("watch_ledger", tables)
            self.assertEqual(cat.sources()[0]["name"], "Photos")
            self.assertEqual(
                len(list((Path(directory) / "Backups").glob("*.zip"))), 1)
            cat.close()

    def test_a_failed_begin_releases_the_catalog_write_lock(self):
        fake = mock.Mock()
        fake._write_lock = __import__("threading").Lock()
        fake.connection.execute.side_effect = OSError("database unavailable")

        with self.assertRaises(OSError):
            catalog_module._WriteTransaction(fake).__enter__()

        self.assertTrue(fake._write_lock.acquire(blocking=False))
        fake._write_lock.release()


class QualifiedNameTests(unittest.TestCase):
    def test_round_trip(self):
        name = catalog_module.qualified_name(4, "trip/frame.RAF")
        self.assertEqual(catalog_module.parse_name(name),
                         (4, "trip/frame.RAF", None))

    def test_virtual_copy_round_trip(self):
        name = catalog_module.qualified_name(2, "a/b.jpg", "copy-7")
        self.assertEqual(catalog_module.parse_name(name),
                         (2, "a/b.jpg", "copy-7"))

    def test_bare_name_has_no_source(self):
        self.assertEqual(catalog_module.parse_name("frame.jpg"),
                         (None, "frame.jpg", None))

    def test_windows_style_name_is_not_mistaken_for_a_source(self):
        source_id, rest, _ = catalog_module.parse_name("C:/photos/a.jpg")
        self.assertIsNone(source_id)
        self.assertEqual(rest, "C:/photos/a.jpg")


class ScanTests(unittest.TestCase):
    def test_raw_dimensions_choose_the_largest_image_ifd(self):
        width, height = catalog_scan._largest_image_dimensions({
            "Exif.Image.ImageWidth": "160",
            "Exif.Image.ImageLength": "120",
            "Exif.SubImage1.ImageWidth": "4032",
            "Exif.SubImage1.ImageLength": "3024",
            "Exif.SubImage2.ImageWidth": "960",
            "Exif.SubImage2.ImageLength": "720",
        })
        self.assertEqual((width, height), (4032, 3024))

    def test_rw2_dimensions_accept_image_height_pair(self):
        width, height = catalog_scan._largest_image_dimensions({
            "Exif.PanasonicRaw.ImageWidth": "5184",
            "Exif.PanasonicRaw.ImageHeight": "3888",
        })
        self.assertEqual((width, height), (5184, 3888))
        self.assertGreaterEqual(catalog_scan.METADATA_VERSION, 3)

    def test_dimension_pairs_must_be_complete_and_positive(self):
        self.assertEqual(catalog_scan._largest_image_dimensions({
            "Exif.Image.ImageWidth": "160",
            "Exif.SubImage1.ImageWidth": "6000",
            "Exif.SubImage1.ImageLength": "0",
        }), (None, None))

    def test_rw2_metadata_reader_keeps_image_height_and_backfills_old_rows(self):
        fields = {
            "Exif.PanasonicRaw.ImageWidth": "6008",
            "Exif.PanasonicRaw.ImageHeight": "4008",
            "Exif.PanasonicRaw.SensorWidth": "6016",
            "Exif.PanasonicRaw.SensorHeight": "4016",
            "Exif.Image.Orientation": "6",
        }
        entries = {key: mock.Mock(**{"key.return_value": key,
                    "toString.return_value": value}) for key, value in fields.items()}

        class ExifData(list):
            def findKey(self, key):
                return entries.get(key)

            def end(self):
                return None

        image = mock.Mock()
        image.exifData.return_value = ExifData(entries.values())
        exiv2 = types.SimpleNamespace(
            ExifKey=lambda key: key,
            ImageFactory=mock.Mock(open=mock.Mock(return_value=image)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            photo = write_photo(root, "street.RW2")
            cat = make_catalog(Path(directory))
            self.addCleanup(cat.close)
            source = cat.add_source(root)
            with mock.patch.object(catalog_scan, "read_metadata", return_value={
                    "width": 1920, "height": 1280, "metadata_version": 5}):
                catalog_scan.scan_source(cat, source)
            before = cat.query()["items"][0]
            cat.save_state(before["id"], {"rating": 5, "grade": {"exposure": 0.5}})
            with mock.patch.dict("sys.modules", {"exiv2": exiv2}):
                metadata = catalog_scan.read_metadata(photo)
                self.assertEqual((metadata["width"], metadata["height"]), (6008, 4008))
                refreshed = catalog_scan.scan_source(cat, source)
                image.readMetadata.reset_mock()
                stable = catalog_scan.scan_source(cat, source)
                image.readMetadata.assert_not_called()
            after = cat.query()["items"][0]
            self.assertEqual(refreshed["updated"], 1)
            self.assertEqual(stable["updated"], 0)
            self.assertEqual((after["width"], after["height"]), (4008, 6008))
            self.assertEqual(after["id"], before["id"])
            self.assertEqual(after["fileKey"], before["fileKey"])
            self.assertEqual(after["rating"], 5)
            self.assertEqual(cat.state_for(after["id"])["grade"]["exposure"], 0.5)

    def test_metadata_version_refreshes_an_unchanged_file_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "portrait.dng")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            original_hash = cat.query()["items"][0]["fileKey"]
            metadata = {
                "width": 6000, "height": 4000, "orientation": 6,
                "metadata_version": catalog_scan.METADATA_VERSION,
            }

            with mock.patch.object(catalog_scan, "read_metadata",
                                   return_value=metadata) as read:
                refreshed = catalog_scan.scan_source(cat, source)
                stable = catalog_scan.scan_source(cat, source)

            item = cat.query()["items"][0]
            self.assertEqual(refreshed["updated"], 1)
            self.assertEqual(stable["updated"], 0)
            self.assertEqual(read.call_count, 1)
            self.assertEqual((item["width"], item["height"]), (4000, 6000))
            self.assertEqual(item["fileKey"], original_hash)

    def test_photo_without_exif_uses_image_header_dimensions(self):
        try:
            import exiv2
        except ImportError:
            self.skipTest("Exiv2 metadata reader required")
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "no-exif.jpg"
            Image.new("RGB", (96, 64)).save(photo)
            metadata = catalog_scan.read_metadata(photo)
            self.assertEqual((metadata["width"], metadata["height"]), (96, 64))

    def test_scan_adds_files_and_skips_non_photos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            write_photo(root, "sub/b.dng")
            write_photo(root, "notes.txt")
            write_photo(root, ".hidden.jpg")
            write_photo(root, "film-exports/out.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            result = catalog_scan.scan_source(cat, source,
                                              read_metadata_for_new=False)
            self.assertEqual(result["added"], 2)
            names = {i["relpath"] for i in cat.query()["items"]}
            self.assertEqual(names, {"a.jpg", "sub/b.dng"})

    def test_rescan_is_cheap_and_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            again = catalog_scan.scan_source(cat, source,
                                             read_metadata_for_new=False)
            self.assertEqual((again["added"], again["updated"]), (0, 0))

    def test_missing_file_is_flagged_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            path = write_photo(root, "a.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            image_id = cat.image_id_for(source, "a.jpg")
            cat.save_state(image_id, {"rating": 4})
            path.unlink()
            result = catalog_scan.scan_source(cat, source,
                                              read_metadata_for_new=False)
            self.assertEqual(result["missing"], 1)
            self.assertEqual(cat.query()["total"], 0)
            self.assertEqual(cat.state_for(image_id)["rating"], 4)

    def test_moved_file_relinks_and_keeps_its_edits(self):
        """The point of hashing content: Finder moves must not lose work."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            path = write_photo(root, "a.jpg", b"unique-content" * 100)
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            image_id = cat.image_id_for(source, "a.jpg")
            cat.save_state(image_id, {"rating": 5, "keywords": ["holiday"]})

            moved = root / "trip" / "renamed.jpg"
            moved.parent.mkdir()
            path.rename(moved)
            result = catalog_scan.scan_source(cat, source,
                                              read_metadata_for_new=False)

            self.assertEqual(result["relinked"], 1)
            self.assertEqual(result["added"], 0)
            items = cat.query()["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["relpath"], "trip/renamed.jpg")
            self.assertEqual(items[0]["rating"], 5)
            state = cat.state_for(items[0]["id"])
            self.assertEqual(state["keywords"], ["holiday"])

    def test_relink_refreshes_the_kind_when_the_extension_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            path = write_photo(root, "shot.dng", b"raw-content" * 100)
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            self.assertEqual(
                cat.connection.execute(
                    "SELECT kind FROM files WHERE relpath='shot.dng'"
                ).fetchone()["kind"], "raw")

            path.rename(root / "shot.tif")
            result = catalog_scan.scan_source(cat, source,
                                              read_metadata_for_new=False)

            self.assertEqual(result["relinked"], 1)
            row = cat.connection.execute(
                "SELECT relpath, kind FROM files").fetchone()
            self.assertEqual(row["relpath"], "shot.tif")
            self.assertEqual(row["kind"], "processed")
            cat.close()

    def test_header_hash_changes_with_content_and_survives_touch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.bin"
            path.write_bytes(b"one" * 100)
            first = catalog_scan.header_hash(path)
            os.utime(path, (0, 0))
            self.assertEqual(catalog_scan.header_hash(path), first)
            path.write_bytes(b"two" * 100)
            self.assertNotEqual(catalog_scan.header_hash(path), first)

    def test_duplicate_detection_groups_identical_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg", b"same" * 100)
            write_photo(root, "copy/a.jpg", b"same" * 100)
            write_photo(root, "b.jpg", b"other" * 100)
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            groups = cat.duplicates()
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["files"]), 2)


class SourceRetirementTests(unittest.TestCase):
    def test_removing_and_readding_a_source_restores_every_edit_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg", b"unique" * 100)
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            image = cat.image_id_for(source, "a.jpg")
            cat.save_state(image, {"rating": 5, "keywords": ["Keep > This"]})
            cat.add_history(image, "careful edit", {"rating": 5})
            cat.save_versions(image, [{
                "id": "v1", "name": "Finished", "created": "2026-09-03",
                "grade": {"exposure": 0.5},
            }])

            cat.remove_source(source)

            self.assertEqual(cat.sources(), [])
            self.assertEqual(cat.query()["total"], 0)
            self.assertEqual(cat.stats()["retiredSources"], 1)
            self.assertEqual(cat.state_for(image)["rating"], 5)
            self.assertEqual(len(cat.history_for(image)), 1)

            restored = cat.add_source(root)

            self.assertEqual(restored, source)
            self.assertEqual(cat.query()["total"], 1)
            self.assertEqual(cat.state_for(image)["keywords"], ["Keep > This"])
            self.assertEqual(cat.versions_for(image)[0]["name"], "Finished")
            cat.close()


class RelocationTransactionTests(unittest.TestCase):
    def test_a_late_batch_conflict_rolls_back_every_catalog_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            write_photo(root, "b.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)

            with self.assertRaises(sqlite3.IntegrityError):
                cat.relocate_files([
                    (source, "a.jpg", source, "same.jpg"),
                    (source, "b.jpg", source, "same.jpg"),
                ])

            self.assertIsNotNone(cat.image_id_for(source, "a.jpg"))
            self.assertIsNotNone(cat.image_id_for(source, "b.jpg"))
            self.assertIsNone(cat.image_id_for(source, "same.jpg"))
            cat.close()

    def test_folder_rename_treats_sql_wildcards_as_literal_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "Trip_1/a.jpg")
            write_photo(root, "TripA1/b.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            folder_id = cat.connection.execute(
                "SELECT id FROM folders WHERE source_id=? AND relpath='Trip_1'",
                (source,),
            ).fetchone()[0]

            scoped = cat.query({
                "scope": "folder", "folderId": folder_id,
                "includeSubfolders": True,
            })
            self.assertEqual(scoped["total"], 1)

            self.assertEqual(
                cat.rename_folder(source, "Trip_1", "Renamed"), 1)

            self.assertIsNotNone(cat.image_id_for(source, "Renamed/a.jpg"))
            self.assertIsNotNone(cat.image_id_for(source, "TripA1/b.jpg"))
            cat.close()

    def test_folder_collections_treat_sql_wildcards_as_literal_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "Trip_1/a.jpg")
            write_photo(root, "TripA1/b.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)

            cat.create_collections_for_source_folders(source)

            collections = {item["name"]: item for item in cat.collections()}
            self.assertIn("Trip_1", collections)
            result = cat.query({"scope": "collection",
                                "collectionId": collections["Trip_1"]["id"]})
            self.assertEqual([item["relpath"] for item in result["items"]],
                             ["Trip_1/a.jpg"])
            cat.close()


class QueryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        directory = Path(self._dir.name)
        root = directory / "photos"
        root.mkdir()
        for n in range(6):
            write_photo(root, f"f{n}.jpg", f"content-{n}".encode() * 50)
        write_photo(root, "raw/r.dng", b"rawfile" * 100)
        self.cat = make_catalog(directory)
        self.source = self.cat.add_source(root)
        catalog_scan.scan_source(self.cat, self.source,
                                 read_metadata_for_new=False)
        self.items = {i["relpath"]: i["id"]
                      for i in self.cat.query(({"limit": 100}))["items"]}

    def tearDown(self):
        self.cat.close()
        self._dir.cleanup()

    def test_rating_and_label_filters(self):
        self.cat.save_state(self.items["f0.jpg"], {"rating": 5,
                                                   "label": "red"})
        self.cat.save_state(self.items["f1.jpg"], {"rating": 2})
        result = self.cat.query({"filter": {"ratingMin": 3}})
        self.assertEqual(result["total"], 1)
        red = self.cat.query({"filter": {"label": "red"}})
        self.assertEqual(red["total"], 1)
        any_label = self.cat.query({"filter": {"label": "any"}})
        self.assertEqual(any_label["total"], 1)

    def test_kind_filter(self):
        self.assertEqual(self.cat.query({"filter": {"kind": "raw"}})["total"], 1)
        self.assertEqual(
            self.cat.query({"filter": {"kind": "processed"}})["total"], 6)

    def test_status_filter(self):
        self.cat.save_state(self.items["f2.jpg"], {"status": "approved"})
        self.assertEqual(
            self.cat.query({"filter": {"status": "approved"}})["total"], 1)

    def test_capture_provenance_distinguishes_metadata_from_filesystem_fallback(self):
        with self.cat.write() as conn:
            conn.execute("UPDATE files SET capture_time=? WHERE relpath=?",
                         ("2024-03-09T18:30:00", "f0.jpg"))
        items = {item["relpath"]: item for item in self.cat.query({"limit": 100})["items"]}
        self.assertTrue(items["f0.jpg"]["captureTimeKnown"])
        self.assertEqual(items["f0.jpg"]["captureTime"], "2024-03-09T18:30:00")
        self.assertFalse(items["f1.jpg"]["captureTimeKnown"])
        self.assertTrue(items["f1.jpg"]["captureTime"])

    def test_a_bare_end_date_includes_that_whole_day(self):
        with self.cat.write() as conn:
            conn.execute("UPDATE files SET capture_time=? WHERE relpath=?",
                         ("2024-03-09T18:30:00", "f0.jpg"))
            conn.execute("UPDATE files SET capture_time=? WHERE relpath=?",
                         ("2024-03-10T00:00:01", "f1.jpg"))
        result = self.cat.query({"filter": {"dateFrom": "2024-03-09",
                                            "dateTo": "2024-03-09"}})
        self.assertEqual([item["relpath"] for item in result["items"]],
                         ["f0.jpg"])
        # A bound with a time of day still compares exactly.
        precise = self.cat.query({"filter": {"dateFrom": "2024-03-09",
                                             "dateTo": "2024-03-09T12:00:00"}})
        self.assertEqual(precise["total"], 0)

    def test_paging_reports_total_beyond_the_page(self):
        page = self.cat.query({"limit": 3})
        self.assertEqual(page["total"], 7)
        self.assertEqual(len(page["items"]), 3)
        second = self.cat.query({"limit": 3, "offset": 3})
        self.assertEqual(len(second["items"]), 3)
        self.assertFalse({i["id"] for i in page["items"]}
                         & {i["id"] for i in second["items"]})

    def test_unknown_folder_does_not_expand_to_the_whole_library(self):
        for recursive in (True, False):
            with self.subTest(includeSubfolders=recursive):
                result = self.cat.query({
                    "scope": "folder", "folderId": 99999,
                    "includeSubfolders": recursive,
                })
                self.assertEqual(result["total"], 0)
                self.assertEqual(result["items"], [])

    def test_keyword_rename_refreshes_search_for_parent_children_and_copies(self):
        parent_image = self.items["f0.jpg"]
        child_image = self.items["f1.jpg"]
        self.cat.save_state(parent_image, {"keywords": ["Trip"]})
        self.cat.save_state(child_image, {"keywords": ["Trip > Rome"]})
        copy_id = self.cat.add_virtual_copy(child_image, "copy-1", "Alternate")
        self.cat.save_state(self.items["f2.jpg"], {"keywords": ["Tripod"]})
        parent = next(k for k in self.cat.keyword_tree() if k["path"] == "Trip")

        self.cat.rename_keyword(parent["id"], "Travel")

        result = self.cat.query({"filter": {"query": "Travel"}})
        self.assertEqual({i["id"] for i in result["items"]},
                         {parent_image, child_image, copy_id})
        self.assertEqual(self.cat.query({"filter": {"query": "Rome"}})["total"], 2)
        self.assertEqual(self.cat.query({"filter": {"query": "Trip"}})["total"], 1)

    def test_keyword_rename_rolls_back_if_search_refresh_fails(self):
        image = self.items["f0.jpg"]
        self.cat.save_state(image, {"keywords": ["Trip > Rome"]})
        parent = next(k for k in self.cat.keyword_tree() if k["path"] == "Trip")
        with mock.patch.object(self.cat, "_reindex", side_effect=sqlite3.OperationalError(
                "disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.cat.rename_keyword(parent["id"], "Travel")
        self.assertEqual(self.cat.keywords_for(image), ["Trip > Rome"])
        self.assertEqual(self.cat.query({"filter": {"query": "Trip"}})["total"], 1)
        self.assertEqual(self.cat.query({"filter": {"query": "Travel"}})["total"], 0)

    def test_rescan_refreshes_camera_and_lens_search_for_all_interpretations(self):
        base = self.items["f0.jpg"]
        copy_id = self.cat.add_virtual_copy(base, "copy-1", "Alternate")
        source_path = Path(self.cat.source_by_id(self.source)["path"])
        for make, lens in (("OldCamera", "OldLens"), ("NewCamera", "NewLens")):
            with self.subTest(camera=make):
                write_photo(source_path, "f0.jpg", make.encode() * 80)
                metadata = {"camera_make": make, "lens": lens,
                            "metadata_version": catalog_scan.METADATA_VERSION}
                with mock.patch.object(catalog_scan, "read_metadata", side_effect=
                        lambda path: metadata if path.name == "f0.jpg" else {
                            "metadata_version": catalog_scan.METADATA_VERSION}):
                    catalog_scan.scan_source(self.cat, self.source)
                for query in (make, lens):
                    result = self.cat.query({"filter": {"query": query}})
                    matching = {i["id"] for i in result["items"]}
                    self.assertEqual(matching, {base, copy_id})
        self.assertEqual(self.cat.query({"filter": {"query": "OldCamera"}})["total"], 0)

    def test_finder_rename_refreshes_names_and_search_without_resetting_copies(self):
        base = self.items["f0.jpg"]
        self.cat.save_state(base, {"rating": 5, "keywords": ["Holiday"]})
        copy_id = self.cat.add_virtual_copy(base, "copy-1", "Alternate")
        root = Path(self.cat.source_by_id(self.source)["path"])
        (root / "f0.jpg").rename(root / "renamed.jpg")

        result = catalog_scan.scan_source(self.cat, self.source,
                                         read_metadata_for_new=False)

        self.assertEqual(result["relinked"], 1)
        found = self.cat.query({"filter": {"query": "renamed"}})["items"]
        self.assertEqual({i["id"] for i in found}, {base, copy_id})
        self.assertEqual({i["id"]: i["displayName"] for i in found},
                         {base: "renamed.jpg", copy_id: "Alternate"})
        self.assertEqual(self.cat.query({"filter": {"query": "f0"}})["total"], 0)
        self.assertEqual(self.cat.state_for(base)["rating"], 5)
        self.assertEqual(self.cat.state_for(copy_id)["keywords"], ["Holiday"])

    def test_unchanged_file_returning_to_its_path_becomes_visible_again(self):
        base = self.items["f0.jpg"]
        self.cat.save_state(base, {"rating": 5, "keywords": ["Holiday"],
                                  "grade": {"exposure": 0.5}})
        self.cat.save_iptc(base, {"caption": "Keep this caption"})
        copy_id = self.cat.add_virtual_copy(base, "copy-1", "Alternate")
        root = Path(self.cat.source_by_id(self.source)["path"])
        original = root / "f0.jpg"
        parked = root.parent / "parked.jpg"
        original.rename(parked)
        catalog_scan.scan_source(self.cat, self.source, read_metadata_for_new=False)
        self.assertEqual(self.cat.query()["total"], 6)
        parked.rename(original)

        result = catalog_scan.scan_source(self.cat, self.source,
                                         read_metadata_for_new=False)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.cat.query()["total"], 8)
        self.assertEqual(self.cat.image_id_for(self.source, "f0.jpg"), base)
        self.assertEqual(self.cat.state_for(base)["rating"], 5)
        self.assertEqual(self.cat.state_for(base)["grade"], {"exposure": 0.5})
        self.assertEqual(self.cat.iptc_for(base)["caption"], "Keep this caption")
        self.assertEqual(self.cat.state_for(copy_id)["keywords"], ["Holiday"])

    def test_restored_file_keeps_metadata_when_the_reader_is_unavailable(self):
        base = self.items["f0.jpg"]
        root = Path(self.cat.source_by_id(self.source)["path"])
        with mock.patch.object(catalog_scan, "read_metadata", return_value={
                "camera_make": "Camera", "lens": "Lens",
                "metadata_version": catalog_scan.METADATA_VERSION - 1}):
            catalog_scan.scan_source(self.cat, self.source)
        original = root / "f0.jpg"
        parked = root.parent / "parked.jpg"
        original.rename(parked)
        catalog_scan.scan_source(self.cat, self.source, read_metadata_for_new=False)
        parked.rename(original)

        with mock.patch.object(catalog_scan, "read_metadata", return_value={}):
            result = catalog_scan.scan_source(self.cat, self.source)

        self.assertEqual(result["updated"], 1)
        found = next(i for i in self.cat.query()["items"] if i["id"] == base)
        self.assertEqual((found["camera"], found["lens"]), ("Camera", "Lens"))

    def test_search_matches_filename_and_keyword(self):
        self.cat.save_state(self.items["f3.jpg"], {"keywords": ["Sunset"]})
        by_keyword = self.cat.query({"filter": {"query": "sunset"}})
        self.assertEqual(by_keyword["total"], 1)
        by_name = self.cat.query({"filter": {"query": "f3"}})
        self.assertGreaterEqual(by_name["total"], 1)

    def test_search_tolerates_punctuation(self):
        """`f/2.8` must not be read as an FTS operator."""
        self.cat.query({"filter": {"query": 'f/2.8 "quoted'}})

    def test_keyword_filter_includes_children(self):
        self.cat.save_state(self.items["f4.jpg"],
                            {"keywords": ["Places > Italy > Rome"]})
        parent = self.cat.query({"filter": {"keyword": "Places"}})
        self.assertEqual(parent["total"], 1)

    def test_keyword_filter_treats_sql_wildcards_as_literal_text(self):
        self.cat.save_state(self.items["f4.jpg"],
                            {"keywords": ["Trip_ > Rome"]})
        self.cat.save_state(self.items["f5.jpg"],
                            {"keywords": ["TripA > Paris"]})

        parent = self.cat.query({"filter": {"keyword": "Trip_"}})

        self.assertEqual(parent["total"], 1)
        self.assertEqual(parent["items"][0]["relpath"], "f4.jpg")

    def test_sort_by_name_is_stable_both_directions(self):
        ascending = [i["filename"] for i in
                     self.cat.query({"sort": {"field": "name"},
                                     "limit": 100})["items"]]
        descending = [i["filename"] for i in
                      self.cat.query({"sort": {"field": "name",
                                               "dir": "desc"},
                                      "limit": 100})["items"]]
        self.assertEqual(ascending, list(reversed(descending)))

    def test_smart_collection_rules_are_applied_server_side(self):
        self.cat.save_state(self.items["f5.jpg"], {"rating": 5})
        collection = self.cat.add_collection(
            "Best", kind="smart", rules={"ratingMin": 4})
        result = self.cat.query({"scope": "collection",
                                 "collectionId": collection})
        self.assertEqual(result["total"], 1)

    def test_regular_collection_membership(self):
        collection = self.cat.add_collection("Set")
        self.cat.set_collection_members(
            collection, [self.items["f0.jpg"], self.items["f1.jpg"]])
        result = self.cat.query({"scope": "collection",
                                 "collectionId": collection})
        self.assertEqual(result["total"], 2)

    def test_create_collections_for_source_folders(self):
        cids = self.cat.create_collections_for_source_folders(self.source)
        self.assertTrue(len(cids) >= 1)
        cols = self.cat.collections()
        self.assertTrue(any(c["name"] == "sub" for c in cols) or len(cols) >= 1)

    def test_create_collections_for_an_empty_source_returns_none(self):
        empty = Path(self._dir.name) / "empty-source"
        empty.mkdir()
        source = self.cat.add_source(empty)
        self.assertEqual(
            self.cat.create_collections_for_source_folders(source), [])

    def test_virtual_copy_has_independent_state(self):
        base = self.items["f0.jpg"]
        self.cat.save_state(base, {"rating": 1, "keywords": ["Places > Rome"]})
        self.cat.save_iptc(base, {"creator": "Photographer"})
        self.cat.save_versions(base, [{
            "id": "v1", "name": "Warm", "created": "2026-01-01",
            "grade": {"temp": 0.2},
        }])
        copy_id = self.cat.add_virtual_copy(base, "copy-1", "f0 (copy)")
        self.cat.save_state(copy_id, {"rating": 5})
        self.assertEqual(self.cat.state_for(base)["rating"], 1)
        self.assertEqual(self.cat.state_for(copy_id)["rating"], 5)
        self.assertEqual(self.cat.state_for(copy_id)["keywords"],
                         ["Places > Rome"])
        self.assertEqual(self.cat.iptc_for(copy_id)["creator"], "Photographer")
        self.assertEqual(self.cat.versions_for(copy_id)[0]["name"], "Warm")
        self.assertEqual(
            self.cat.query({"filter": {"kind": "virtual"}})["total"], 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        directory = Path(self._dir.name)
        root = directory / "photos"
        root.mkdir()
        write_photo(root, "a.jpg")
        self.cat = make_catalog(directory)
        self.source = self.cat.add_source(root)
        catalog_scan.scan_source(self.cat, self.source,
                                 read_metadata_for_new=False)
        self.image_id = self.cat.image_id_for(self.source, "a.jpg")

    def tearDown(self):
        self.cat.close()
        self._dir.cleanup()

    def test_partial_save_leaves_other_keys_alone(self):
        self.cat.save_state(self.image_id, {"rating": 3, "grade":
                                            {"exposure": 0.5}})
        self.cat.save_state(self.image_id, {"status": "approved"})
        state = self.cat.state_for(self.image_id)
        self.assertEqual(state["rating"], 3)
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["grade"], {"exposure": 0.5})

    def test_state_save_normalizes_invalid_enums_and_rating(self):
        self.cat.save_state(self.image_id, {
            "status": "destroyed", "rating": "99", "label": "orange",
        })

        state = self.cat.state_for(self.image_id)

        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["rating"], 5)
        self.assertEqual(state["label"], "none")

        self.cat.save_state(self.image_id, {"rating": "not-a-number"})
        self.assertEqual(self.cat.state_for(self.image_id)["rating"], 0)

    def test_keyword_hierarchy_is_interned(self):
        self.cat.save_state(self.image_id,
                            {"keywords": ["Places > Italy > Rome"]})
        paths = {k["path"] for k in self.cat.keyword_tree()}
        self.assertEqual(paths, {"Places", "Places > Italy",
                                 "Places > Italy > Rome"})

    def test_renaming_a_parent_keyword_rewrites_children(self):
        self.cat.save_state(self.image_id, {"keywords": ["Trip > Rome"]})
        parent = [k for k in self.cat.keyword_tree() if k["path"] == "Trip"][0]
        self.cat.rename_keyword(parent["id"], "Travel")
        paths = {k["path"] for k in self.cat.keyword_tree()}
        self.assertEqual(paths, {"Travel", "Travel > Rome"})

    def test_keyword_rename_treats_sql_wildcards_as_literal_text(self):
        self.cat.save_state(
            self.image_id, {"keywords": ["Trip_ > Rome", "TripA > Paris"]})
        parent = [
            item for item in self.cat.keyword_tree()
            if item["path"] == "Trip_"
        ][0]

        self.cat.rename_keyword(parent["id"], "Travel")

        paths = {item["path"] for item in self.cat.keyword_tree()}
        self.assertIn("Travel > Rome", paths)
        self.assertIn("TripA > Paris", paths)

    def test_iptc_round_trip(self):
        self.cat.save_iptc(self.image_id, {"creator": "Nicholas",
                                           "copyright": "(c) 2026",
                                           "gps_lat": 41.9, "gps_lon": 12.5})
        stored = self.cat.iptc_for(self.image_id)
        self.assertEqual(stored["creator"], "Nicholas")
        self.assertAlmostEqual(stored["gps_lat"], 41.9)

    def test_history_appends_and_caps(self):
        for n in range(12):
            self.cat.add_history(self.image_id, f"step {n}",
                                 {"grade": {"exposure": n / 10}}, cap=5)
        steps = self.cat.history_for(self.image_id)
        self.assertEqual(len(steps), 5)
        newest = steps[0]
        self.assertEqual(self.cat.history_state(newest["id"])["grade"]
                         ["exposure"], 1.1)

    def test_history_truncation_after_a_step(self):
        for n in range(5):
            self.cat.add_history(self.image_id, f"s{n}", {"n": n})
        self.cat.truncate_history_after(self.image_id, 2)
        self.assertEqual(len(self.cat.history_for(self.image_id)), 2)

    def test_versions_round_trip(self):
        self.cat.save_versions(self.image_id, [
            {"id": "v1", "name": "Warm", "created": "2026-01-01",
             "grade": {"temp": 0.2}, "crop": None}])
        versions = self.cat.versions_for(self.image_id)
        self.assertEqual(versions[0]["name"], "Warm")
        self.assertEqual(versions[0]["grade"], {"temp": 0.2})

    def test_logically_corrupt_edit_blobs_degrade_per_field_not_per_library(self):
        history = self.cat.add_history(self.image_id, "good", {"rating": 5})
        collection = self.cat.add_collection(
            "Smart", kind="smart", rules={"ratingMin": 4})
        self.cat.save_versions(self.image_id, [{
            "id": "v1", "name": "Good", "created": "2026-09-03",
            "grade": {"exposure": 0.2},
        }])
        with self.cat.write() as connection:
            connection.execute(
                "UPDATE image_state SET grade_json='{broken' WHERE image_id=?",
                (self.image_id,),
            )
            connection.execute(
                "UPDATE versions SET state_json='{broken' WHERE image_id=?",
                (self.image_id,),
            )
            connection.execute(
                "UPDATE collections SET rules_json='{broken' WHERE id=?",
                (collection,),
            )
            connection.execute(
                "UPDATE history SET state_blob=x'00ff' WHERE id=?", (history,))

        state = self.cat.state_for(self.image_id)
        self.assertIsNone(state["grade"])
        self.assertEqual(state["versions"], [])
        self.assertIsNone(self.cat.collection(collection)["rules"])
        self.assertIsNone(self.cat.history_state(history))
        self.assertEqual(self.cat.query({"limit": 10}, include_state=True)["total"], 1)

    def test_logically_corrupt_scalar_cells_do_not_make_library_unopenable(self):
        with self.cat.write() as connection:
            connection.execute(
                "UPDATE image_state SET status=x'ff', rating='broken',"
                " label='orange' WHERE image_id=?", (self.image_id,),
            )
            connection.execute(
                "UPDATE files SET mtime_ns='broken', size='broken',"
                " width='broken', height=-2, camera_make=x'ff',"
                " lens=x'ff' WHERE id=(SELECT file_id FROM images WHERE id=?)",
                (self.image_id,),
            )
            connection.execute(
                "UPDATE sources SET path=x'ff', display_name=x'ff'"
                " WHERE id=?", (self.source,),
            )

        state = self.cat.state_for(self.image_id)
        page = self.cat.query({"limit": 10}, include_state=True)
        source = self.cat.sources()[0]

        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["rating"], 0)
        self.assertEqual(state["label"], "none")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["mtime"], 0)
        self.assertEqual(page["items"][0]["size"], 0)
        self.assertIsNone(page["items"][0]["width"])
        self.assertIsNone(page["items"][0]["height"])
        self.assertEqual(page["items"][0]["camera"], "")
        self.assertIsNone(page["items"][0]["lens"])
        self.assertEqual(source["path"], "")
        self.assertEqual(source["name"], "Unavailable source")
        self.assertFalse(source["available"])


class MigrationTests(unittest.TestCase):
    def test_state_file_import_reproduces_the_old_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg", b"aaa" * 100)
            write_photo(root, "b.jpg", b"bbb" * 100)
            state = {
                "images": {
                    "a.jpg": {"status": "approved", "rating": 4,
                              "keywords": ["Rome"],
                              "grade": {"exposure": 0.25},
                              "versions": [{"id": "v1", "name": "First",
                                            "created": "2026-01-01",
                                            "grade": {"temp": 0.1}}]},
                    "b.jpg": {"status": "skipped", "rating": 1},
                    "gone.jpg": {"rating": 5},
                },
                "collections": [
                    {"id": "c1", "name": "Picks", "type": "regular",
                     "members": ["a.jpg"]},
                    {"id": "c2", "name": "Best", "type": "smart",
                     "rules": {"ratingMin": 4}},
                ],
                "stacks": [{"id": "s1", "name": "Burst",
                            "members": ["a.jpg", "b.jpg"], "collapsed": True}],
            }
            (root / catalog_scan.STATE_FILENAME).write_text(json.dumps(state))

            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            result = catalog_scan.import_state_file(cat, source)

            self.assertEqual(result["images"], 2)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["collections"], 2)
            self.assertEqual(result["stacks"], 1)

            image_id = cat.image_id_for(source, "a.jpg")
            state_back = cat.state_for(image_id)
            self.assertEqual(state_back["rating"], 4)
            self.assertEqual(state_back["status"], "approved")
            self.assertEqual(state_back["keywords"], ["Rome"])
            self.assertEqual(state_back["grade"], {"exposure": 0.25})
            self.assertEqual(state_back["versions"][0]["name"], "First")
            self.assertTrue((root / catalog_scan.STATE_FILENAME).is_file())

    def test_state_file_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            (root / catalog_scan.STATE_FILENAME).write_text(json.dumps(
                {"images": {"a.jpg": {"rating": 3}},
                 "collections": [{"id": "c", "name": "One", "type": "regular",
                                  "members": ["a.jpg"]}]}))
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            catalog_scan.import_state_file(cat, source)
            catalog_scan.import_state_file(cat, source)
            self.assertEqual(len(cat.collections()), 1)

    def test_virtual_copies_survive_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            marker = catalog_module.VIRTUAL_MARKER
            (root / catalog_scan.STATE_FILENAME).write_text(json.dumps({
                "images": {
                    "a.jpg": {"rating": 1},
                    f"a.jpg{marker}copy-9": {"rating": 5},
                },
                "virtualCopies": [{"id": "copy-9", "source": "a.jpg",
                                   "name": f"a.jpg{marker}copy-9",
                                   "displayName": "a (copy)"}],
            }))
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            result = catalog_scan.import_state_file(cat, source)
            self.assertEqual(result["virtual"], 1)
            copy_id = cat.image_id_for(source, "a.jpg", "copy-9")
            self.assertEqual(cat.state_for(copy_id)["rating"], 5)
            base_id = cat.image_id_for(source, "a.jpg")
            self.assertEqual(cat.state_for(base_id)["rating"], 1)

    def test_mirror_writes_a_readable_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            cat.save_state(cat.image_id_for(source, "a.jpg"), {"rating": 2})
            self.assertTrue(catalog_scan.mirror_state_file(cat, source))
            written = json.loads(
                (root / catalog_scan.STATE_FILENAME).read_text())
            self.assertEqual(written["images"]["a.jpg"]["rating"], 2)

    def test_mirror_preserves_non_image_top_level_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            target = root / catalog_scan.STATE_FILENAME
            target.write_text(json.dumps({
                "images": {"gone.jpg": {"rating": 5}},
                "collections": [{"id": "c1", "name": "Portable"}],
                "stacks": [{"id": "s1", "members": ["a.jpg"]}],
                "futureSetting": {"enabled": True},
            }))
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            cat.save_state(cat.image_id_for(source, "a.jpg"), {"rating": 2})

            # Classify the legacy file before mirroring. An unavailable
            # original's pending recipe must survive alongside these keys.
            catalog_scan.import_state_file(cat, source)
            self.assertTrue(catalog_scan.mirror_state_file(cat, source))
            written = json.loads(target.read_text())
            self.assertEqual(written["collections"][0]["name"], "Portable")
            self.assertEqual(written["stacks"][0]["id"], "s1")
            self.assertEqual(written["futureSetting"], {"enabled": True})
            self.assertEqual(written["images"]["gone.jpg"]["rating"], 5)

    def test_second_mirror_reads_only_state_changed_since_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            write_photo(root, "b.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            a_id = cat.image_id_for(source, "a.jpg")
            cat.save_state(a_id, {"rating": 2})
            self.assertTrue(catalog_scan.mirror_state_file(cat, source))

            with mock.patch.object(cat, "state_for", wraps=cat.state_for) as read:
                self.assertTrue(catalog_scan.mirror_state_file(cat, source))
                read.assert_not_called()

            cat.save_state(a_id, {"rating": 4})
            with mock.patch.object(cat, "state_for", wraps=cat.state_for) as read:
                self.assertTrue(catalog_scan.mirror_state_file(cat, source))
                self.assertEqual(read.call_count, 1)
            written = json.loads(
                (root / catalog_scan.STATE_FILENAME).read_text())
            self.assertEqual(written["images"]["a.jpg"]["rating"], 4)

    def test_mirror_on_a_read_only_folder_reports_false_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            write_photo(root, "a.jpg")
            cat = make_catalog(Path(directory))
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            os.chmod(root, 0o500)
            try:
                self.assertFalse(catalog_scan.mirror_state_file(cat, source))
            finally:
                os.chmod(root, 0o700)


class BackupTests(unittest.TestCase):
    def test_backup_produces_a_zip_and_prunes(self):
        with tempfile.TemporaryDirectory() as directory:
            cat = make_catalog(Path(directory))
            cat.add_source(directory)
            target = Path(directory) / "backups"
            archives = [cat.backup(target) for _ in range(3)]
            for archive in archives:
                self.assertTrue(archive.is_file())
            self.assertEqual(len({a.name for a in archives}), 3,
                             "backups taken in the same millisecond must not "
                             "overwrite each other")
            self.assertEqual(cat.prune_backups(target, keep=1), 2)
            self.assertEqual(
                len(list(target.glob("LightTable-catalog-*.zip"))), 1)

    def test_concurrent_backups_never_publish_over_the_same_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = make_catalog(root)
            cat.add_source(root)
            target = root / "backups"
            frozen = mock.Mock()
            frozen.strftime.return_value = "2026-09-03-120000-000000"
            datetime_api = mock.Mock()
            datetime_api.now.return_value = frozen

            def take_backup(_):
                try:
                    return cat.backup(target)
                finally:
                    cat.close()

            with mock.patch.object(catalog_module, "datetime", datetime_api):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    archives = list(pool.map(take_backup, range(3)))

            self.assertEqual(len(set(archives)), 3)
            self.assertTrue(all(path.is_file() for path in archives))
            self.assertEqual(len(list(target.glob("*.zip"))), 3)
            cat.close()

    def test_recovery_skips_a_newer_bad_archive_and_preserves_the_damaged_db(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "library.sqlite3"
            photos = root / "photos"
            photos.mkdir()
            write_photo(photos, "a.jpg")
            cat = catalog_module.Catalog(catalog_path)
            source = cat.add_source(photos)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            image = cat.image_id_for(source, "a.jpg")
            cat.save_state(image, {"rating": 5})
            valid = cat.backup()
            cat.close()
            invalid = valid.parent / "LightTable-catalog-9999-bad.zip"
            invalid.write_bytes(b"not a zip")
            os.utime(invalid, None)
            catalog_path.write_bytes(b"damaged sqlite bytes")

            recovered = catalog_module.recover_latest_backup(catalog_path)

            self.assertIsNotNone(recovered)
            self.assertEqual(Path(recovered["archive"]), valid)
            self.assertEqual(
                (Path(recovered["quarantine"]) / catalog_path.name).read_bytes(),
                b"damaged sqlite bytes",
            )
            restored = catalog_module.Catalog(catalog_path)
            self.assertEqual(restored.state_for(image)["rating"], 5)
            restored.close()

    def test_failed_recovery_install_never_leaves_a_missing_empty_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "library.sqlite3"
            cat = make_catalog(root)
            cat.add_source(root)
            cat.backup()
            cat.close()
            damaged = b"damaged catalog still needs recovery"
            catalog_path.write_bytes(damaged)
            wal = Path(str(catalog_path) + "-wal")
            shm = Path(str(catalog_path) + "-shm")
            wal.write_bytes(b"damaged wal")
            shm.write_bytes(b"damaged shm")

            with mock.patch.object(
                    durable_io, "publish_file",
                    side_effect=OSError("power failed before install")):
                recovered = catalog_module.recover_latest_backup(catalog_path)

            self.assertIsNone(recovered)
            self.assertEqual(catalog_path.read_bytes(), damaged)
            self.assertEqual(wal.read_bytes(), b"damaged wal")
            self.assertEqual(shm.read_bytes(), b"damaged shm")
            quarantined = list((root / "Recovery").glob("*/library.sqlite3"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), damaged)

    def test_backup_failure_leaves_no_archive_that_could_be_pruned_as_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = make_catalog(root)
            cat.add_source(root)
            with mock.patch.object(catalog_module, "_catalog_file_ok",
                                   return_value=False):
                with self.assertRaises(RuntimeError):
                    cat.backup()
            self.assertEqual(list((root / "Backups").glob("*.zip")), [])
            self.assertEqual(list((root / "Backups").glob(".*")), [])
            cat.close()


if __name__ == "__main__":
    unittest.main()
