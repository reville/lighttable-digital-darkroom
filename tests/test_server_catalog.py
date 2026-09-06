"""The server's catalog wiring: name resolution, state routing, and exports."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import catalog as catalog_module
import catalog_scan
import server


def make_photo(path: Path, colour=(120, 90, 60)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), colour).save(path, quality=95)
    return path


class CatalogServerTestCase(unittest.TestCase):
    """Open a real catalog over a temporary folder, wired into the server."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "photos"
        self.root.mkdir()
        make_photo(self.root / "a.jpg")
        make_photo(self.root / "sub" / "b.jpg", (30, 140, 200))
        self.catalog = catalog_module.Catalog(
            Path(self._dir.name) / "library.sqlite3")
        self.source = self.catalog.add_source(self.root)
        catalog_scan.scan_source(self.catalog, self.source,
                                 read_metadata_for_new=False)
        self._patches = [
            mock.patch.object(server, "FOLDER", self.root),
            mock.patch.object(server, "CATALOG", self.catalog),
            mock.patch.object(server, "PRIMARY_SOURCE_ID", self.source),
            mock.patch.object(server, "CATALOG_MIRROR", False),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self.catalog.close()
        self._dir.cleanup()

    def qualified(self, relpath: str) -> str:
        return catalog_module.qualified_name(self.source, relpath)


class NameResolutionTests(CatalogServerTestCase):
    def test_qualified_name_resolves_to_the_right_file(self):
        path, source_id, relpath, copy_ident = server.resolve_name(
            self.qualified("sub/b.jpg"))
        self.assertEqual(path, (self.root / "sub" / "b.jpg").resolve())
        self.assertEqual((source_id, relpath, copy_ident),
                         (self.source, "sub/b.jpg", None))

    def test_legacy_relative_name_still_resolves(self):
        """A client that has not been updated must keep working."""
        path, source_id, _, _ = server.resolve_name("a.jpg")
        self.assertEqual(path, (self.root / "a.jpg").resolve())
        self.assertIsNone(source_id)

    def test_escaping_the_source_is_refused(self):
        with self.assertRaises(ValueError):
            server.resolve_name(self.qualified("../outside.jpg"))
        with self.assertRaises(ValueError):
            server.resolve_name("../../etc/passwd")

    def test_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            server.resolve_name(catalog_module.qualified_name(9999, "a.jpg"))

    def test_non_image_extension_is_refused(self):
        (self.root / "notes.txt").write_text("hello")
        with self.assertRaises(ValueError):
            server.resolve_name("notes.txt")

    def test_reveal_photo_returns_the_resolved_original(self):
        result = server.reveal_photo({"name": self.qualified("sub/b.jpg")})
        self.assertEqual(result, {
            "ok": True,
            "path": str((self.root / "sub" / "b.jpg").resolve()),
        })

    def test_reveal_photo_requires_a_selection(self):
        with self.assertRaisesRegex(ValueError, "no photo selected"):
            server.reveal_photo({})


class FileKeyTests(CatalogServerTestCase):
    def test_file_key_survives_a_rename(self):
        """Caches are keyed on content, so a rename must not orphan them."""
        before = server.file_key("a.jpg")
        moved = self.root / "renamed.jpg"
        os.rename(self.root / "a.jpg", moved)
        os.utime(moved, (0, 0))
        original = self.root / "a.jpg"
        os.rename(moved, original)
        os.utime(original, (0, 0))
        first = server.file_key("a.jpg")
        os.rename(original, moved)
        os.utime(moved, (0, 0))
        self.assertEqual(first, server.file_key("renamed.jpg"))
        self.assertNotEqual(before, "")

    def test_file_key_changes_when_content_changes(self):
        before = server.file_key("a.jpg")
        make_photo(self.root / "a.jpg", (10, 10, 10))
        self.assertNotEqual(before, server.file_key("a.jpg"))


class MergeStartTests(CatalogServerTestCase):
    def test_start_merge_initializes_phase_progress_before_queueing(self):
        initial = {
            "running": False, "mode": "", "progress": 0, "total": 0,
            "phase": "", "phaseProgress": 0, "phaseTotal": 0,
            "alignmentInliers": 0, "elapsedSeconds": 0.0,
            "timings": {}, "output": "", "error": "",
        }
        with mock.patch.dict(server.MERGE, initial, clear=True), \
                mock.patch.object(server.MERGE_POOL, "submit") as submit:
            result = server.start_merge({
                "mode": "panorama", "names": ["a.jpg", "sub/b.jpg"],
            })
            self.assertEqual(result, {"queued": 2, "mode": "panorama"})
            self.assertEqual(server.MERGE["phase"], "preparing")
            self.assertEqual(server.MERGE["phaseTotal"], 2)
            submit.assert_called_once()

class StateRoutingTests(CatalogServerTestCase):
    def test_state_is_written_to_and_read_from_the_catalog(self):
        name = self.qualified("a.jpg")
        server.save_image_state(name, {"rating": 4, "label": "red",
                                       "keywords": ["Rome"]})
        entry = server.catalog_entry_for(name)
        self.assertEqual(entry["rating"], 4)
        self.assertEqual(entry["label"], "red")
        self.assertEqual(entry["keywords"], ["Rome"])
        self.assertFalse((self.root / ".lighttable-state.json").exists(),
                         "the catalog must not write beside the originals")

    def test_versions_round_trip_through_the_catalog(self):
        name = self.qualified("a.jpg")
        server.save_image_state(name, {"versions": [
            {"id": "v1", "name": "Warm", "created": "2026-01-01",
             "grade": {"temp": 0.3}}]})
        entry = server.catalog_entry_for(name)
        self.assertEqual(len(entry["versions"]), 1)
        self.assertEqual(entry["versions"][0]["name"], "Warm")

    def test_label_cleaning_rejects_unknown_colours(self):
        self.assertEqual(server.clean_label("chartreuse"), "none")
        self.assertEqual(server.clean_label("BLUE"), "blue")
        self.assertEqual(server.clean_label(None), "none")

    def test_bulk_save_covers_every_named_image(self):
        names = [self.qualified("a.jpg"), self.qualified("sub/b.jpg")]
        server.save_image_states({n: {"label": "green"} for n in names})
        for name in names:
            self.assertEqual(server.catalog_entry_for(name)["label"], "green")


class SourceActionTests(CatalogServerTestCase):
    def test_add_source_scans_and_adopts_a_legacy_state_file(self):
        other = Path(self._dir.name) / "second"
        make_photo(other / "c.jpg")
        (other / ".lighttable-state.json").write_text(json.dumps(
            {"images": {"c.jpg": {"rating": 5, "status": "approved"}}}))
        result = server.catalog_sources_action({"action": "add",
                                                "path": str(other)})
        self.assertTrue(result["ok"])
        self.assertEqual(result["imported"]["images"], 1)
        new_id = result["sourceId"]
        entry = server.catalog_entry_for(
            catalog_module.qualified_name(new_id, "c.jpg"))
        self.assertEqual(entry["rating"], 5)

    def test_add_source_rejects_a_missing_folder(self):
        with self.assertRaises(ValueError):
            server.catalog_sources_action(
                {"action": "add", "path": str(self.root / "nope")})

    def test_favorite_and_rename_and_remove(self):
        server.catalog_sources_action({"action": "favorite",
                                       "id": self.source, "favorite": True})
        self.assertTrue(self.catalog.sources()[0]["favorite"])
        server.catalog_sources_action({"action": "rename", "id": self.source,
                                       "name": "Travel"})
        self.assertEqual(self.catalog.sources()[0]["name"], "Travel")
        result = server.catalog_sources_action({"action": "remove",
                                                "id": self.source})
        self.assertEqual(result["sources"], [])

    def test_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            server.catalog_sources_action({"action": "detonate"})


class CollectionActionTests(CatalogServerTestCase):
    def test_create_add_and_delete(self):
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        created = server.catalog_collections_action(
            {"action": "create", "name": "Picks", "imageIds": [image_id]})
        collection_id = created["id"]
        result = self.catalog.query({"scope": "collection",
                                     "collectionId": collection_id})
        self.assertEqual(result["total"], 1)
        server.catalog_collections_action({"action": "delete",
                                           "id": collection_id})
        self.assertEqual(self.catalog.collections(), [])

    def test_smart_collection_is_evaluated_by_the_catalog(self):
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.save_state(image_id, {"rating": 5})
        created = server.catalog_collections_action(
            {"action": "create_smart", "name": "Best",
             "rules": {"ratingMin": 4}})
        result = self.catalog.query({"scope": "collection",
                                     "collectionId": created["id"]})
        self.assertEqual(result["total"], 1)

    def test_legacy_library_commands_route_to_the_catalog(self):
        first = self.qualified("a.jpg")
        second = self.qualified("sub/b.jpg")
        created = server.update_library({
            "action": "create_collection", "name": "Picks",
            "members": [first],
        })
        collection_id = created["id"]
        self.assertEqual(self.catalog.query({
            "scope": "collection", "collectionId": collection_id,
        })["total"], 1)

        stack = server.update_library({
            "action": "create_stack", "name": "Pair",
            "members": [first, second],
        })
        self.assertTrue(any(item["id"] == str(stack["id"])
                            for item in stack["library"]["stacks"]))

        copied = server.update_library({
            "action": "create_virtual", "name": first,
            "displayName": "Alternate",
        })
        copy_name = copied["copy"]["name"]
        self.assertEqual(
            self.catalog.query({"filter": {"kind": "virtual"}})["total"], 1)
        server.update_library({"action": "delete_virtual", "name": copy_name})
        self.assertEqual(
            self.catalog.query({"filter": {"kind": "virtual"}})["total"], 0)
        self.assertFalse((self.root / ".lighttable-state.json").exists())


class SidecarImportTests(CatalogServerTestCase):
    SIDECAR = """<?xml version="1.0"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmp:Rating="4" xmp:Label="Blue"
    crs:Exposure2012="+0.50" crs:Contrast2012="+25">
   <dc:subject><rdf:Bag>
     <rdf:li>Rome</rdf:li><rdf:li>Travel</rdf:li>
   </rdf:Bag></dc:subject>
   <dc:rights><rdf:Alt>
     <rdf:li xml:lang="x-default">(c) 2026 Photographer</rdf:li>
   </rdf:Alt></dc:rights>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

    def test_metadata_is_imported_without_develop_settings(self):
        (self.root / "a.xmp").write_text(self.SIDECAR)
        report = server.import_sidecars(
            {"names": [self.qualified("a.jpg")],
             "apply": {"metadata": True, "develop": False}})
        self.assertEqual(report["read"], 1)
        self.assertEqual(report["applied"], 1)
        entry = server.catalog_entry_for(self.qualified("a.jpg"))
        self.assertEqual(entry["rating"], 4)
        self.assertEqual(entry["label"], "blue")
        self.assertEqual(sorted(entry["keywords"]), ["Rome", "Travel"])
        self.assertIsNone(entry["grade"])
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        self.assertEqual(self.catalog.iptc_for(image_id)["copyright"],
                         "(c) 2026 Photographer")

    def test_develop_settings_are_imported_when_asked(self):
        (self.root / "a.xmp").write_text(self.SIDECAR)
        server.import_sidecars({"names": [self.qualified("a.jpg")],
                                "apply": {"metadata": True, "develop": True}})
        entry = server.catalog_entry_for(self.qualified("a.jpg"))
        self.assertAlmostEqual(entry["grade"]["exposure"], 0.5, places=3)
        self.assertAlmostEqual(entry["grade"]["contrast"], 0.25, places=3)

    def test_the_second_sidecar_naming_form_is_found(self):
        (self.root / "a.jpg.xmp").write_text(self.SIDECAR)
        report = server.import_sidecars({"names": [self.qualified("a.jpg")]})
        self.assertEqual(report["read"], 1)

    def test_missing_sidecars_are_counted_not_fatal(self):
        report = server.import_sidecars({"names": [self.qualified("a.jpg")]})
        self.assertEqual(report["read"], 0)
        self.assertEqual(report["missing"], 1)


class RenameTests(CatalogServerTestCase):
    def test_preview_does_not_touch_the_filesystem(self):
        result = server.rename_photos(
            {"names": [self.qualified("a.jpg")], "template": "Trip_{sequence}",
             "preview": True})
        self.assertEqual(result["total"], 1)
        self.assertTrue((self.root / "a.jpg").exists())

    def test_rename_moves_the_file_and_updates_the_catalog(self):
        name = self.qualified("a.jpg")
        server.save_image_state(name, {"rating": 3})
        result = server.rename_photos(
            {"names": [name], "template": "Trip_{sequence}", "start": 7})
        self.assertEqual(result["renamed"], 1)
        self.assertTrue((self.root / "Trip_0007.jpg").exists())
        self.assertFalse((self.root / "a.jpg").exists())
        moved = self.catalog.query({"filter": {"query": "Trip"}})
        self.assertEqual(moved["total"], 1)
        self.assertEqual(moved["items"][0]["rating"], 3)

    def test_camera_and_date_tokens_describe_each_photo(self):
        """Every photo is described itself, not the first one in its folder."""
        import ingest_workflow

        def describe(path):
            path = Path(path)
            return {"name": path.name, "camera": f"Cam {path.stem.upper()}",
                    "captureTime": "2024-03-09T10:11:12", "mtime": 0.0}

        names = [self.qualified("a.jpg"), self.qualified("sub/b.jpg")]
        with mock.patch.object(ingest_workflow, "describe_file",
                               side_effect=describe):
            result = server.rename_photos({
                "names": names,
                "template": "{camera}_{yyyy}{mm}{dd}_{sequence}"})
        self.assertEqual(result["renamed"], 2)
        self.assertTrue((self.root / "Cam A_20240309_0001.jpg").is_file())
        self.assertTrue(
            (self.root / "sub" / "Cam B_20240309_0002.jpg").is_file())

    def test_a_template_without_values_keeps_the_original_name(self):
        """Empty tokens must never rename a photo to a hidden ".jpg"."""
        result = server.rename_photos(
            {"names": [self.qualified("a.jpg")], "template": "{camera}"})
        self.assertEqual(result["renamed"], 0)
        self.assertTrue((self.root / "a.jpg").is_file())
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith(".")], [])

    def test_a_folder_separator_in_the_template_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot create folders"):
            server.rename_photos({"names": [self.qualified("a.jpg")],
                                  "template": "{yyyy}/{filename}"})
        self.assertTrue((self.root / "a.jpg").is_file())

    def test_a_virtual_copy_and_its_original_rename_the_file_once(self):
        original = self.qualified("a.jpg")
        created = server.update_library(
            {"action": "create_virtual", "name": original})
        result = server.rename_photos(
            {"names": [original, created["copy"]["name"]],
             "template": "Trip_{sequence}"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["renamed"], 1)
        self.assertTrue((self.root / "Trip_0001.jpg").is_file())
        self.assertFalse((self.root / "Trip_0002.jpg").exists())

    def test_rename_carries_the_sidecar(self):
        (self.root / "a.xmp").write_text("<x/>")
        server.rename_photos({"names": [self.qualified("a.jpg")],
                              "template": "Named"})
        self.assertTrue((self.root / "Named.xmp").exists())

    def test_rename_records_an_undo_batch(self):
        result = server.rename_photos({"names": [self.qualified("a.jpg")],
                                       "template": "One"})
        rows = self.catalog.connection.execute(
            "SELECT old_relpath, new_relpath FROM rename_log WHERE batch=?",
            (result["batch"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old_relpath"], "a.jpg")

    def test_sidecar_collision_refuses_the_rename_without_overwriting(self):
        name = self.qualified("a.jpg")
        (self.root / "a.xmp").write_text("source metadata")
        (self.root / "Named.xmp").write_text("unrelated metadata")

        result = server.rename_photos({"names": [name], "template": "Named"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["renamed"], 0)
        self.assertTrue((self.root / "a.jpg").is_file())
        self.assertEqual((self.root / "a.xmp").read_text(), "source metadata")
        self.assertEqual((self.root / "Named.xmp").read_text(),
                         "unrelated metadata")
        self.assertIsNotNone(self.catalog.image_id_for(self.source, "a.jpg"))

    def test_a_destination_appearing_after_preflight_is_never_overwritten(self):
        source = self.root / "a.jpg"
        target = self.root / "Named.jpg"
        original = source.read_bytes()
        plan = server._photo_move_plan(source, target)
        target.write_bytes(b"raced in")

        with self.assertRaises(FileExistsError):
            server._stage_photo_move(plan)

        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(target.read_bytes(), b"raced in")

    def test_catalog_failure_rolls_back_the_photo_and_sidecar_move(self):
        name = self.qualified("a.jpg")
        (self.root / "a.xmp").write_text("metadata")

        with mock.patch.object(self.catalog, "relocate_files",
                               side_effect=OSError("database full")):
            result = server.rename_photos({"names": [name], "template": "Named"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["renamed"], 0)
        self.assertTrue((self.root / "a.jpg").is_file())
        self.assertEqual((self.root / "a.xmp").read_text(), "metadata")
        self.assertFalse((self.root / "Named.jpg").exists())
        self.assertFalse((self.root / "Named.xmp").exists())
        self.assertIsNotNone(self.catalog.image_id_for(self.source, "a.jpg"))


class TrashTests(CatalogServerTestCase):
    def test_trash_returns_paths_for_the_host_and_deletes_nothing(self):
        (self.root / "a.xmp").write_text("<x/>")
        result = server.trash_photos({"names": [self.qualified("a.jpg")]})
        self.assertIn(str((self.root / "a.jpg").resolve()), result["paths"])
        self.assertEqual(len(result["paths"]), 2)
        self.assertTrue((self.root / "a.jpg").exists(),
                        "the server must never unlink an original itself")

    def test_unknown_names_are_ignored(self):
        result = server.trash_photos({"names": ["nope.jpg"]})
        self.assertEqual(result["paths"], [])


class ExportMetadataTests(CatalogServerTestCase):
    def test_fields_come_from_the_catalog(self):
        name = self.qualified("a.jpg")
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.save_iptc(image_id, {"creator": "Nicholas",
                                          "copyright": "(c) 2026"})
        server.save_image_state(name, {"rating": 5, "label": "red",
                                       "keywords": ["Places > Italy"]})
        fields = server.export_metadata_fields(name)
        self.assertEqual(fields["creator"], "Nicholas")
        self.assertEqual(fields["rating"], 5)
        self.assertEqual(fields["label"], "red")
        self.assertEqual(fields["keywords"], ["Italy"])
        self.assertEqual(fields["keywordPaths"], ["Places > Italy"])

    def test_metadata_policy_none_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.jpg"
            make_photo(target)
            before = target.read_bytes()
            self.assertFalse(server.embed_export_metadata(
                target, {"metadata": "none"}))
            self.assertEqual(target.read_bytes(), before)

    def test_export_candidates_cover_all_catalog_sources_and_current_state(self):
        other = Path(self._dir.name) / "other"
        make_photo(other / "c.jpg", (220, 30, 80))
        other_source = self.catalog.add_source(other)
        catalog_scan.scan_source(self.catalog, other_source,
                                 read_metadata_for_new=False)
        first = self.qualified("a.jpg")
        third = catalog_module.qualified_name(other_source, "c.jpg")
        server.save_image_state(first, {"status": "approved", "rating": 5})
        server.save_image_state(third, {"status": "approved", "rating": 4})

        candidates = {name: entry for name, entry, _ in
                      server.export_candidates()}
        self.assertEqual(set(candidates), {
            self.qualified("a.jpg"), self.qualified("sub/b.jpg"), third,
        })
        self.assertEqual(candidates[first]["rating"], 5)
        self.assertEqual(candidates[third]["rating"], 4)

    def test_queueing_export_defers_metadata_and_lens_work_to_worker(self):
        name = self.qualified("a.jpg")
        entry = {
            "status": "approved", "rating": 5, "params": None,
            "grade": None, "crop": None, "masks": [], "heals": [],
            "optics": {},
        }

        class RecordingPool:
            def __init__(self):
                self.calls = []

            def submit(self, *args):
                self.calls.append(args)

        pool = RecordingPool()
        with server.EXPORT_LOCK:
            server.EXPORT["running"] = False
        try:
            with mock.patch.object(server, "export_candidates", return_value=[
                    (name, entry, "a")]), \
                    mock.patch.object(server, "EXPORT_POOL", pool), \
                    mock.patch.object(
                        server, "effective_new_photo_defaults",
                        return_value=({"profile_enabled": False},
                                      {"exposure": 0.25})), \
                    mock.patch.object(server, "exif_for") as exif, \
                    mock.patch.object(server.edits, "lens_profile_for") as lens:
                result = server.start_export({
                    "which": "approved", "destination": "test-exports",
                })
            self.assertEqual(result["queued"], 1)
            self.assertEqual(len(pool.calls), 1)
            exif.assert_not_called()
            lens.assert_not_called()
            queued_job = pool.calls[0][2]
            self.assertFalse(queued_job["params"]["profile_enabled"])
            self.assertEqual(queued_job["grade"]["exposure"], 0.25)
            self.assertNotIn("metadataFields", queued_job)
            self.assertNotIn("lensProfile", queued_job)
        finally:
            with server.EXPORT_LOCK:
                server.EXPORT["running"] = False

    def test_export_specific_names_and_selection(self):
        class RecordingPool:
            def __init__(self):
                self.calls = []

            def submit(self, *args):
                self.calls.append(args)

        pool = RecordingPool()
        cand1 = ("DSC_0001.JPG", {"status": "pending", "rating": 0, "params": {}, "grade": {}, "crop": None, "masks": [], "heals": [], "optics": {}, "keywords": [], "provenance": None}, "DSC_0001")
        cand2 = ("DSC_0002.JPG", {"status": "approved", "rating": 5, "params": {}, "grade": {}, "crop": None, "masks": [], "heals": [], "optics": {}, "keywords": [], "provenance": None}, "DSC_0002")
        with server.EXPORT_LOCK:
            server.EXPORT["running"] = False
        try:
            with mock.patch.object(server, "export_candidates", return_value=[cand1, cand2]), \
                    mock.patch.object(server, "EXPORT_POOL", pool):
                # Target cand1 specifically by name even though its status is pending
                res = server.start_export({"names": ["DSC_0001.JPG"], "destination": "test-exports"})
                self.assertEqual(res["queued"], 1)
                self.assertEqual(len(pool.calls), 1)
                self.assertEqual(pool.calls[0][1], "DSC_0001.JPG")
                self.assertEqual(pool.calls[0][2]["sequence"], 1)
        finally:
            with server.EXPORT_LOCK:
                server.EXPORT["running"] = False

    def test_empty_explicit_selection_never_falls_through_to_export_all(self):
        class RecordingPool:
            def __init__(self):
                self.calls = []

            def submit(self, *args):
                self.calls.append(args)

        pool = RecordingPool()
        candidate = ("DSC_0001.JPG", {"status": "approved", "rating": 5,
                     "params": {}, "grade": {}, "crop": None, "masks": [],
                     "heals": [], "optics": {}, "keywords": [],
                     "provenance": None}, "DSC_0001")
        with server.EXPORT_LOCK:
            server.EXPORT["running"] = False
        try:
            with mock.patch.object(server, "export_candidates", return_value=[candidate]), \
                    mock.patch.object(server, "EXPORT_POOL", pool):
                result = server.start_export({"which": "selected", "names": [],
                                              "destination": "test-exports"})
                self.assertEqual(result["queued"], 0)
                self.assertEqual(pool.calls, [])
        finally:
            with server.EXPORT_LOCK:
                server.EXPORT["running"] = False

    def test_failed_overwrite_export_preserves_the_previous_complete_file(self):
        output = Path(self._dir.name) / "exports"
        output.mkdir()
        target = output / "delivery.jpg"
        target.write_bytes(b"previous complete delivery")
        cache = Path(self._dir.name) / "cache"
        cache.mkdir()
        job = {
            "destination": str(output), "params": dict(server.fp.DEFAULT_PARAMS),
            "format": "jpeg", "filenameTemplate": "delivery",
            "collision": "overwrite", "sidecar": False,
        }

        def failed_render(command, **_kwargs):
            Path(command[3]).write_bytes(b"partial replacement")
            return mock.Mock(returncode=1, stderr="renderer stopped")

        with server.EXPORT_LOCK:
            server.EXPORT.update(total=1, done=0, errors=[], running=True, log=[])
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(server, "RUST_WORKER_BIN", None), \
                mock.patch.object(server, "tiff_for", return_value=cache / "in.tif"), \
                mock.patch.object(server, "exif_for", return_value={}), \
                mock.patch.object(server.edits, "lens_profile_for", return_value=None), \
                mock.patch.object(server.subprocess, "run", side_effect=failed_render):
            server.export_one(self.qualified("a.jpg"), job)

        self.assertEqual(target.read_bytes(), b"previous complete delivery")
        self.assertEqual(list(output.glob(".*.export.*")), [])
        self.assertTrue(any("renderer stopped" in item
                            for item in server.EXPORT["errors"]))

    def test_rename_export_never_clobbers_a_destination_that_races_in(self):
        output = Path(self._dir.name) / "exports"
        output.mkdir()
        target = output / "delivery.jpg"
        cache = Path(self._dir.name) / "cache"
        cache.mkdir()
        job = {
            "destination": str(output), "params": dict(server.fp.DEFAULT_PARAMS),
            "format": "jpeg", "filenameTemplate": "delivery",
            "collision": "rename", "sidecar": False,
        }

        def raced_render(command, **_kwargs):
            Path(command[3]).write_bytes(b"completed export")
            target.write_bytes(b"another process won")
            return mock.Mock(returncode=0, stderr="")

        with server.EXPORT_LOCK:
            server.EXPORT.update(total=1, done=0, errors=[], running=True, log=[])
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(server, "RUST_WORKER_BIN", None), \
                mock.patch.object(server, "tiff_for", return_value=cache / "in.tif"), \
                mock.patch.object(server, "exif_for", return_value={}), \
                mock.patch.object(server.edits, "lens_profile_for", return_value=None), \
                mock.patch.object(server.subprocess, "run",
                                  side_effect=raced_render):
            server.export_one(self.qualified("a.jpg"), job)

        self.assertEqual(target.read_bytes(), b"another process won")
        self.assertEqual(list(output.glob(".*.export.*")), [])
        self.assertTrue(server.EXPORT["errors"])

    def test_each_fallback_export_uses_an_isolated_job_file(self):
        output = Path(self._dir.name) / "exports"
        output.mkdir()
        cache = Path(self._dir.name) / "cache"
        cache.mkdir()
        seen = []

        def completed_render(command, **_kwargs):
            job_path = Path(command[4])
            seen.append((job_path, json.loads(job_path.read_text())))
            Path(command[3]).write_bytes(b"completed export")
            return mock.Mock(returncode=0, stderr="")

        base = {
            "destination": str(output),
            "params": dict(server.fp.DEFAULT_PARAMS),
            "format": "jpeg", "filenameTemplate": "delivery",
            "collision": "rename", "sidecar": False,
        }
        with server.EXPORT_LOCK:
            server.EXPORT.update(total=2, done=0, errors=[], running=True, log=[])
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(server, "RUST_WORKER_BIN", None), \
                mock.patch.object(server, "tiff_for", return_value=cache / "in.tif"), \
                mock.patch.object(server, "exif_for", return_value={}), \
                mock.patch.object(server.edits, "lens_profile_for", return_value=None), \
                mock.patch.object(server.subprocess, "run",
                                  side_effect=completed_render):
            server.export_one(self.qualified("a.jpg"), dict(base, marker="first"))
            server.export_one(self.qualified("a.jpg"), dict(base, marker="second"))

        self.assertEqual([payload["marker"] for _, payload in seen],
                         ["first", "second"])
        self.assertEqual(len({path for path, _ in seen}), 2)
        self.assertTrue(all(not path.exists() for path, _ in seen))
        self.assertEqual(server.EXPORT["errors"], [])

    def test_fallback_export_names_the_capture_as_metadata_source(self):
        """The worker gets an intermediate TIFF, so the job must name the
        original capture or the export loses the camera EXIF the resident
        engine path embeds."""
        output = Path(self._dir.name) / "exports"
        output.mkdir()
        cache = Path(self._dir.name) / "cache"
        cache.mkdir()
        seen = []

        def completed_render(command, **_kwargs):
            seen.append(json.loads(Path(command[4]).read_text()))
            Path(command[3]).write_bytes(b"completed export")
            return mock.Mock(returncode=0, stderr="")

        name = self.qualified("a.jpg")
        job = {
            "destination": str(output), "sourceName": name,
            "params": dict(server.fp.DEFAULT_PARAMS),
            "format": "jpeg", "filenameTemplate": "delivery",
            "collision": "rename", "sidecar": False,
            "metadata": "all-except-location",
        }
        with server.EXPORT_LOCK:
            server.EXPORT.update(total=1, done=0, errors=[], running=True, log=[])
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(server, "RUST_WORKER_BIN", None), \
                mock.patch.object(server, "tiff_for", return_value=cache / "in.tif"), \
                mock.patch.object(server, "exif_for", return_value={}), \
                mock.patch.object(server.edits, "lens_profile_for", return_value=None), \
                mock.patch.object(server.subprocess, "run",
                                  side_effect=completed_render):
            server.export_one(name, job)

        self.assertEqual(server.EXPORT["errors"], [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["metadataSource"], str(server.src_path(name)))
        self.assertEqual(seen[0]["metadata"], "all-except-location")


class PayloadAndCacheTests(CatalogServerTestCase):
    def test_catalog_boot_payload_is_lean_and_reports_total(self):
        for name in (self.qualified("a.jpg"), self.qualified("sub/b.jpg")):
            server.save_image_state(name, {
                "masks": [{"type": "radial"}],
            })
        rows, snapshot = server.library_payload(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(snapshot["total"], 2)
        self.assertFalse(rows[0]["stateLoaded"])
        self.assertTrue(rows[0]["hasEdits"])
        for blob in ("params", "grade", "crop", "masks", "heals", "optics"):
            self.assertNotIn(blob, rows[0])

    def test_browser_payload_and_queries_hide_catalogued_videos(self):
        (self.root / "clip.mov").write_bytes(b"catalogued for later")
        catalog_scan.scan_source(self.catalog, self.source,
                                 read_metadata_for_new=False)

        self.assertEqual(
            self.catalog.query({"filter": {"kind": "video"}})["total"], 1)
        rows, snapshot = server.library_payload(limit=20)
        video_query = server.browser_catalog_query({
            "limit": 20, "filter": {"kind": "video"},
        })

        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["kind"] != "video" for row in rows))
        self.assertEqual(video_query["total"], 0)

    def test_raw_thumbnail_uses_embedded_preview_without_display_decode(self):
        raw = self.root / "raw.dng"
        raw.write_bytes(b"not-a-real-raw")
        catalog_scan.scan_source(self.catalog, self.source,
                                 read_metadata_for_new=False)
        output = Path(self._dir.name) / "thumb" / "raw.jpg"
        output.parent.mkdir()
        pixels = np.zeros((18, 24, 3), dtype=np.uint8)
        with mock.patch.object(server.color_pipeline, "raw_embedded_preview",
                               return_value=pixels) as embedded, \
                mock.patch.object(server, "raw_display") as display, \
                mock.patch.object(server, "prune_cache_throttled"):
            payload = server._build_thumb(self.qualified("raw.dng"), output)
        self.assertTrue(payload.startswith(b"\xff\xd8"))
        embedded.assert_called_once()
        display.assert_not_called()

    def test_edited_thumbnail_key_changes_with_visual_state(self):
        name = self.qualified("a.jpg")
        initial = server.edited_thumbnail_key(name)

        server.save_image_state(name, {"grade": {"exposure": 0.75}})

        self.assertNotEqual(initial, server.edited_thumbnail_key(name))

    def test_edited_thumbnail_uses_current_state_and_caches_the_result(self):
        name = self.qualified("a.jpg")
        cache = Path(self._dir.name) / "cache"
        server.save_image_state(name, {
            "grade": {"exposure": 0.75},
            "crop": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        })
        rendered = Image.new("RGB", (512, 384), (20, 40, 60))
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(
                    server, "_accurate_thumbnail_base_ready",
                    return_value=True,
                ), \
                mock.patch.object(
                    server, "program_render_image", return_value=rendered,
                ) as render, \
                mock.patch.object(server, "prune_cache_throttled"):
            first = server.edited_thumbnail(name)
            second = server.edited_thumbnail(name)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        render.assert_called_once()
        body = render.call_args.args[0]
        self.assertEqual(render.call_args.kwargs["priority"], "prefetch")
        self.assertEqual(body["state"]["grade"]["exposure"], 0.75)
        self.assertEqual(body["state"]["crop"]["w"], 0.8)
        with Image.open(first[0]) as image:
            self.assertEqual(max(image.size), server.EDITED_THUMB_OUTPUT_EDGE)

    def test_edited_thumbnail_waits_for_accurate_base(self):
        name = self.qualified("a.jpg")
        cache = Path(self._dir.name) / "cache"
        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(
                    server, "_accurate_thumbnail_base_ready",
                    return_value=False,
                ), \
                mock.patch.object(server, "program_render_image") as render:
            self.assertIsNone(server.edited_thumbnail(name))
        render.assert_not_called()

    def test_render_cache_prunes_complete_old_bundles(self):
        folder = Path(self._dir.name) / "render-cache"
        folder.mkdir()
        for stem in ("old", "new"):
            for suffix in ("jpg", "rgba", "json"):
                (folder / f"{stem}.{suffix}").write_bytes(b"12345")
        for path in folder.glob("old.*"):
            os.utime(path, (1, 1))
        for path in folder.glob("new.*"):
            os.utime(path, (2, 2))
        server.prune_render_cache(folder, 15)
        self.assertEqual({path.name for path in folder.iterdir()},
                         {"new.jpg", "new.rgba", "new.json"})

    def test_truncated_render_surface_is_discarded_for_regeneration(self):
        folder = Path(self._dir.name) / "render-cache"
        folder.mkdir()
        meta = folder / "frame.json"
        jpg = folder / "frame.jpg"
        native = folder / "frame.rgba"
        meta.write_text('{"ms": 1}')
        native.write_bytes(server.NATIVE_SURFACE_HEADER.pack(
            server.NATIVE_SURFACE_MAGIC, 2, 2, 8) + b"short")

        self.assertEqual(
            server._render_bundle_metadata(meta, jpg, native), {"ms": 1})
        self.assertFalse(native.exists())

    def test_corrupt_render_metadata_is_discarded_for_regeneration(self):
        folder = Path(self._dir.name) / "render-cache"
        folder.mkdir()
        meta = folder / "frame.json"
        meta.write_text("{")

        self.assertIsNone(server._render_bundle_metadata(
            meta, folder / "frame.jpg", folder / "frame.rgba"))
        self.assertFalse(meta.exists())

    def test_failed_tiff_rebuild_preserves_the_previous_complete_cache(self):
        cache = Path(self._dir.name) / "cache"
        (cache / "tiff").mkdir(parents=True)
        target = cache / "tiff" / (
            f"v{server.INPUT_CACHE_VERSION}_key_romm.tif")
        Image.new("RGB", (8, 6), (120, 90, 60)).save(target, "TIFF")
        previous = target.read_bytes()
        os.utime(target, (1, 1))

        def fail_after_partial(_source, output, **_kwargs):
            Path(output).write_bytes(b"partial")
            raise OSError("converter stopped")

        with mock.patch.object(server, "CACHE", cache), \
                mock.patch.object(server, "file_key", return_value="key"), \
                mock.patch.object(
                    server.platform_image, "convert_processed_to_tiff",
                    side_effect=fail_after_partial,
                ):
            with self.assertRaises(OSError):
                server.tiff_for(self.qualified("a.jpg"))

        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list((cache / "tiff").glob(".*.decode.*")), [])


class HistoryTests(CatalogServerTestCase):
    def test_history_records_and_restores(self):
        name = self.qualified("a.jpg")
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.add_history(image_id, "Exposure",
                                 {"grade": {"exposure": 0.4}})
        steps = self.catalog.history_for(image_id)
        self.assertEqual(steps[0]["label"], "Exposure")
        state = self.catalog.history_state(steps[0]["id"])
        self.assertAlmostEqual(state["grade"]["exposure"], 0.4)


class FolderRowTests(CatalogServerTestCase):
    def test_folder_counts_come_from_sql(self):
        rows = server.catalog_folder_rows(self.source)
        by_path = {row["relpath"]: row["count"] for row in rows}
        self.assertEqual(by_path[""], 1)
        self.assertEqual(by_path["sub"], 1)

    def test_folder_counts_ignore_hidden_videos(self):
        (self.root / "clip.mov").write_bytes(b"root clip")
        (self.root / "sub" / "clip.mp4").write_bytes(b"nested clip")
        catalog_scan.scan_source(self.catalog, self.source,
                                 read_metadata_for_new=False)

        rows = server.catalog_folder_rows(self.source)
        by_path = {row["relpath"]: row["count"] for row in rows}
        self.assertEqual(by_path[""], 1)
        self.assertEqual(by_path["sub"], 1)

class CatalogOpenRecoveryTests(unittest.TestCase):
    def test_newer_catalog_is_left_unchanged_and_never_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "library.sqlite3"
            cat = catalog_module.Catalog(catalog_path)
            cat.backup()
            with cat.write() as connection:
                connection.execute(
                    "UPDATE meta SET value='999' WHERE key='schema_version'")
            cat.close()

            with mock.patch.object(server, "CATALOG_ENABLED", True), \
                    mock.patch.object(server, "CATALOG", None), \
                    mock.patch.object(server, "SCANNER", None), \
                    mock.patch.object(server, "PRIMARY_SOURCE_ID", None), \
                    mock.patch.object(server, "CATALOG_NOTICE", None), \
                    mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(
                        catalog_module, "default_catalog_path",
                        return_value=catalog_path,
                    ), \
                    mock.patch.object(
                        catalog_module, "recover_latest_backup",
                    ) as recover:
                self.assertIsNone(server.open_catalog())
                recover.assert_not_called()
                self.assertIn("newer", server.CATALOG_NOTICE["message"])

            connection = __import__("sqlite3").connect(catalog_path)
            try:
                version = connection.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, "999")

class FolderModeTests(unittest.TestCase):
    """With the catalog off, everything falls back to the per-folder file."""

    def test_state_uses_the_folder_file_when_the_catalog_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_photo(root / "a.jpg")
            with mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(server, "CATALOG", None):
                server.save_image_state("a.jpg", {"rating": 2})
                self.assertEqual(
                    server.catalog_entry_for("a.jpg")["rating"], 2)
                self.assertTrue((root / ".lighttable-state.json").exists())

    def test_require_catalog_explains_itself(self):
        with mock.patch.object(server, "CATALOG", None):
            with self.assertRaises(ValueError) as caught:
                server.require_catalog()
            self.assertIn("folder mode", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
