from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

import catalog
import catalog_scan


class ScanIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "photos"
        self.root.mkdir()
        self.cat = catalog.Catalog(Path(self.temp.name) / "library.sqlite3")
        self.addCleanup(self.cat.close)
        self.source = self.cat.add_source(self.root)

    def photo(self, name, content=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content or name.encode() * 20)
        return path

    def scan(self, **kwargs):
        return catalog_scan.scan_source(self.cat, self.source,
                                        read_metadata_for_new=False, **kwargs)

    def mirror(self):
        self.assertTrue(catalog_scan.mirror_state_file(self.cat, self.source))
        return json.loads((self.root / catalog_scan.STATE_FILENAME).read_text())["images"]

    def test_mirror_keeps_renamed_photos_and_virtual_copy_edits(self):
        original = self.photo("original.jpg")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        copy_id = self.cat.add_virtual_copy(image, "copy", "Alternate")
        self.cat.save_state(copy_id, {"rating": 2})
        self.mirror()
        original.rename(self.root / "renamed.jpg")
        self.scan()

        mirrored = self.mirror()

        self.assertEqual(mirrored["renamed.jpg"]["rating"], 5)
        self.assertEqual(mirrored["renamed.jpg" + catalog.VIRTUAL_MARKER + "copy"]["rating"], 2)
        self.assertNotIn("original.jpg", mirrored)

    def test_mirror_restores_edits_when_a_missing_photo_returns(self):
        original = self.photo("original.jpg")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        self.mirror()
        parked = self.root.parent / "parked.jpg"
        original.rename(parked)
        self.scan()
        self.assertEqual(self.mirror(), {})
        parked.rename(original)
        self.scan()
        self.assertEqual(self.mirror()["original.jpg"]["rating"], 5)

    def test_mirror_refreshes_renamed_keywords(self):
        original = self.photo("original.jpg")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"keywords": ["Trip > Rome"]})
        self.mirror()
        parent = next(k for k in self.cat.keyword_tree() if k["path"] == "Trip")
        self.cat.rename_keyword(parent["id"], "Travel")
        self.assertEqual(self.mirror()["original.jpg"]["keywords"], ["Travel > Rome"])

    def test_mirror_refreshes_saved_versions_and_their_removal(self):
        original = self.photo("original.jpg")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        self.mirror()
        self.cat.save_versions(image, [{
            "id": "v1", "name": "Warm", "created": "2026-09-04",
            "grade": {"temp": 0.2}}])
        self.assertEqual(self.mirror()["original.jpg"]["versions"][0]["name"], "Warm")
        self.cat.save_versions(image, [])
        self.assertEqual(self.mirror()["original.jpg"]["versions"], [])

    def test_unreadable_subfolder_does_not_hide_catalogued_photos(self):
        self.photo("blocked/edited.jpg")
        self.photo("visible.jpg")
        self.scan()
        image = self.cat.image_id_for(self.source, "blocked/edited.jpg")
        self.cat.save_state(image, {"rating": 5, "grade": {"exposure": 0.5}})
        last_scan = self.cat.source_by_id(self.source)["last_scan_at"]
        self.photo("new.jpg")
        scandir = catalog_scan.os.scandir

        def blocked(path):
            if Path(path) == self.root / "blocked":
                raise PermissionError("permission denied")
            return scandir(path)

        with mock.patch.object(catalog_scan.os, "scandir", side_effect=blocked):
            result = self.scan()

        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["added"], 1)
        self.assertFalse(result["complete"])
        self.assertIn("blocked", result["error"])
        self.assertEqual(self.cat.query()["total"], 3)
        self.assertEqual(self.cat.source_by_id(self.source)["last_scan_at"], last_scan)
        self.assertEqual(self.cat.state_for(image)["grade"], {"exposure": 0.5})
        self.assertTrue(self.scan()["complete"])

    def test_scan_limit_does_not_mark_unvisited_photos_missing(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.photo(name)
        self.scan()

        partial = self.scan(limit=1)

        self.assertEqual(partial["missing"], 0)
        self.assertFalse(partial["complete"])
        self.assertIn("limit", partial["error"].lower())
        self.assertEqual(self.cat.query()["total"], 3)
        complete = self.scan(limit=3)
        self.assertTrue(complete["complete"])
        (self.root / "b.jpg").unlink()
        self.assertEqual(self.scan()["missing"], 1)

    def test_failed_entry_stat_does_not_hide_the_photo(self):
        self.photo("edited.jpg")
        self.scan()
        with catalog_scan.os.scandir(self.root) as entries:
            entry = next(entries)
        broken = mock.Mock(wraps=entry)
        broken.name = entry.name
        broken.path = entry.path
        broken.stat.side_effect = OSError("device read failed")
        with mock.patch.object(catalog_scan.os, "scandir", return_value=[broken]):
            result = self.scan()
        self.assertEqual(result["missing"], 0)
        self.assertFalse(result["complete"])
        self.assertEqual(self.cat.query()["total"], 1)

    def test_changed_file_keeps_metadata_when_reading_fails(self):
        photo = self.photo("photo.jpg")
        self.scan()
        with self.cat.write() as conn:
            conn.execute(
                "UPDATE files SET capture_time=?, camera_make=?, camera_model=?,"
                " lens=?, width=?, height=?, orientation=?, metadata_version=?"
                " WHERE source_id=? AND relpath=?",
                ("2024-05-01T12:00:00", "Canon", "EOS R", "RF 50mm", 6000,
                 4000, 1, catalog_scan.METADATA_VERSION, self.source,
                 photo.name))
        photo.write_bytes(b"changed bytes" * 40)

        with mock.patch.object(catalog_scan, "read_metadata", return_value={}):
            result = catalog_scan.scan_source(self.cat, self.source)

        self.assertEqual(result["updated"], 1)
        row = self.cat.connection.execute(
            "SELECT capture_time, camera_make, camera_model, lens, width,"
            " height, orientation, metadata_version FROM files"
            " WHERE source_id=? AND relpath=?",
            (self.source, photo.name)).fetchone()
        self.assertEqual(row["capture_time"], "2024-05-01T12:00:00")
        self.assertEqual(row["camera_make"], "Canon")
        self.assertEqual(row["camera_model"], "EOS R")
        self.assertEqual(row["lens"], "RF 50mm")
        self.assertEqual(row["width"], 6000)
        self.assertEqual(row["height"], 4000)
        self.assertEqual(row["orientation"], 1)
        self.assertEqual(row["metadata_version"], catalog_scan.METADATA_VERSION)

    def test_case_only_rename_reattaches_the_same_row(self):
        probe = self.root / "CaseProbe"
        probe.write_bytes(b"case")
        if not (self.root / "caseprobe").exists():
            probe.unlink()
            self.skipTest("case-insensitive filesystem required")
        probe.unlink()

        photo = self.photo("IMG.JPG")
        self.scan()
        image = self.cat.image_id_for(self.source, "IMG.JPG")
        self.cat.save_state(image, {"rating": 4})
        photo.rename(self.root / "img.jpg")

        self.scan()

        self.assertEqual(self.cat.query()["total"], 1)
        row = self.cat.connection.execute(
            "SELECT relpath FROM files WHERE source_id=?",
            (self.source,)).fetchone()
        self.assertEqual(row["relpath"], "img.jpg")
        self.assertEqual(self.cat.image_id_for(self.source, "img.jpg"), image)
        self.assertEqual(self.cat.state_for(image)["rating"], 4)

    def test_invalid_scan_limit_leaves_the_catalog_alone(self):
        self.photo("a.jpg")
        self.scan()
        with self.assertRaisesRegex(ValueError, "positive"):
            self.scan(limit=0)
        self.assertEqual(self.cat.query()["total"], 1)

    def test_scan_all_reports_an_unavailable_source_as_incomplete(self):
        self.photo("a.jpg")
        self.scan()
        self.root.rename(self.root.parent / "offline")
        result = catalog_scan.scan_all(self.cat, read_metadata_for_new=False)
        self.assertFalse(result["complete"])
        self.assertEqual(self.cat.query()["total"], 1)

    def test_restored_original_does_not_lose_its_identity_to_a_duplicate(self):
        original = self.photo("original.jpg", b"same photo" * 100)
        self.scan()
        image = self.cat.image_id_for(self.source, "original.jpg")
        self.cat.save_state(image, {"rating": 5})
        parked = self.root.parent / "parked.jpg"
        original.rename(parked)
        self.scan()
        parked.rename(original)
        self.photo("duplicate.jpg", original.read_bytes())
        walk = catalog_scan.walk_source

        def duplicate_first(*args, **kwargs):
            return iter(sorted(walk(*args, **kwargs), key=lambda r: r["filename"]))

        with mock.patch.object(catalog_scan, "walk_source", side_effect=duplicate_first):
            result = self.scan()

        self.assertEqual(result["relinked"], 0)
        self.assertEqual(self.cat.image_id_for(self.source, "original.jpg"), image)
        duplicate = self.cat.image_id_for(self.source, "duplicate.jpg")
        self.assertNotEqual(duplicate, image)
        self.assertEqual(self.cat.state_for(duplicate)["rating"], 0)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertEqual(self.cat.query()["total"], 2)

    def test_offline_source_does_not_transfer_edits_to_another_copy(self):
        original = self.photo("original.jpg", b"same photo" * 100)
        self.scan()
        image = self.cat.image_id_for(self.source, "original.jpg")
        self.cat.save_state(image, {"rating": 5})
        other_root = self.root.parent / "other"
        other_root.mkdir()
        (other_root / "duplicate.jpg").write_bytes(original.read_bytes())
        other_source = self.cat.add_source(other_root)
        self.root.rename(self.root.parent / "offline")

        result = catalog_scan.scan_source(self.cat, other_source,
                                         read_metadata_for_new=False)

        self.assertEqual(result["relinked"], 0)
        self.assertEqual(self.cat.image_id_for(self.source, "original.jpg"), image)
        other = self.cat.image_id_for(other_source, "duplicate.jpg")
        self.assertNotEqual(other, image)
        self.assertEqual(self.cat.state_for(other)["rating"], 0)

    def test_unreadable_original_does_not_abort_scanning_a_duplicate(self):
        original = self.photo("original.jpg", b"same photo" * 100)
        self.scan()
        image = self.cat.image_id_for(self.source, "original.jpg")
        self.photo("duplicate.jpg", original.read_bytes())
        stat = Path.stat

        def guarded(path, *args, **kwargs):
            if path == original:
                raise PermissionError("permission denied")
            return stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", guarded):
            result = self.scan()

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(self.cat.image_id_for(self.source, "original.jpg"), image)

    def test_stale_missing_candidates_do_not_prevent_a_real_relink(self):
        content = b"same photo" * 100
        self.photo("present.jpg", content)
        gone = self.photo("gone.jpg", content)
        self.scan()
        present_id = self.cat.image_id_for(self.source, "present.jpg")
        gone_id = self.cat.image_id_for(self.source, "gone.jpg")
        self.cat.save_state(gone_id, {"rating": 4})
        self.cat.mark_missing(self.source, ())
        gone.rename(self.root / "renamed.jpg")

        result = self.scan()

        self.assertEqual(result["relinked"], 1)
        self.assertEqual(self.cat.image_id_for(self.source, "present.jpg"), present_id)
        self.assertEqual(self.cat.image_id_for(self.source, "renamed.jpg"), gone_id)
        self.assertEqual(self.cat.state_for(gone_id)["rating"], 4)
        self.assertEqual(self.cat.query()["total"], 2)

    def test_checking_stale_missing_candidates_is_bounded(self):
        content = b"same photo" * 100
        for index in range(catalog.RELINK_CANDIDATE_LIMIT + 5):
            self.photo(f"copy-{index}.jpg", content)
        self.scan()
        self.cat.mark_missing(self.source, ())
        new = self.photo("new.jpg", content)
        record = next(r for r in catalog_scan.walk_source(self.root)
                      if r["filename"] == new.name)
        record["header_hash"] = catalog_scan.header_hash(new)
        record["content_hash"] = catalog_scan.file_identity.content_hash(new)
        checked = []
        stat = Path.stat

        def counted(path, *args, **kwargs):
            checked.append(path)
            return stat(path, *args, **kwargs)

        with self.cat.write() as conn, mock.patch.object(Path, "stat", counted):
            result = self.cat.relink_by_hash(conn, self.source, record)

        self.assertIsNone(result)
        self.assertGreater(len(checked), 0)
        self.assertLessEqual(len(checked), catalog.RELINK_CANDIDATE_LIMIT)


if __name__ == "__main__":
    unittest.main()
