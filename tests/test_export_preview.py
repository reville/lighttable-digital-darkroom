"""Delivery preview uses export selection/naming without starting a render."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile
from PIL import Image

import export_workflow
import server


class ExportPreviewTests(unittest.TestCase):
    def candidate(self, name, *, status="approved", rating=2, base=None):
        return (name, {"status": status, "rating": rating,
                      "params": {"profile_enabled": False}, "grade": {},
                      "crop": None, "masks": [], "heals": [], "optics": {}},
                base or Path(name).stem)

    def test_preview_matches_export_plan_and_does_not_create_destination_or_job(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "not-created"
            candidates = [self.candidate("a.jpg"), self.candidate("b.jpg", base="Virtual copy")]
            with mock.patch.object(server, "export_candidates", return_value=candidates), \
                    mock.patch.object(server, "effective_new_photo_defaults", return_value=({}, {})), \
                    mock.patch.object(server, "exif_for", return_value={"DateTimeOriginal": "2026:09:04 12:00:00"}), \
                    mock.patch.object(server, "export_source_dimensions", return_value=(6000, 4000)), \
                    mock.patch.object(server.EXPORT_POOL, "submit") as submit, \
                    mock.patch.object(server.JOBS, "create") as create:
                opts = {"names": ["b.jpg", "a.jpg"], "destination": str(destination),
                        "filenameTemplate": "{sequence}_{filename}_{stock}_{date}", "longEdge": 3000}
                result = server.preview_export(opts)
                items, planned_destination = server.prepare_export(opts)
                self.assertEqual(result["total"], len(items))
                self.assertEqual(result["destination"], str(planned_destination))
                self.assertEqual(result["sample"]["name"], items[0][0])
                self.assertEqual(result["sample"]["filename"], "1_Virtual copy_neutral_2026_09_04.jpg")
                self.assertEqual((result["sample"]["width"], result["sample"]["height"]), (3000, 2000))
                self.assertTrue(result["sample"]["dimensionsExact"])
                self.assertFalse(destination.exists())
                submit.assert_not_called()
                create.assert_not_called()

    def test_preview_collision_and_unavailable_dimensions_are_honest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a_neutral.jpg"
            path.touch()
            with mock.patch.object(server, "export_candidates", return_value=[self.candidate("a.jpg")]), \
                    mock.patch.object(server, "effective_new_photo_defaults", return_value=({}, {})), \
                    mock.patch.object(server, "exif_for", return_value={}), \
                    mock.patch.object(server, "export_source_dimensions", side_effect=OSError("missing")):
                opts = {"destination": folder, "collision": "skip"}
                sample = server.preview_export(opts)["sample"]
                self.assertTrue(sample["skipped"])
                self.assertIsNone(sample["width"])
                self.assertFalse(sample["dimensionsExact"])
                sample = server.preview_export(dict(opts, collision="rename"))["sample"]
                self.assertEqual(sample["filename"], "a_neutral-2.jpg")
                self.assertFalse(sample["skipped"])
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_preview_respects_empty_and_approved_selection(self):
        candidates = [self.candidate("a.jpg"), self.candidate("b.jpg", status="skipped")]
        with mock.patch.object(server, "export_candidates", return_value=candidates), \
                mock.patch.object(server, "effective_new_photo_defaults", return_value=({}, {})):
            self.assertIsNone(server.preview_export({"names": []})["sample"])
            self.assertEqual([name for name, _ in server.prepare_export({"which": "approved"})[0]], ["a.jpg"])

    def test_geometry_matches_written_export_after_rotation_crop_and_resize(self):
        crop = {"x": .133, "y": .2, "w": .7, "h": .6}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "render.tif"
            for rotation in (0, 90, 180, 270):
                for edge in (None, 320, 4000):
                    with self.subTest(rotation=rotation, edge=edge):
                        pixels = np.zeros((601, 903, 3), dtype=np.float32)
                        pixels = np.rot90(pixels, server.rot90k(rotation))
                        tifffile.imwrite(source, pixels, photometric="rgb")
                        destination = Path(folder) / "export.jpg"
                        with mock.patch.object(server, "embed_export_metadata"):
                            server.finish_export(source, destination, {"crop": crop, "longEdge": edge})
                        with Image.open(destination) as output:
                            actual = output.size
                        self.assertEqual(export_workflow.output_dimensions(
                            903, 601, rotate=rotation, crop=crop, long_edge=edge), actual)

    def test_raw_header_geometry_never_unpacks_or_demosaics(self):
        raw = mock.MagicMock()
        raw.__enter__.return_value = raw
        raw.sizes = mock.Mock(iwidth=6000, iheight=4000, flip=6, pixel_aspect=1.0)
        with mock.patch("rawpy.RawPy", return_value=raw), \
                mock.patch.object(server, "src_path", return_value=Path("photo.dng")), \
                mock.patch.object(server, "is_raw", return_value=True):
            self.assertEqual(server.export_source_dimensions("photo.dng"), (4000, 6000))
        raw.open_file.assert_called_once_with("photo.dng")
        raw.unpack.assert_not_called()
        raw.postprocess.assert_not_called()


    def test_source_dimensions_include_exif_orientation_without_decoding(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "portrait.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (903, 601)).save(source, exif=exif)
            with mock.patch.object(server, "src_path", return_value=source), \
                    mock.patch.object(server, "is_raw", return_value=False):
                self.assertEqual(server.export_source_dimensions("portrait.jpg"), (601, 903))


if __name__ == "__main__":
    unittest.main()
