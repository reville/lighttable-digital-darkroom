"""Metadata failure reports and non-destructive cross-editor sidecar updates."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile
from PIL import Image

import catalog
import catalog_scan
import color_pipeline
import durable_io
import platform_image
import render_cli
import server
import xmp_sidecar


FOREIGN_XMP = '''<?xpacket begin=""?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmlns:foreign="urn:other-editor" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmp:Rating="3" xmp:CustomField="keep-me" crs:CameraProfile="Custom profile"
   crs:Exposure2012="1.5" foreign:Qualified="foreign:Camera">
   <foreign:Masks><rdf:Seq><rdf:li foreign:opacity="0.8">mask one</rdf:li></rdf:Seq></foreign:Masks>
   <crs:ToneCurve><rdf:Seq><rdf:li>128,140</rdf:li></rdf:Seq></crs:ToneCurve>
   <!-- preserve this editor's annotation -->
  </rdf:Description>
  <rdf:Description rdf:about="urn:another-resource" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="5"/>
 </rdf:RDF>
</x:xmpmeta><?xpacket end="w"?>'''


class SidecarPreservationTests(unittest.TestCase):
    def test_reject_round_trips_without_consuming_a_color_label(self):
        document = xmp_sidecar.build_sidecar({"status": "skipped", "rating": 4,
                                              "label": "green"})
        parsed = xmp_sidecar.parse(document)
        self.assertTrue(parsed["rejected"])
        self.assertEqual(parsed["label"], "green")

    def test_foreign_namespaces_structures_and_later_edits_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "a.CR2"
            original.write_bytes(b"original")
            sidecar = Path(str(original) + ".xmp")
            sidecar.write_text(FOREIGN_XMP)
            self.assertTrue(xmp_sidecar.write_sidecar(original, {
                "rating": 4, "grade": {"exposure": .25}, "keywords": ["New"]}))
            first = sidecar.read_text()
            self.assertIn('xmlns:foreign="urn:other-editor"', first)
            self.assertIn('foreign:Qualified="foreign:Camera"', first)
            self.assertIn('xmp:CustomField="keep-me"', first)
            self.assertIn('crs:CameraProfile="Custom profile"', first)
            self.assertIn('foreign:opacity="0.8"', first)
            self.assertIn('128,140', first)
            self.assertIn("preserve this editor's annotation", first)
            self.assertEqual(xmp_sidecar.parse(first)["rating"], 4)
            # A foreign application edits again after our initial backup.
            sidecar.write_text(first.replace("mask one", "mask two"))
            self.assertTrue(xmp_sidecar.write_sidecar(original, {
                "rating": 0, "grade": {}, "keywords": [], "crop": None}))
            second = sidecar.read_text()
            self.assertIn("mask two", second)
            self.assertNotIn('crs:Exposure2012=', second)
            self.assertNotIn('dc:subject', second)
            descriptions = ET.fromstring(second).findall(
                ".//{" + xmp_sidecar.RDF_NS + "}Description")
            own = [node for node in descriptions if not node.get(
                "{" + xmp_sidecar.RDF_NS + "}about")]
            self.assertFalse(any("{" + xmp_sidecar.NAMESPACES["xmp"] + "}Rating"
                                 in node.attrib for node in own))
            self.assertEqual(durable_io.backup_path(sidecar).read_text(), FOREIGN_XMP)
            self.assertFalse(original.with_suffix(".xmp").exists())

    def test_malformed_or_non_rdf_sidecars_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.raw"
            sidecar = source.with_suffix(".xmp")
            for content in ("external unfinished work", "<x/>",
                            '<!DOCTYPE x [<!ENTITY a "text">]><x/>'):
                sidecar.write_text(content)
                errors = []
                self.assertFalse(xmp_sidecar.write_sidecar(source, {"rating": 4}, errors))
                self.assertEqual(sidecar.read_text(), content)
                self.assertTrue(errors)

    def test_element_form_owned_values_are_replaced_without_duplicates(self):
        document = FOREIGN_XMP.replace('xmp:Rating="3"', '').replace(
            '<foreign:Masks>', '<xmp:Rating>3</xmp:Rating><foreign:Masks>')
        updated = xmp_sidecar.merge_sidecar(document, {"rating": 2})
        parsed = ET.fromstring(updated)
        own_holders = [node for node in parsed.iter() if node.tag ==
                       "{" + xmp_sidecar.RDF_NS + "}Description" and not node.get(
                           "{" + xmp_sidecar.RDF_NS + "}about")]
        key = "{" + xmp_sidecar.NAMESPACES["xmp"] + "}Rating"
        self.assertEqual(sum(key in node.attrib for node in own_holders), 1)
        self.assertFalse(any(node.find(key) is not None for node in own_holders))

    def test_repeated_sync_does_not_accumulate_empty_descriptions(self):
        document = FOREIGN_XMP
        for rating in range(25):
            document = xmp_sidecar.merge_sidecar(document, {"rating": rating % 6})
        self.assertLessEqual(len(ET.fromstring(document).findall(
            ".//{" + xmp_sidecar.RDF_NS + "}Description")), 3)


class SidecarOutboxTests(unittest.TestCase):
    def test_virtual_copy_cannot_queue_an_original_sidecar_write(self):
        with mock.patch.object(server, "catalog_handle") as handle:
            server.queue_sidecar("a.jpg::lighttable-copy::version-2")
        handle.assert_not_called()

    def test_failed_write_survives_catalog_reopen_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "photos"
            root.mkdir()
            Image.new("RGB", (8, 8)).save(root / "a.jpg")
            path = Path(directory) / "catalog.sqlite3"
            cat = catalog.Catalog(path)
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            with mock.patch.object(server, "CATALOG", cat), \
                    mock.patch.object(server, "PRIMARY_SOURCE_ID", source), \
                    mock.patch.object(server, "FOLDER", root), \
                    mock.patch.object(server, "CATALOG_MIRROR", False), \
                    mock.patch.object(server, "_queue_mirror"):
                server.save_image_state("a.jpg", {"rating": 4})
                with mock.patch.object(xmp_sidecar, "write_sidecar", return_value=False):
                    self.assertEqual(server.write_pending_sidecars(), 0)
                self.assertEqual(server.sidecar_sync_status()["failed"], 1)
            cat.close()
            cat = catalog.Catalog(path)
            try:
                with mock.patch.object(server, "CATALOG", cat), \
                        mock.patch.object(server, "PRIMARY_SOURCE_ID", source), \
                        mock.patch.object(server, "FOLDER", root):
                    self.assertEqual(server.sidecar_sync_status()["pending"], 1)
                    self.assertEqual(server.write_pending_sidecars(), 1)
                    self.assertEqual(server.sidecar_sync_status()["pending"], 0)
                self.assertEqual(xmp_sidecar.parse((root / "a.xmp").read_text())["rating"], 4)
            finally:
                cat.close()

    def test_an_edit_during_sync_is_not_removed_from_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (8, 8)).save(root / "a.jpg")
            cat = catalog.Catalog(root / "catalog.sqlite3")
            self.addCleanup(cat.close)
            source = cat.add_source(root)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            with mock.patch.object(server, "CATALOG", cat), \
                    mock.patch.object(server, "PRIMARY_SOURCE_ID", source), \
                    mock.patch.object(server, "FOLDER", root):
                server.queue_sidecar("a.jpg")
                def write(*_args, **_kwargs):
                    server.queue_sidecar("a.jpg")
                    return True
                with mock.patch.object(xmp_sidecar, "write_sidecar", side_effect=write):
                    self.assertEqual(server.write_pending_sidecars(), 1)
                self.assertEqual(server.sidecar_sync_status()["pending"], 1)


class ExportWarningTests(unittest.TestCase):
    def test_metadata_writer_failure_does_not_damage_encoded_pixels(self):
        try:
            import exiv2
        except ImportError:
            self.skipTest("exiv2 binding unavailable")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jpg"
            Image.new("RGB", (12, 8), "red").save(output)
            original = output.read_bytes()
            image = mock.Mock()
            image.exifData.return_value = {}
            image.xmpData.return_value = {}
            image.pixelWidth.return_value = 12
            image.pixelHeight.return_value = 8
            def open_image(path):
                def fail():
                    Path(path).write_bytes(b"half-written metadata")
                    raise OSError("disk is full")
                image.writeMetadata.side_effect = fail
                return image
            warnings = []
            factory = mock.Mock()
            factory.open.side_effect = open_image
            with mock.patch.object(exiv2, "ImageFactory", factory), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(platform_image.write_metadata(
                    output, policy="copyright", warnings=warnings))
            self.assertEqual(output.read_bytes(), original)
            self.assertIn("disk is full", warnings[0])
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_failed_metadata_keeps_resident_precision_pixels_and_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "render.tif"
            destination = Path(directory) / "delivery.jpg"
            tifffile.imwrite(source, np.full((8, 12, 3), .5, np.float32), photometric="rgb")
            job = {"format": "jpeg", "metadata": "all", "sourceName": "a.raw"}
            with mock.patch.object(server, "src_path", return_value=source), \
                    mock.patch.object(platform_image, "write_metadata", return_value=False):
                self.assertEqual(server.finish_export(source, destination, job), (12, 8))
            with Image.open(destination) as image:
                self.assertEqual(image.size, (12, 8))
            self.assertEqual(job["warnings"], ["Requested metadata could not be saved."])

    def test_cli_returns_machine_readable_metadata_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, job_path = root / "source.tif", root / "out.jpg", root / "job.json"
            tifffile.imwrite(source, np.full((8, 12, 3), .5, np.float32), photometric="rgb")
            job_path.write_text(json.dumps({"params": {"profile_enabled": False},
                                           "metadata": "all"}))
            capture = io.StringIO()
            with mock.patch.object(sys, "argv", ["render_cli.py", str(source), str(output), str(job_path)]), \
                    mock.patch.object(platform_image, "write_metadata", return_value=False), \
                    contextlib.redirect_stdout(capture):
                render_cli.main()
            result = json.loads(capture.getvalue().splitlines()[-1])
            self.assertTrue(result["ok"])
            self.assertTrue(result["warnings"])
            with Image.open(output) as image:
                self.assertEqual(image.size, (12, 8))

    def test_missing_required_profile_refuses_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jpg"
            with mock.patch.object(color_pipeline, "icc_bytes", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "color profile is missing"):
                    color_pipeline.save_export_image(np.zeros((2, 3, 3)), output, fmt="jpeg")
                with mock.patch.object(server, "_resident_render_full") as render:
                    with self.assertRaisesRegex(RuntimeError, "color profile is missing"):
                        server.export_with_resident_engine("a.jpg", output, {"params": {}})
                    render.assert_not_called()
            self.assertFalse(output.exists())

    def test_heif_staging_reports_metadata_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.heic"
            warnings = []
            def encode(command, **_kwargs):
                output.write_bytes(b"encoded heif")
                return subprocess.CompletedProcess(command, 0, "", "")
            with mock.patch.object(color_pipeline.sys, "platform", "darwin"), \
                    mock.patch.object(color_pipeline.os, "access", return_value=True), \
                    mock.patch.object(platform_image, "write_metadata", return_value=False), \
                    mock.patch.object(color_pipeline.subprocess, "run", side_effect=encode):
                color_pipeline.save_export_image(np.zeros((2, 3, 3)), output, fmt="heif",
                                                 metadata_policy="all", warnings=warnings)
            self.assertTrue(output.is_file())
            self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
