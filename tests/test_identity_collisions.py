"""Valid images whose first 64 KiB match must retain separate identities."""
from pathlib import Path
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from PIL import Image

import catalog
import catalog_scan
import ingest_workflow as ingest
import file_identity
import watch_workflow


def collision_pair(first: Path, second: Path) -> None:
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (256, 256), "gray")
    image.save(first, compression="raw")
    image.putpixel((255, 255), (255, 0, 0))
    image.save(second, compression="raw")
    assert first.stat().st_size == second.stat().st_size
    assert first.read_bytes()[:65536] == second.read_bytes()[:65536]
    with Image.open(first) as a, Image.open(second) as b:
        assert a.getpixel((255, 255)) != b.getpixel((255, 255))


class IdentityCollisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.cat = catalog.Catalog(self.root / "catalog.sqlite3")
        self.addCleanup(self.cat.close)
        self.source = self.cat.add_source(self.photos)

    def scan(self):
        return catalog_scan.scan_source(self.cat, self.source,
                                        read_metadata_for_new=False)

    def test_different_lower_pixels_do_not_inherit_missing_photos_edits(self):
        original, other = self.photos / "original.tif", self.root / "other.tif"
        collision_pair(original, other)
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5, "keywords": ["original"]})
        virtual = self.cat.add_virtual_copy(image, "copy", "Original alternate")
        self.cat.save_state(virtual, {"rating": 4})
        original.unlink()
        other.rename(self.photos / other.name)
        result = self.scan()
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(result["added"], 1)
        other_image = self.cat.image_id_for(self.source, other.name)
        self.assertNotEqual(other_image, image)
        self.assertEqual(self.cat.state_for(other_image)["rating"], 0)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertEqual(self.cat.state_for(virtual)["rating"], 4)

    def test_duplicate_groups_require_identical_complete_files(self):
        first, second = self.photos / "first.tif", self.photos / "second.tif"
        collision_pair(first, second)
        shutil.copyfile(first, self.photos / "copy.tif")
        self.scan()
        groups = self.cat.duplicates()
        self.assertEqual(len(groups), 1)
        self.assertEqual({m["relpath"] for m in groups[0]["files"]},
                         {"first.tif", "copy.tif"})

    def test_watch_registers_same_header_different_pixels(self):
        collision_pair(self.photos / "first.tif", self.photos / "second.tif")
        watch = {"id": "test", "path": str(self.photos), "mode": "catalog"}
        service = watch_workflow.WatchService(self.cat, lambda: [watch])
        service.poll_once()
        service.poll_once()
        self.assertEqual(self.cat.query()["total"], 2)

    def test_resume_selects_new_name_for_same_header_different_pixels(self):
        first = self.root / "card" / "frame.tif"
        second = self.photos / "session" / "frame.tif"
        collision_pair(first, second)
        item = ingest.describe_file(first)
        plan = ingest.build_plan([item], {"destination": str(self.photos),
            "folderTemplate": "session", "filenameTemplate": "{filename}"})
        self.assertEqual(Path(plan["items"][0]["destination"]).name,
                         "frame-2.tif")
        copied = ingest.copy_item(plan["items"][0], verify="hash")
        self.assertTrue(copied["ok"], copied)
        self.assertEqual(Path(copied["destination"]).read_bytes(), first.read_bytes())

    def test_existing_catalog_copy_is_detected_by_ingest(self):
        original, _ = self.photos / "original.tif", self.root / "unused.tif"
        collision_pair(original, self.root / "unused.tif")
        self.scan()
        card = self.root / "card.tif"
        shutil.copyfile(original, card)
        items = [ingest.describe_file(card)]
        known = self.cat.ingest_content_hashes(items)
        plan = ingest.build_plan(items, {"destination": str(self.photos)},
                                 existing_content_hashes=known)
        self.assertEqual(plan["duplicates"], 1)
        self.assertEqual(plan["total"], 0)

    def test_ingest_skips_only_full_matches_with_a_shared_prefix(self):
        original = self.photos / "original.tif"
        other = self.root / "card" / "other.tif"
        collision_pair(original, other)
        self.scan()
        identical = other.with_name("identical.tif")
        shutil.copyfile(original, identical)
        items = [ingest.describe_file(other), ingest.describe_file(identical)]
        plan = ingest.build_plan(items, {"destination": str(self.photos)},
            existing_content_hashes=self.cat.ingest_content_hashes(items))
        self.assertEqual(plan["duplicates"], 1)
        self.assertEqual([item["source"] for item in plan["items"]], [str(other)])

    def test_ingest_does_not_skip_a_deleted_catalog_original(self):
        original, other = self.photos / "original.tif", self.root / "other.tif"
        collision_pair(original, other)
        shutil.copyfile(original, other)
        self.scan()
        original.unlink()  # Catalog has not rescanned yet.
        items = [ingest.describe_file(other)]
        plan = ingest.build_plan(items, {"destination": str(self.photos)},
            existing_content_hashes=self.cat.ingest_content_hashes(items))
        self.assertEqual(plan["duplicates"], 0)
        self.assertEqual(plan["total"], 1)

    def test_ingest_revalidates_original_changed_without_a_new_mtime(self):
        original, other = self.photos / "original.tif", self.root / "other.tif"
        collision_pair(original, other)
        incoming = self.root / "incoming.tif"
        shutil.copyfile(original, incoming)
        self.scan()
        saved_stat = original.stat()
        original.write_bytes(other.read_bytes())
        os.utime(original, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
        items = [ingest.describe_file(incoming)]
        plan = ingest.build_plan(items, {"destination": str(self.photos)},
            existing_content_hashes=self.cat.ingest_content_hashes(items))
        self.assertEqual(plan["duplicates"], 0)

    def test_unchanged_rescan_and_ingest_reuse_verified_catalog_bytes(self):
        original, other = self.photos / "original.tif", self.root / "other.tif"
        collision_pair(original, other)
        self.scan()
        with mock.patch.object(file_identity, "content_hash", side_effect=AssertionError("rehash")):
            self.scan()
            known = self.cat.ingest_content_hashes([ingest.describe_file(original)])
        self.assertTrue(next(iter(known.values())))

    def test_v5_migration_backfills_once_and_preserves_edits(self):
        original = self.photos / "original.tif"
        collision_pair(original, self.root / "other.tif")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        self.cat.close()
        with sqlite3.connect(self.cat.path) as connection:
            connection.execute("ALTER TABLE files DROP COLUMN content_hash")
            connection.execute("ALTER TABLE files DROP COLUMN content_signature")
            connection.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        self.cat = catalog.Catalog(self.cat.path)
        self.addCleanup(self.cat.close)
        self.assertEqual(self.cat.stats()["schema"], 6)
        with mock.patch.object(file_identity, "content_hash", wraps=file_identity.content_hash) as hashing:
            self.scan()
            self.scan()
            self.assertEqual(hashing.call_count, 1)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertTrue(self.cat.integrity_ok())

    def test_legacy_missing_identity_cannot_relink_from_prefix_alone(self):
        original, other = self.photos / "original.tif", self.root / "other.tif"
        collision_pair(original, other)
        self.scan()
        with self.cat.write() as connection:
            connection.execute("UPDATE files SET content_hash=NULL, content_signature=NULL")
        original.rename(self.photos / "moved.tif")
        result = self.scan()
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(result["added"], 1)

    def test_existing_destination_never_adopted_on_size_alone(self):
        original, other = self.root / "source.tif", self.photos / "destination.tif"
        collision_pair(original, other)
        before = other.read_bytes()
        for mode in ("none", "size", "hash"):
            with self.subTest(mode=mode):
                copied = ingest.copy_item({"source": str(original), "destination": str(other)},
                                           verify=mode)
                self.assertFalse(copied["ok"])
                self.assertEqual(other.read_bytes(), before)

    def test_legacy_watch_ack_is_upgraded_without_reapplying_a_preset(self):
        original = self.photos / "original.tif"
        collision_pair(original, self.root / "other.tif")
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"grade": {"exposure": 2}})
        self.cat.record_watch_handled("test", ingest.header_hash(original))
        watch = {"id": "test", "path": str(self.photos), "presetId": "preset"}
        service = watch_workflow.WatchService(self.cat, lambda: [watch],
            presets=lambda: [{"id": "preset", "grade": {"exposure": 0}}])
        service.poll_once()
        service.poll_once()
        self.assertEqual(self.cat.state_for(image)["grade"]["exposure"], 2)
        with mock.patch.object(file_identity, "content_hash", side_effect=AssertionError("rehash")):
            service.poll_once()
        self.assertEqual(service.status[0]["error"], "")
        (self.root / "other.tif").rename(self.photos / "other.tif")
        service.poll_once()
        service.poll_once()
        self.assertEqual(self.cat.query()["total"], 2)

    def test_scan_hash_failure_is_incomplete_and_does_not_hide_existing_photos(self):
        original = self.photos / "original.tif"
        collision_pair(original, self.root / "other.tif")
        self.scan()
        original.rename(self.photos / "moved.tif")
        with mock.patch.object(file_identity, "content_hash", side_effect=OSError("changed while hashing")):
            result = self.scan()
        self.assertFalse(result["complete"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(self.cat.query()["total"], 1)

    def test_hash_rejects_file_changed_during_read(self):
        original = self.photos / "original.tif"
        collision_pair(original, self.root / "other.tif")
        digest = file_identity.hashlib.blake2b(digest_size=16)
        class ChangingDigest:
            def update(self, block):
                digest.update(block)
                stat = original.stat()
                os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            def hexdigest(self):
                return digest.hexdigest()
        with mock.patch.object(file_identity.hashlib, "blake2b", return_value=ChangingDigest()):
            with self.assertRaisesRegex(OSError, "changed while hashing"):
                file_identity.content_hash(original)

    def test_scan_rechecks_completed_hash_before_relinking(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5, "grade": {"exposure": 2}})
        moved = self.photos / "moved.tif"
        original.rename(moved)
        actual_hash = file_identity.content_hash

        def replace_after_hash(path, **options):
            digest = actual_hash(path, **options)
            shutil.copyfile(replacement, path)
            return digest

        warmed = []
        with mock.patch.object(file_identity, "content_hash", side_effect=replace_after_hash):
            result = catalog_scan.scan_source(self.cat, self.source,
                read_metadata_for_new=False, on_local_file=lambda *args: warmed.append(args))
        self.assertFalse(result["complete"])
        self.assertEqual((result["added"], result["updated"], result["relinked"], result["missing"]),
                         (0, 0, 0, 0))
        self.assertEqual(self.cat.image_row(image)["relpath"], original.name)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertEqual(warmed, [])
        self.assertIsNone(self.cat.image_id_for(self.source, moved.name))
        retry = self.scan()
        self.assertTrue(retry["complete"])
        self.assertEqual(retry["added"], 1)
        self.assertNotEqual(self.cat.image_id_for(self.source, moved.name), image)

    def test_scan_rolls_back_relink_if_source_changes_during_catalog_work(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        moved = self.photos / "moved.tif"
        original.rename(moved)
        actual_relink = self.cat.relink_by_hash

        def replace_after_relink(connection, source, record):
            result = actual_relink(connection, source, record)
            shutil.copyfile(replacement, record["path"])
            return result

        with mock.patch.object(self.cat, "relink_by_hash", side_effect=replace_after_relink):
            result = self.scan()
        self.assertFalse(result["complete"])
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(self.cat.image_row(image)["relpath"], original.name)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertTrue(self.cat.integrity_ok())

    def test_stale_prehashed_arrival_does_not_block_a_stable_arrival(self):
        first, replacement = self.photos / "first.tif", self.root / "replacement.tif"
        collision_pair(first, replacement)
        second = self.photos / "second.tif"
        Image.new("RGB", (256, 256), "blue").save(second, compression="raw")
        actual_hash = file_identity.content_hash

        def replace_first_after_hash(path, **options):
            digest = actual_hash(path, **options)
            if Path(path).name == first.name:
                shutil.copyfile(replacement, path)
            return digest

        warmed = []
        with mock.patch.object(file_identity, "content_hash", side_effect=replace_first_after_hash):
            result = catalog_scan.scan_source(self.cat, self.source,
                read_metadata_for_new=False, on_local_file=lambda *args: warmed.append(args))
        self.assertFalse(result["complete"])
        self.assertEqual(result["added"], 1)
        self.assertIsNone(self.cat.image_id_for(self.source, first.name))
        self.assertIsNotNone(self.cat.image_id_for(self.source, second.name))
        self.assertEqual(warmed, [(self.source, second.name)])

    def test_scan_rolls_back_restore_if_source_changes_during_metadata(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        saved = self.root / "saved.tif"
        shutil.copy2(original, saved)
        self.scan()
        image = self.cat.image_id_for(self.source, original.name)
        self.cat.save_state(image, {"rating": 5})
        original.unlink()
        self.scan()
        shutil.copy2(saved, original)

        def replace_during_metadata(path):
            shutil.copyfile(replacement, path)
            return {"metadata_version": catalog_scan.METADATA_VERSION, "width": 999}

        warmed = []
        with mock.patch.object(catalog_scan, "read_metadata", side_effect=replace_during_metadata):
            result = catalog_scan.scan_source(self.cat, self.source,
                on_local_file=lambda *args: warmed.append(args))
        self.assertFalse(result["complete"])
        self.assertEqual(result["updated"], 0)
        row = self.cat.connection.execute("SELECT missing, width FROM files WHERE id=?",
            (self.cat.image_row(image)["file_id"],)).fetchone()
        self.assertTrue(row["missing"])
        self.assertNotEqual(row["width"], 999)
        self.assertEqual(self.cat.state_for(image)["rating"], 5)
        self.assertEqual(warmed, [])

    def test_later_record_cannot_publish_an_earlier_changed_source(self):
        collision_pair(self.photos / "first.tif", self.photos / "second.tif")
        inspected = []

        def replace_earlier_during_metadata(path):
            inspected.append(path)
            if len(inspected) == 2:
                shutil.copyfile(path, inspected[0])
            return {"metadata_version": catalog_scan.METADATA_VERSION}

        warmed = []
        with mock.patch.object(catalog_scan, "read_metadata", side_effect=replace_earlier_during_metadata):
            result = catalog_scan.scan_source(self.cat, self.source,
                on_local_file=lambda *args: warmed.append(args))
        self.assertEqual(len(inspected), 2)
        self.assertFalse(result["complete"])
        self.assertEqual((result["added"], result["updated"], result["relinked"]), (0, 0, 0))
        self.assertEqual(self.cat.query()["total"], 0)
        self.assertEqual(warmed, [])
        self.assertEqual(self.scan()["added"], 2)

    def test_hash_does_not_open_cloud_placeholder(self):
        original = self.photos / "original.tif"
        collision_pair(original, self.root / "other.tif")
        with mock.patch("media_availability.from_stat", return_value="cloud-only"), \
                mock.patch.object(Path, "open", side_effect=AssertionError("hydration")):
            with self.assertRaises(OSError):
                file_identity.content_hash(original)

    def test_register_rejects_source_replaced_during_metadata(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)

        def replace_during_metadata(path):
            shutil.copyfile(replacement, path)
            return {"metadata_version": catalog_scan.METADATA_VERSION}

        with mock.patch.object(catalog_scan, "read_metadata", side_effect=replace_during_metadata):
            with self.assertRaises(OSError):
                catalog_scan.register_file(self.cat, original)
        self.assertEqual(self.cat.query()["total"], 0)

    def test_register_rolls_back_source_replaced_during_upsert(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        actual_upsert = self.cat.upsert_file

        def replace_during_upsert(connection, source, record):
            result = actual_upsert(connection, source, record)
            shutil.copyfile(replacement, record["path"])
            return result

        with mock.patch.object(self.cat, "upsert_file", side_effect=replace_during_upsert):
            with self.assertRaises(OSError):
                catalog_scan.register_file(self.cat, original)
        self.assertEqual(self.cat.query()["total"], 0)
        self.assertTrue(self.cat.integrity_ok())

    def test_watcher_does_not_apply_old_digest_to_a_new_revision(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        old_digest = "full:" + file_identity.content_hash(original)
        watch = {"id": "test", "path": str(self.photos), "presetId": "look"}
        service = watch_workflow.WatchService(self.cat, lambda: [watch],
            presets=lambda: [{"id": "look", "grade": {"exposure": 1}}])
        service.poll_once()
        actual_hash = file_identity.content_hash
        calls = []

        def replace_after_watch_hash(path, **options):
            digest = actual_hash(path, **options)
            calls.append(path)
            if len(calls) == 1:
                shutil.copyfile(replacement, path)
            return digest

        with mock.patch.object(file_identity, "content_hash", side_effect=replace_after_watch_hash):
            service.poll_once()
        self.assertFalse(self.cat.watch_handled("test", old_digest))
        self.assertEqual(self.cat.query()["total"], 0)
        self.assertEqual(service.status[0]["handled"], 0)
        self.assertTrue(service.status[0]["error"])
        service.poll_once(); service.poll_once()
        self.assertEqual(service.status[0]["handled"], 1)

    def test_watcher_guards_preset_and_ledger_after_registration(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        old_digest = "full:" + file_identity.content_hash(original)
        watch = {"id": "test", "path": str(self.photos), "presetId": "look"}
        service = watch_workflow.WatchService(self.cat, lambda: [watch],
            presets=lambda: [{"id": "look", "grade": {"exposure": 1}}])
        service.poll_once()
        actual_register = catalog_scan.register_file

        def replace_after_registration(*args, **options):
            result = actual_register(*args, **options)
            shutil.copyfile(replacement, original)
            return result

        with mock.patch.object(catalog_scan, "register_file", side_effect=replace_after_registration):
            service.poll_once()
        image = self.cat.image_id_for(self.source, original.name)
        self.assertIsNone(self.cat.state_for(image)["grade"])
        self.assertFalse(self.cat.watch_handled("test", old_digest))
        self.assertEqual(service.status[0]["handled"], 0)

    def test_watcher_rolls_back_preset_and_ledger_if_source_changes_during_save(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        old_digest = "full:" + file_identity.content_hash(original)
        watch = {"id": "test", "path": str(self.photos), "presetId": "look"}
        service = watch_workflow.WatchService(self.cat, lambda: [watch],
            presets=lambda: [{"id": "look", "grade": {"exposure": 1}}])
        service.poll_once()
        actual_save = self.cat._save_state

        def replace_during_save(connection, image_id, entry):
            actual_save(connection, image_id, entry)
            shutil.copyfile(replacement, original)

        with mock.patch.object(self.cat, "_save_state", side_effect=replace_during_save):
            service.poll_once()
        image = self.cat.image_id_for(self.source, original.name)
        self.assertIsNone(self.cat.state_for(image)["grade"])
        self.assertFalse(self.cat.watch_handled("test", old_digest))
        self.assertEqual(service.status[0]["handled"], 0)

    def test_ingest_watch_rejects_changed_original_or_copied_destination(self):
        for changed in ("original", "destination"):
            with self.subTest(changed=changed):
                original = self.root / ("card-" + changed) / "frame.tif"
                replacement = self.root / ("replacement-" + changed + ".tif")
                collision_pair(original, replacement)
                old_digest = "full:" + file_identity.content_hash(original)
                watch = {"id": "test-" + changed, "path": str(original.parent),
                    "mode": "ingest", "presetId": "look", "request": {
                        "destination": str(self.photos / changed), "folderTemplate": "",
                        "filenameTemplate": "{filename}", "verify": "hash"}}
                service = watch_workflow.WatchService(self.cat, lambda: [watch],
                    presets=lambda: [{"id": "look", "grade": {"exposure": 1}}])
                before = self.cat.query()["total"]
                service.poll_once()
                actual_copy = ingest.copy_item

                def replace_after_copy(*args, **options):
                    result = actual_copy(*args, **options)
                    self.assertTrue(result["ok"], result)
                    target = original if changed == "original" else Path(result["destination"])
                    shutil.copyfile(replacement, target)
                    return result

                with mock.patch.object(ingest, "copy_item", side_effect=replace_after_copy):
                    service.poll_once()
                self.assertFalse(self.cat.watch_handled(watch["id"], old_digest))
                self.assertEqual(self.cat.query()["total"], before)
                self.assertEqual(service.status[0]["handled"], 0)
                self.assertTrue(service.status[0]["error"])
                service.poll_once(); service.poll_once()
                self.assertEqual(service.status[0]["handled"], 1)
                self.assertEqual(self.cat.query()["total"], before + 1)

    def test_legacy_watch_ack_is_not_migrated_after_source_replacement(self):
        original, replacement = self.photos / "original.tif", self.root / "replacement.tif"
        collision_pair(original, replacement)
        self.scan()
        old_digest = "full:" + file_identity.content_hash(original)
        self.cat.record_watch_handled("test", ingest.header_hash(original))
        watch = {"id": "test", "path": str(self.photos)}
        service = watch_workflow.WatchService(self.cat, lambda: [watch])
        service.poll_once()
        actual_known = self.cat.ingest_content_hashes

        def replace_after_verification(items):
            result = actual_known(items)
            shutil.copyfile(replacement, original)
            return result

        with mock.patch.object(self.cat, "ingest_content_hashes", side_effect=replace_after_verification):
            service.poll_once()
        self.assertFalse(self.cat.watch_handled("test", old_digest))
        self.assertTrue(service.status[0]["error"])
