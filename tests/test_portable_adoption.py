# SPDX-License-Identifier: GPL-3.0-only
"""Portable adoption protects catalog authority and survives interrupted scans."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalog
import catalog_scan


class PortableAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "photos"
        self.root.mkdir()
        self.database = self.root.parent / "catalog.sqlite3"
        self.cat = catalog.Catalog(self.database)
        self.addCleanup(lambda: self.cat.close())
        self.source = self.cat.add_source(self.root)

    def photo(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode() * 30)
        return path

    def scan(self, **kwargs):
        return catalog_scan.scan_source(self.cat, self.source,
                                        read_metadata_for_new=False, **kwargs)

    def write_portable(self, state):
        (self.root / catalog_scan.STATE_FILENAME).write_text(json.dumps(state))

    def adopt(self):
        return catalog_scan.import_state_file(self.cat, self.source)

    def reopen(self):
        self.cat.close()
        self.cat = catalog.Catalog(self.database)

    def image(self, name="a.jpg", copy_ident=None):
        return self.cat.image_id_for(self.source, name, copy_ident)

    def test_new_source_still_adopts_after_reopen_before_first_import(self):
        self.photo("a.jpg")
        self.write_portable({"images": {"a.jpg": {"rating": 4}}})
        self.scan()
        self.reopen()
        self.assertEqual(self.adopt()["images"], 1)
        self.assertEqual(self.cat.state_for(self.image())["rating"], 4)

    def test_startup_never_replaces_a_committed_edit_with_its_old_mirror(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.save_state(self.image(), {"rating": 5, "grade": {"exposure": 0.2}})
        catalog_scan.mirror_state_file(self.cat, self.source)
        self.cat.save_state(self.image(), {"rating": 2, "grade": {"exposure": 0.7}})
        self.reopen()
        scanner = catalog_scan.ScanService(self.cat)
        scanner.request(adopt_state_file=True)
        scanner._thread.join(timeout=5)
        self.assertFalse(scanner._thread.is_alive())
        self.assertNotIn("error", scanner.status["last"])
        self.assertEqual(self.cat.state_for(self.image())["rating"], 2)
        self.assertEqual(self.cat.state_for(self.image())["grade"], {"exposure": 0.7})

    def test_upgrade_preserves_existing_rows_even_when_reset_to_defaults(self):
        self.photo("a.jpg")
        self.scan()
        self.write_portable({"images": {"a.jpg": {"rating": 5, "grade": {"exposure": 1}}}})
        # Model the catalog before adoption ownership existed, including a
        # deliberate default state that cannot be distinguished from new state.
        with self.cat.write() as conn:
            conn.execute("DELETE FROM meta WHERE key LIKE 'portable.%'")
        self.reopen()
        self.assertTrue(self.adopt()["adopted"])
        self.assertEqual(self.cat.state_for(self.image())["rating"], 0)
        self.assertIsNone(self.cat.state_for(self.image())["grade"])

    def test_edit_before_initial_adoption_protects_only_that_photo(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        self.scan()
        self.write_portable({"images": {"a.jpg": {"rating": 5}, "b.jpg": {"rating": 4}}})
        self.cat.save_state(self.image(), {"rating": 0, "grade": None})
        self.assertEqual(self.adopt()["images"], 1)
        self.assertEqual(self.cat.state_for(self.image())["rating"], 0)
        self.assertEqual(self.cat.state_for(self.image("b.jpg"))["rating"], 4)

    def test_retiring_and_readding_source_does_not_repeat_adoption(self):
        self.photo("a.jpg")
        self.scan()
        self.write_portable({"images": {"a.jpg": {"rating": 5}}})
        self.adopt()
        self.cat.save_state(self.image(), {"rating": 1})
        self.cat.remove_source(self.source)
        self.assertEqual(self.cat.add_source(self.root), self.source)
        self.reopen()
        self.assertTrue(self.adopt()["adopted"])
        self.assertEqual(self.cat.state_for(self.image())["rating"], 1)

    def test_incomplete_adoption_resumes_without_reverting_or_resurrecting(self):
        self.photo("a.jpg")
        self.scan()
        copy_name = "a.jpg" + catalog.VIRTUAL_MARKER + "alternate"
        self.write_portable({"images": {"a.jpg": {"rating": 4},
                                       copy_name: {"rating": 2}, "later.jpg": {"rating": 3}}})
        self.assertEqual(self.adopt()["skipped"], 1)
        self.cat.delete_virtual_copy(self.image(copy_ident="alternate"))
        self.cat.save_state(self.image(), {"rating": 1})
        self.photo("later.jpg")
        self.scan()
        self.reopen()
        report = self.adopt()
        self.assertEqual(report["images"], 1)
        self.assertEqual(report["virtual"], 0)
        self.assertIsNone(self.image(copy_ident="alternate"))
        self.assertEqual(self.cat.state_for(self.image())["rating"], 1)
        self.assertEqual(self.cat.state_for(self.image("later.jpg"))["rating"], 3)

    def test_import_failure_rolls_back_recipes_copies_and_ownership(self):
        self.photo("a.jpg")
        self.scan()
        copy_name = "a.jpg" + catalog.VIRTUAL_MARKER + "alternate"
        self.write_portable({"images": {
            "a.jpg": {"rating": 4, "versions": [{"id": "v1", "name": "Warm"}]},
            copy_name: {"rating": 3}}})
        with mock.patch.object(self.cat, "save_versions", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.adopt()
        self.assertEqual(self.cat.state_for(self.image())["rating"], 0)
        self.assertIsNone(self.image(copy_ident="alternate"))
        self.assertFalse(self.cat.connection.in_transaction)
        self.assertEqual(self.adopt()["images"], 2)
        self.assertEqual(self.cat.state_for(self.image())["versions"][0]["name"], "Warm")

    def test_current_virtual_descriptors_round_trip_and_remove_deleted_copies(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.save_state(self.image(), {"rating": 4})
        copy_id = self.cat.add_virtual_copy(self.image(), "alternate", "Warm alternate")
        self.cat.save_state(copy_id, {"grade": {"exposure": 1.2}})
        catalog_scan.mirror_state_file(self.cat, self.source)
        fresh = catalog.Catalog(self.root.parent / "fresh.sqlite3")
        self.addCleanup(fresh.close)
        fresh_source = fresh.add_source(self.root)
        catalog_scan.scan_source(fresh, fresh_source, read_metadata_for_new=False)
        report = catalog_scan.import_state_file(fresh, fresh_source)
        self.assertEqual(report["virtual"], 1)
        fresh_copy = fresh.image_id_for(fresh_source, "a.jpg", "alternate")
        self.assertEqual(fresh.state_for(fresh_copy)["grade"], {"exposure": 1.2})
        self.assertEqual(fresh.image_row(fresh_copy)["display_name"], "Warm alternate")
        self.cat.delete_virtual_copy(copy_id)
        catalog_scan.mirror_state_file(self.cat, self.source)
        portable = json.loads((self.root / catalog_scan.STATE_FILENAME).read_text())
        self.assertEqual(portable["virtualCopies"], [])
        self.assertNotIn("a.jpg" + catalog.VIRTUAL_MARKER + "alternate", portable["images"])

    def test_old_mirror_without_descriptors_recovers_copy_recipe(self):
        self.photo("a.jpg")
        self.scan()
        copy_name = "a.jpg" + catalog.VIRTUAL_MARKER + "alternate"
        self.write_portable({"images": {copy_name: {"grade": {"exposure": 1.2}}}})
        self.assertEqual(self.adopt()["virtual"], 1)
        self.assertEqual(self.cat.state_for(self.image(copy_ident="alternate"))["grade"], {"exposure": 1.2})

    def test_default_portable_copy_does_not_inherit_recent_original_edits(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.save_state(self.image(), {"grade": {"exposure": 0.7}, "keywords": ["Recent"]})
        self.cat.save_versions(self.image(), [{"id": "v1", "name": "Recent version"}])
        self.write_portable({"images": {}, "virtualCopies": [{
            "source": "a.jpg", "id": "default", "displayName": "Unedited"}]})
        self.assertEqual(self.adopt()["virtual"], 1)
        restored = self.cat.state_for(self.image(copy_ident="default"))
        self.assertIsNone(restored["grade"])
        self.assertEqual(restored["keywords"], [])
        self.assertEqual(restored["versions"], [])

    def test_malformed_portable_root_does_not_prevent_later_valid_import(self):
        self.photo("a.jpg")
        self.scan()
        self.write_portable([])
        self.assertEqual(self.adopt()["error"], "unreadable")
        self.write_portable({"images": {"a.jpg": {"rating": 3}}})
        self.assertEqual(self.adopt()["images"], 1)

    def test_mirror_waits_for_initial_scan_and_legacy_adoption(self):
        for index in range(201):
            self.photo(f"photo-{index}.jpg")
        self.write_portable({"images": {f"photo-{index}.jpg": {"rating": 4}
                                       for index in range(201)}})
        target = self.root / catalog_scan.STATE_FILENAME
        original = target.read_bytes()
        mirrored_during_scan = []
        def edit_during_scan(source_id, relpath):
            if not mirrored_during_scan:
                self.cat.save_state(self.image(relpath), {"rating": 2})
                mirrored_during_scan.append(catalog_scan.mirror_state_file(self.cat, self.source))
                self.assertEqual(target.read_bytes(), original)
        self.scan(on_local_file=edit_during_scan)
        self.assertEqual(mirrored_during_scan, [False])
        # Also close the brief interval between scan completion and adoption.
        self.assertFalse(catalog_scan.mirror_state_file(self.cat, self.source))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.adopt()["images"], 200)
        self.assertTrue(catalog_scan.mirror_state_file(self.cat, self.source))
        restored = json.loads(target.read_text())["images"]
        self.assertEqual(len(restored), 201)
        self.assertEqual(sum(value["rating"] == 4 for value in restored.values()), 200)

    def test_bad_utf8_mirror_can_be_replaced_with_committed_catalog_state(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.save_state(self.image(), {"rating": 3})
        (self.root / catalog_scan.STATE_FILENAME).write_bytes(b"\xff")
        self.assertTrue(catalog_scan.mirror_state_file(self.cat, self.source))
        self.assertEqual(json.loads((self.root / catalog_scan.STATE_FILENAME).read_text())["images"]["a.jpg"]["rating"], 3)

    def test_completed_scan_does_not_erase_unavailable_recipe_before_first_adoption(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.save_state(self.image(), {"rating": 2})
        self.write_portable({"images": {"unavailable.jpg": {"grade": {"exposure": 1.2}}}})
        target = self.root / catalog_scan.STATE_FILENAME
        original = target.read_bytes()
        self.assertFalse(catalog_scan.mirror_state_file(self.cat, self.source))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.adopt()["skipped"], 1)
        self.assertTrue(catalog_scan.mirror_state_file(self.cat, self.source))
        self.assertEqual(json.loads(target.read_text())["images"]["unavailable.jpg"]["grade"], {"exposure": 1.2})

    def test_adopting_copy_does_not_revert_its_originals_local_capture_time(self):
        self.photo("a.jpg")
        self.scan()
        self.cat.set_capture_override(self.image(), "2026-09-09T15:00:00")
        copy_name = "a.jpg" + catalog.VIRTUAL_MARKER + "alternate"
        self.write_portable({"images": {copy_name: {
            "grade": {"exposure": 1.2}, "captureTimeOverride": "2026-01-01T12:00:00"}}})
        self.assertEqual(self.adopt()["virtual"], 1)
        self.assertEqual(self.cat.capture_details(self.image())["override"], "2026-09-09T15:00:00")
        self.assertEqual(self.cat.state_for(self.image(copy_ident="alternate"))["grade"], {"exposure": 1.2})

    def test_groups_without_image_recipes_wait_for_all_members(self):
        self.photo("a.jpg")
        self.scan()
        self.write_portable({"images": {},
            "collections": [{"name": "Together", "members": ["a.jpg", "later.jpg"]}],
            "stacks": [{"name": "Burst", "members": ["a.jpg", "later.jpg"]}]})
        report = self.adopt()
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["pendingGroups"], 2)
        self.assertEqual(self.cat.collections(), [])
        self.photo("later.jpg")
        self.scan()
        self.reopen()
        report = self.adopt()
        self.assertEqual((report["collections"], report["stacks"]), (1, 1))
        self.assertEqual(self.cat.collections()[0]["count"], 2)
        self.assertEqual(self.cat.stack_id_for(self.image()), self.cat.stack_id_for(self.image("later.jpg")))

    def test_retry_never_recreates_deleted_groups_or_changes_user_group(self):
        self.photo("a.jpg")
        self.scan()
        self.write_portable({"images": {}, "collections": [
            {"name": "Now", "members": ["a.jpg"]},
            {"name": "Later", "members": ["a.jpg", "later.jpg"]}]})
        self.assertEqual(self.adopt()["pendingGroups"], 1)
        self.cat.delete_collection(self.cat.collections()[0]["id"])
        user_group = self.cat.add_collection("Later")
        self.cat.set_collection_members(user_group, [self.image()])
        self.photo("later.jpg")
        self.scan()
        self.adopt()
        self.assertEqual([(row["name"], row["count"]) for row in self.cat.collections()], [("Later", 1)])

    def test_retry_never_adds_a_photo_already_in_a_user_stack(self):
        self.photo("a.jpg")
        self.photo("user.jpg")
        self.scan()
        self.write_portable({"images": {}, "stacks": [
            {"name": "Portable", "members": ["a.jpg", "later.jpg"]}]})
        self.assertEqual(self.adopt()["pendingGroups"], 1)
        user_stack = self.cat.add_stack("User", [self.image(), self.image("user.jpg")])
        self.photo("later.jpg")
        self.scan()
        self.assertEqual(self.adopt()["stacks"], 0)
        self.assertEqual(self.cat.stack_id_for(self.image()), user_stack)
        self.assertIsNone(self.cat.stack_id_for(self.image("later.jpg")))

    def test_mirror_preserves_pending_recipes_while_updating_local_edits(self):
        self.photo("a.jpg")
        self.scan()
        copy_name = "later.jpg" + catalog.VIRTUAL_MARKER + "alternate"
        self.write_portable({"images": {"a.jpg": {"rating": 4},
            "later.jpg": {"rating": 3}, copy_name: {"grade": {"exposure": 1.2}}},
            "virtualCopies": [{"source": "later.jpg", "id": "alternate", "name": copy_name,
                               "displayName": "Later alternate"}]})
        self.assertEqual(self.adopt()["skipped"], 2)
        self.cat.save_state(self.image(), {"rating": 1})
        self.assertTrue(catalog_scan.mirror_state_file(self.cat, self.source))
        portable = json.loads((self.root / catalog_scan.STATE_FILENAME).read_text())
        self.assertEqual(portable["images"]["a.jpg"]["rating"], 1)
        self.assertEqual(portable["images"]["later.jpg"]["rating"], 3)
        self.assertEqual(portable["images"][copy_name]["grade"], {"exposure": 1.2})
        self.assertEqual(portable["virtualCopies"][0]["displayName"], "Later alternate")
        self.photo("later.jpg")
        self.scan()
        self.assertEqual(self.adopt()["images"], 2)
        self.assertEqual(self.cat.state_for(self.image())["rating"], 1)
        self.assertEqual(self.cat.state_for(self.image("later.jpg", "alternate"))["grade"], {"exposure": 1.2})

    def test_scan_keeps_photo_renamed_after_its_batch_commits(self):
        original = self.photo("a.jpg")
        self.scan()
        image = self.image()
        self.cat.save_state(image, {"rating": 5})
        def rename(source_id, relative):
            original.rename(self.root / "renamed.jpg")
            self.cat.rename_file(self.source, "a.jpg", "renamed.jpg")
        report = self.scan(on_local_file=rename)
        self.assertEqual(report["missing"], 0)
        self.assertEqual(self.cat.query()["total"], 1)
        self.assertEqual(self.image("renamed.jpg"), image)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)

    def test_scan_keeps_file_registered_after_directory_traversal(self):
        self.photo("a.jpg")
        self.scan()
        def arrival(source_id, relative):
            catalog_scan.register_file(self.cat, self.photo("arrival.jpg"))
        report = self.scan(on_local_file=arrival)
        self.assertEqual(report["missing"], 0)
        self.assertEqual(self.cat.query()["total"], 2)

    def test_missing_check_does_not_hide_unreadable_unseen_file(self):
        target = self.photo("a.jpg")
        self.scan()
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            if path == target:
                raise PermissionError("unreadable volume")
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(self.cat.mark_missing(self.source, []), 0)
        self.assertEqual(self.cat.query()["total"], 1)

    def test_missing_check_only_stats_unseen_paths(self):
        self.photo("a.jpg")
        removed = self.photo("gone.jpg")
        self.scan()
        removed.unlink()
        calls = []
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            calls.append(path)
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(self.cat.mark_missing(self.source, ["a.jpg"]), 1)
        self.assertNotIn(self.root / "a.jpg", calls)
        self.assertIn(removed, calls)

    def test_nested_write_rollback_can_leave_outer_transaction_usable(self):
        with self.cat.write() as conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('outer','kept')")
            with self.assertRaisesRegex(ValueError, "cancel inner"):
                with self.cat.write() as nested:
                    nested.execute("INSERT INTO meta(key,value) VALUES('inner','rolled back')")
                    raise ValueError("cancel inner")
            conn.execute("INSERT INTO meta(key,value) VALUES('outer-second','kept')")
        self.assertIsNone(self.cat.connection.execute("SELECT 1 FROM meta WHERE key='inner'").fetchone())
        self.assertEqual(self.cat.connection.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'outer%'").fetchone()[0], 2)

    def test_updated_portable_state_resyncs_when_mtime_changes(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        self.scan()
        self.write_portable({"images": {"a.jpg": {"rating": 3}}})
        # Initial adoption
        res1 = self.adopt()
        self.assertEqual(res1["images"], 1)
        self.assertEqual(self.cat.state_for(self.image("a.jpg"))["rating"], 3)

        # Immediate repeat adoption without file change is a no-op
        res2 = self.adopt()
        self.assertTrue(res2["adopted"])
        self.assertEqual(res2["images"], 0)

        # External update changes state file (e.g. adds rating for b.jpg)
        import time
        time.sleep(0.02)
        self.write_portable({"images": {"a.jpg": {"rating": 3}, "b.jpg": {"rating": 5}}})
        res3 = self.adopt()
        self.assertEqual(res3["images"], 1)
        self.assertEqual(self.cat.state_for(self.image("b.jpg"))["rating"], 5)

    def test_query_supports_include_missing_and_missing_only(self):
        self.photo("present.jpg")
        missing_file = self.photo("missing.jpg")
        self.scan()
        missing_file.unlink()
        self.cat.mark_missing(self.source, ["present.jpg"])

        # Default query excludes missing files
        default_res = self.cat.query()
        self.assertEqual(default_res["total"], 1)
        self.assertEqual(default_res["items"][0]["filename"], "present.jpg")
        self.assertFalse(default_res["items"][0]["missing"])

        # includeMissing returns both
        all_res = self.cat.query({"includeMissing": True})
        self.assertEqual(all_res["total"], 2)
        missing_map = {item["filename"]: item["missing"] for item in all_res["items"]}
        self.assertFalse(missing_map["present.jpg"])
        self.assertTrue(missing_map["missing.jpg"])

        # missingOnly returns only missing
        only_res = self.cat.query({"missingOnly": True})
        self.assertEqual(only_res["total"], 1)
        self.assertEqual(only_res["items"][0]["filename"], "missing.jpg")
        self.assertTrue(only_res["items"][0]["missing"])


if __name__ == "__main__":
    unittest.main()
