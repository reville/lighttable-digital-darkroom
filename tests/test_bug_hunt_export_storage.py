# SPDX-License-Identifier: GPL-3.0-only
"""Boundary and interruption probes found by the September bug hunt."""
import json
import os
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from xml.dom import minidom

import durable_io
import export_workflow
import xmp_sidecar


class LongOutputNamesTests(unittest.TestCase):
    def test_atomic_replacement_supports_a_valid_long_filename_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / ("a" * 230 + ".json")
            target.write_text('{"rating": 2}')
            durable_io.atomic_write_json(target, {"rating": 4})
            self.assertEqual(json.loads(target.read_text()), {"rating": 4})
            self.assertEqual(json.loads(durable_io.backup_path(target).read_text()),
                             {"rating": 2})
            self.assertEqual(set(Path(folder).iterdir()),
                             {target, durable_io.backup_path(target)})

    def test_long_original_name_can_write_and_update_its_xmp(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / ("a" * 247 + ".RAF")
            source.write_bytes(b"unchanged original")
            errors = []
            for rating in (2, 4):
                self.assertTrue(xmp_sidecar.write_sidecar(source, {"rating": rating}, errors),
                                errors)
                self.assertEqual(xmp_sidecar.read_sidecar(source)["rating"], rating)
            self.assertEqual(source.read_bytes(), b"unchanged original")
            self.assertEqual(xmp_sidecar.parse(
                durable_io.backup_path(source.with_suffix(".xmp")).read_text())["rating"], 2)

    def test_long_backup_names_stay_distinct_and_recover_the_right_document(self):
        with tempfile.TemporaryDirectory() as folder:
            targets = [Path(folder) / ("a" * 244 + ending + ".json") for ending in ("1", "2")]
            for index, target in enumerate(targets):
                durable_io.atomic_write_json(target, {"photo": index, "rating": 2})
                durable_io.atomic_write_json(target, {"photo": index, "rating": 4})
                target.write_text("{interrupted")
                self.assertEqual(durable_io.load_json(target, {}), {"photo": index, "rating": 2})
            self.assertNotEqual(durable_io.backup_path(targets[0]), durable_io.backup_path(targets[1]))

    def test_unicode_export_names_leave_room_for_sidecars_on_byte_limited_volumes(self):
        # APFS accepts these longer Unicode names, but byte-limited destination
        # filesystems reject the same recipe. The filename contract is portable.
        for stem in ("写真" * 60, "🍊" * 120):
            name = export_workflow.render_filename("{filename}", {"filename": stem}, "png")
            self.assertLessEqual(len((name + ".lighttable.json").encode("utf-8")), 255)
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / name
                staged = durable_io.temporary_path(output, "export")
                self.assertLessEqual(len(staged.name.encode("utf-8")), 255)
                staged.write_bytes(b"complete output")
                durable_io.publish_file(staged, output)
                durable_io.atomic_create_json(Path(str(output) + ".lighttable.json"),
                                             {"output": name})
                self.assertEqual(output.read_bytes(), b"complete output")

    def test_unicode_ingest_templates_fit_portable_components_and_copy_real_photos(self):
        import ingest_workflow as ingest
        from PIL import Image

        recipes = (
            {"custom": "🍊" * 60, "folderTemplate": "{custom}{custom}",
             "filenameTemplate": "{custom}{custom}"},
            {"folderTemplate": "{filename}{filename}{filename}",
             "filenameTemplate": "{filename}{filename}{filename}"},
            {"folderTemplate": "旅" * 120, "filenameTemplate": "海" * 120},
        )
        for index, recipe in enumerate(recipes):
            with self.subTest(recipe=index), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                card = root / "card"
                card.mkdir()
                sources = []
                for number in (1, 2):
                    source = card / ("写真" * 20 + str(number) + ".JPG")
                    Image.new("RGB", (12, 8), (20 * number, 70, 120)).save(source)
                    source.with_suffix(".xmp").write_text(xmp_sidecar.build_sidecar({"rating": number}))
                    sources.append(source)
                originals = {path: path.read_bytes() for path in card.iterdir()}
                destination, backup = root / "delivery", root / "backup"
                plan = ingest.build_plan([ingest.describe_file(path) for path in sources], {
                    **recipe, "destination": str(destination), "backupDestination": str(backup)})
                self.assertEqual(plan["total"], 2)
                self.assertEqual(len({item["destination"] for item in plan["items"]}), 2)
                for item in plan["items"]:
                    result = ingest.copy_item(item, verify="hash")
                    self.assertTrue(result["ok"], result)
                    source = Path(item["source"])
                    for output in (Path(item["destination"]), Path(item["backup"])):
                        self.assertEqual(output.read_bytes(), originals[source])
                        self.assertEqual(output.with_suffix(".xmp").read_bytes(),
                                         originals[source.with_suffix(".xmp")])
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)
                # Successful APFS copies alone do not establish portability:
                # other destinations enforce a 255-byte component limit.
                for item in plan["items"]:
                    output = Path(item["destination"])
                    for component in output.relative_to(destination).parts:
                        self.assertLessEqual(len(component.encode("utf-8")), 255)
                    self.assertLessEqual(len((output.name + ".xmp").encode("utf-8")), 255)


class SidecarMetadataBoundaryTests(unittest.TestCase):
    def test_repeated_capture_time_correction_replaces_both_existing_properties(self):
        before = "2026-01-02T03:04:05-05:00"
        after = "2026-01-02T04:04:05-05:00"
        existing = xmp_sidecar.build_sidecar({"captureTimeOverride": before})
        updated = xmp_sidecar.merge_sidecar(existing, {"captureTimeOverride": after})
        self.assertEqual(xmp_sidecar.parse(updated)["captureTime"], after)
        document = minidom.parseString(updated)
        for prefix, local in (("exif", "DateTimeOriginal"), ("photoshop", "DateCreated")):
            values = [node.getAttributeNS(xmp_sidecar.NAMESPACES[prefix], local)
                      for node in document.getElementsByTagNameNS(xmp_sidecar.RDF_NS, "Description")
                      if node.hasAttributeNS(xmp_sidecar.NAMESPACES[prefix], local)]
            self.assertEqual(values, [after])

    def test_reset_capture_time_removes_stale_override_but_absent_key_preserves_it(self):
        original = xmp_sidecar.build_sidecar({"captureTimeOverride": "2026-01-02T03:04:05Z"})
        preserved = xmp_sidecar.merge_sidecar(original, {"rating": 4})
        self.assertEqual(xmp_sidecar.parse(preserved)["captureTime"], "2026-01-02T03:04:05+00:00")
        reset = xmp_sidecar.merge_sidecar(original, {"captureTimeOverride": None})
        self.assertIsNone(xmp_sidecar.parse(reset)["captureTime"])

    def test_pasted_control_characters_cannot_create_an_unreadable_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "frame.RAF"
            source.write_bytes(b"unchanged original")
            caption = "Page one\fPage two\x00Done\ufffe"
            errors = []
            self.assertTrue(xmp_sidecar.write_sidecar(source, {"iptc": {"caption": caption}}, errors), errors)
            parsed = xmp_sidecar.read_sidecar(source)
            self.assertIsNotNone(parsed, "A successful sidecar write must be readable")
            self.assertEqual(parsed["caption"], "Page one\ufffdPage two\ufffdDone\ufffd")
            self.assertTrue(xmp_sidecar.write_sidecar(source, {"rating": 4}, errors), errors)
            self.assertEqual(xmp_sidecar.read_sidecar(source)["rating"], 4)
            self.assertEqual(source.read_bytes(), b"unchanged original")

    def test_explicit_null_preserves_unowned_foreign_camera_dates(self):
        original = self.foreign_capture_document()
        updated = xmp_sidecar.merge_sidecar(original, {"rating": 4, "captureTimeOverride": None})
        self.assertEqual(xmp_sidecar.parse(updated)["captureTime"], "2025-01-02T03:04:05Z")
        self.assertIn("2025-01-02T03:05:06Z", updated)
        self.assertNotIn("lighttable:captureTimeOriginal", updated)

    @staticmethod
    def foreign_capture_document():
        # Exercise an attribute and an element whose foreign namespace prefix
        # is declared on an ancestor rather than the property itself.
        return '''<x:xmpmeta xmlns:x="adobe:ns:meta/"
          xmlns:e="http://ns.adobe.com/exif/1.0/"
          xmlns:p="http://ns.adobe.com/photoshop/1.0/"
          xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
          <rdf:RDF><rdf:Description rdf:about="" e:DateTimeOriginal="2025-01-02T03:04:05Z">
            <p:DateCreated>2025-01-02T03:05:06Z</p:DateCreated>
          </rdf:Description></rdf:RDF></x:xmpmeta>'''

    def test_repeated_correction_then_reset_restores_both_foreign_dates(self):
        updated = self.foreign_capture_document()
        for hour in (4, 5):
            wanted = f"2026-01-02T0{hour}:04:05+00:00"
            updated = xmp_sidecar.merge_sidecar(updated, {"captureTimeOverride": wanted})
            self.assertEqual(xmp_sidecar.parse(updated)["captureTime"], wanted)
        reset = xmp_sidecar.merge_sidecar(updated, {"captureTimeOverride": None})
        self.assertEqual(xmp_sidecar.parse(reset)["captureTime"], "2025-01-02T03:04:05Z")
        document = minidom.parseString(reset)
        created = document.getElementsByTagNameNS(xmp_sidecar.NAMESPACES["photoshop"], "DateCreated")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].firstChild.data, "2025-01-02T03:05:06Z")
        self.assertNotIn("lighttable:captureTimeOriginal", reset)


class BackupInterruptionTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Requires process-kill semantics")
    def test_process_kill_around_publication_never_exposes_a_partial_backup(self):
        import catalog
        import catalog_scan
        from PIL import Image

        child = """
import os, signal, sys
from pathlib import Path
import catalog, durable_io
cat = catalog.Catalog(Path(sys.argv[1]))
publish = durable_io.publish_file_no_replace
def interrupt(staged, destination):
    if sys.argv[3] == 'before':
        os.kill(os.getpid(), signal.SIGKILL)
    publish(staged, destination)
    os.kill(os.getpid(), signal.SIGKILL)
durable_io.publish_file_no_replace = interrupt
cat.backup(Path(sys.argv[2]))
"""
        for phase in ("before", "after"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                originals = root / "photos"
                originals.mkdir()
                photo = originals / "frame.png"
                Image.new("RGB", (12, 8), (23, 61, 117)).save(photo)
                original_bytes = photo.read_bytes()
                database = root / "catalog" / "library.sqlite3"
                current = catalog.Catalog(database)
                try:
                    source = current.add_source(originals)
                    catalog_scan.scan_source(current, source, read_metadata_for_new=False)
                    image_id = current.image_id_for(source, "frame.png")
                    current.save_state(image_id, {"rating": 2})
                    backups = root / "backups"
                    baseline = current.backup(backups)
                    before_bytes = baseline.read_bytes()
                    current.save_state(image_id, {"rating": 4, "grade": {"exposure": .5}})
                    current.add_history(image_id, "Acknowledged edit", {"rating": 4})
                finally:
                    current.close()
                result = subprocess.run(
                    [sys.executable, "-c", child, str(database), str(backups), phase],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                archives = sorted(backups.glob("LightTable-catalog-*.zip"))
                self.assertEqual(len(archives), 1 if phase == "before" else 2)
                self.assertEqual(baseline.read_bytes(), before_bytes)
                for archive in archives:
                    self.assertEqual(catalog.verify_backup(archive)["summary"]["images"], 1)
                if phase == "after":
                    published = next(path for path in archives if path != baseline)
                    restored_path = root / "restored" / "library.sqlite3"
                    restored_path.parent.mkdir()
                    catalog.restore_backup(restored_path, published)
                    restored = catalog.Catalog(restored_path)
                    try:
                        self.assertEqual(restored.state_for(image_id)["rating"], 4)
                        self.assertEqual(restored.state_for(image_id)["grade"], {"exposure": .5})
                        self.assertEqual(len(restored.history_for(image_id)), 1)
                    finally:
                        restored.close()
                self.assertEqual(photo.read_bytes(), original_bytes)


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        import server
        from test_server_catalog import CatalogServerTestCase

        self.case = CatalogServerTestCase()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.name = self.case.qualified("a.jpg")
        self.image_id = server.catalog_image_id(self.name)
        self.case.catalog.save_state(self.image_id, {
            "status": "approved", "rating": 1, "params": {"profile_enabled": True}})
        self.options = {
            "names": [self.name], "format": "jpeg", "metadata": "all", "sidecar": False,
            "destination": str(Path(self.case._dir.name) / "delivery"),
            "filenameTemplate": "{filename}_{rating}"}
        self.resources.enter_context(mock.patch.object(server, "_EXIF_CACHE", {}))
        self.resources.enter_context(mock.patch.object(server, "RUST_WORKER_BIN", "fixture"))
        self.resources.enter_context(mock.patch.object(server, "guard_photo"))

        def render(name, destination, job):
            # Keep production finishing, encoding, metadata and publication;
            # use the tiny fixture's pixels in place of the film simulation.
            server.finish_export(server.src_path(name), destination, job)
            return {"backend": "fixture", "total_ms": 0}

        self.resources.enter_context(mock.patch.object(server, "export_with_resident_engine", side_effect=render))

    def test_rating_metadata_matches_the_queued_filename_after_later_edit(self):
        import server

        items, _ = server.prepare_export(self.options)
        self.case.catalog.save_state(self.image_id, {"rating": 5})
        result = server._export_one(self.name, items[0][1])
        self.assertEqual(result.get("completed"), 1, result)
        output = Path(result["path"])
        self.assertEqual(output.name, "a_1.jpg")
        self.assertEqual(xmp_sidecar.read_embedded(output)["rating"], 1)

    def test_capture_time_snapshot_controls_filename_metadata_and_filesystem_date(self):
        import exiv2
        import server

        source = self.case.root / "a.jpg"
        image = exiv2.ImageFactory.open(str(source))
        image.readMetadata()
        image.exifData()["Exif.Photo.DateTimeOriginal"] = "2025:01:02 03:04:05"
        image.exifData()["Exif.Photo.OffsetTimeOriginal"] = "+00:00"
        image.writeMetadata()
        original_bytes = source.read_bytes()
        for before in ("2026-01-02T03:04:05+00:00", None):
            with self.subTest(queued_override=before):
                self.case.catalog.set_capture_override(self.image_id, before)
                options = dict(self.options, filenameTemplate="{filename}_{date}", preserveCaptureTime=True)
                items, _ = server.prepare_export(options)
                self.case.catalog.set_capture_override(self.image_id, "2026-08-09T03:04:05+00:00")
                result = server._export_one(self.name, items[0][1])
                self.assertEqual(result.get("completed"), 1, result)
                output = Path(result["path"])
                wanted = before or "2025-01-02T03:04:05+00:00"
                date = datetime.fromisoformat(wanted)
                self.assertEqual(output.name, "a_" + date.strftime("%Y_%m_%d") + ".jpg")
                delivered = exiv2.ImageFactory.open(str(output))
                delivered.readMetadata()
                actual = {datum.key(): datum.toString() for datum in delivered.exifData()}
                self.assertEqual(actual["Exif.Photo.DateTimeOriginal"], date.strftime("%Y:%m:%d %H:%M:%S"))
                self.assertEqual(actual["Exif.Photo.OffsetTimeOriginal"], "+00:00")
                self.assertAlmostEqual(output.stat().st_mtime, date.timestamp(), delta=1)
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_source_replaced_after_queue_is_rejected_before_rendering(self):
        import server
        from PIL import Image

        items, _ = server.prepare_export(self.options)
        source = self.case.root / "a.jpg"
        Image.new("RGB", (32, 24), (220, 10, 180)).save(source, quality=98)
        result = server._export_one(self.name, items[0][1])
        self.assertIn("Original changed", result.get("error", ""), result)
        self.assertNotIn("completed", result)
        server.export_with_resident_engine.assert_not_called()
        self.assertFalse(list(Path(self.options["destination"]).glob("*")))

    def test_source_changed_during_render_preserves_previous_output_and_cleans_staging(self):
        import server
        from PIL import Image

        for legacy_job in (False, True):
            with self.subTest(legacy_job=legacy_job):
                items, _ = server.prepare_export(dict(self.options, collision="overwrite", sidecar=True))
                job = items[0][1]
                if legacy_job:
                    job.pop("sourceSignature", None)
                destination = Path(self.options["destination"])
                destination.mkdir(exist_ok=True)
                previous = destination / "a_1.jpg"
                previous.write_bytes(b"previous complete delivery")
                recipe = previous.with_suffix(".jpg.lighttable.json")
                recipe.write_bytes(b"previous complete recipe")
                render = server.export_with_resident_engine.side_effect

                def replace_after_render(name, staged, render_job):
                    metrics = render(name, staged, render_job)
                    Image.new("RGB", (32, 24), (20, 170, 90)).save(self.case.root / "a.jpg")
                    return metrics

                with mock.patch.object(server, "export_with_resident_engine", side_effect=replace_after_render):
                    result = server._export_one(self.name, job)
                self.assertIn("Original changed", result.get("error", ""), result)
                self.assertNotIn("completed", result)
                self.assertEqual(previous.read_bytes(), b"previous complete delivery")
                self.assertEqual(recipe.read_bytes(), b"previous complete recipe")
                self.assertEqual(set(destination.iterdir()), {previous, recipe})
                self.assertNotIn(previous, server.EXPORT_RESERVED_PATHS)

    def test_exif_cache_refreshes_after_same_size_change_with_restored_mtime(self):
        import exiv2
        import server

        source = self.case.root / "a.jpg"
        def set_date(value):
            image = exiv2.ImageFactory.open(str(source))
            image.readMetadata()
            image.exifData()["Exif.Photo.DateTimeOriginal"] = value
            image.writeMetadata()

        set_date("2025:01:02 03:04:05")
        with mock.patch.object(server.platform_image, "metadata", wraps=server.platform_image.metadata) as read:
            self.assertEqual(server.exif_for(self.name)["DateTimeOriginal"], "2025:01:02 03:04:05")
            self.assertEqual(server.exif_for(self.name)["DateTimeOriginal"], "2025:01:02 03:04:05")
            self.assertEqual(read.call_count, 1)
            original_stat = source.stat()
            set_date("2026:08:09 10:11:12")
            self.assertEqual(source.stat().st_size, original_stat.st_size)
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            self.assertEqual(server.exif_for(self.name)["DateTimeOriginal"], "2026:08:09 10:11:12")
            self.assertEqual(read.call_count, 2)

    def test_original_missing_at_queue_is_not_adopted_when_it_later_arrives(self):
        import server
        from PIL import Image

        source = self.case.root / "a.jpg"
        source.unlink()
        items, _ = server.prepare_export(self.options)
        Image.new("RGB", (32, 24), (220, 10, 180)).save(source)
        result = server._export_one(self.name, items[0][1])
        self.assertIn("unavailable when export was queued", result.get("error", ""), result)
        self.assertNotIn("completed", result)
        server.export_with_resident_engine.assert_not_called()

    def test_metadata_changed_during_extraction_is_not_cached(self):
        import server
        source = self.case.root / "a.jpg"
        read = server.platform_image.metadata

        def changed_during_read(path):
            metadata = read(path)
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            return metadata

        with mock.patch.object(server.platform_image, "metadata", side_effect=changed_during_read):
            with self.assertRaisesRegex(OSError, "Original changed while reading"):
                server.exif_for(self.name)
        self.assertNotIn(self.name, server._EXIF_CACHE)


class ExportCancellationBoundaryTests(unittest.TestCase):
    def test_cancel_before_first_dispatch_finishes_without_waiting_for_a_worker(self):
        import server
        from jobs import JobRegistry

        with tempfile.TemporaryDirectory() as folder:
            registry = JobRegistry()
            batch = server.ExportBatch([("photo", {})], Path(folder))
            record = registry.create("export", total=1, state="running", cancel=batch.cancel)
            batch.status["jobId"] = record["id"]
            with mock.patch.object(server, "JOBS", registry), \
                 mock.patch.object(server, "EXPORT", dict(batch.status)), \
                 mock.patch.object(server.EXPORT_POOL, "submit") as submit:
                registry.cancel(record["id"])
                batch.dispatch()
                state = registry.get(record["id"])
                self.assertEqual(state["state"], "cancelled", state)
                self.assertFalse(server.EXPORT["running"])
                self.assertEqual(state["result"]["cancelledCount"], 1)
                submit.assert_not_called()


class PortableMetadataBoundaryTests(unittest.TestCase):
    def test_unrecognized_iso_alias_cannot_discard_other_real_camera_metadata(self):
        import exiv2
        import platform_image
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "camera.jpg"
            Image.new("RGB", (12, 8)).save(source)
            image = exiv2.ImageFactory.open(str(source))
            image.readMetadata()
            fields = image.exifData()
            fields["Exif.Image.Make"] = "Example Camera"
            fields["Exif.Photo.DateTimeOriginal"] = "2025:01:02 03:04:05"
            fields["Exif.Photo.ISOSpeedRatings"] = "100"
            image.writeMetadata()
            actual = platform_image.metadata(source, force_portable=True)
            self.assertEqual(actual.get("Make"), "Example Camera", actual)
            self.assertEqual(actual.get("DateTimeOriginal"), "2025:01:02 03:04:05", actual)
            self.assertEqual(actual.get("ISO"), "100", actual)
