import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import export_workflow


class ExportWorkflowTests(unittest.TestCase):
    def test_recipe_is_bounded_and_validated(self):
        recipe = export_workflow.clean_recipe({
            "format": "exe", "quality": 900, "longEdge": 2,
            "outputSpace": "unknown", "collision": "destroy",
        })
        self.assertEqual(recipe["format"], "jpeg")
        self.assertEqual(recipe["quality"], 100)
        self.assertEqual(recipe["longEdge"], 320)
        self.assertEqual(recipe["outputSpace"], "srgb")
        self.assertEqual(recipe["collision"], "rename")

    def test_filename_template_sanitizes_path_characters(self):
        name = export_workflow.render_filename(
            "{filename}_{date}_{sequence}",
            {"filename": "trip/beach", "date": "2026:09:02", "sequence": 7},
            "jpg")
        self.assertEqual(name, "trip_beach_2026_09_02_7.jpg")

    def test_heif_recipe_is_preserved(self):
        self.assertEqual(
            export_workflow.clean_recipe({"format": "heif"})["format"],
            "heif")

    def test_collision_policy_can_rename_or_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.touch()
            self.assertIsNone(export_workflow.collision_path(path, "skip"))
            self.assertEqual(export_workflow.collision_path(path, "rename").name,
                             "photo-2.jpg")

    def test_concurrent_destination_reservations_get_distinct_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            reserved = {path, path.with_name("photo-2.jpg")}

            self.assertEqual(
                export_workflow.collision_path(path, "rename", reserved).name,
                "photo-3.jpg",
            )
            self.assertEqual(
                export_workflow.collision_path(path, "overwrite", reserved).name,
                "photo-3.jpg",
                "an in-flight output must not be overwritten concurrently",
            )

    def test_a_case_different_reservation_still_blocks_the_name(self):
        # macOS and Windows treat these two spellings as one file, and the
        # first export has only staged, so `exists()` cannot see it yet. The
        # second must rename rather than clobber what the first is publishing.
        with tempfile.TemporaryDirectory() as directory:
            reserved = {Path(directory) / "Sunset.jpg"}
            requested = Path(directory) / "sunset.jpg"

            self.assertEqual(
                export_workflow.collision_path(requested, "rename", reserved).name,
                "sunset-2.jpg",
            )
            self.assertEqual(
                export_workflow.collision_path(requested, "overwrite", reserved).name,
                "sunset-2.jpg",
                "an in-flight output must not be overwritten under another case",
            )

    def test_orphan_manifest_reserves_its_matching_export_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            Path(str(path) + ".lighttable.json").touch()

            self.assertEqual(
                export_workflow.collision_path(
                    path, "rename", companions=(".lighttable.json",)),
                path.with_name("photo-2.jpg"),
            )

    def test_relative_destination_stays_under_library(self):
        root = Path("/tmp/library")
        self.assertEqual(export_workflow.resolve_destination(root, "delivery"),
                         Path("/tmp/library/delivery").resolve())

    def test_recipe_defaults_cover_metadata_sidecar_and_watermark(self):
        recipe = export_workflow.clean_recipe({})
        self.assertEqual(recipe["metadata"], "all-except-location")
        self.assertTrue(recipe["sidecar"])
        self.assertEqual(recipe["watermark"],
                         export_workflow.clean_watermark(None))
        self.assertFalse(recipe["watermark"]["enabled"])

    def test_unknown_metadata_policy_falls_back(self):
        self.assertEqual(export_workflow.clean_recipe({"metadata": False})["metadata"], "none")
        self.assertEqual(export_workflow.clean_recipe({"metadata": True})["metadata"], "all")
        self.assertEqual(
            export_workflow.clean_recipe({"metadata": "everything"})["metadata"],
            "all-except-location")
        self.assertEqual(
            export_workflow.clean_recipe({"metadata": "COPYRIGHT"})["metadata"],
            "copyright")
        for falsy in (False, "false", 0):
            self.assertFalse(
                export_workflow.clean_recipe({"sidecar": falsy})["sidecar"])

    def test_watermark_is_clamped_and_validated(self):
        watermark = export_workflow.clean_watermark({
            "enabled": True, "kind": "hologram", "anchor": "middle-left",
            "inset": 9.0, "scale": 0.0, "opacity": -3, "text": "x " * 200,
        })
        self.assertTrue(watermark["enabled"])
        self.assertEqual(watermark["kind"], "text")
        self.assertEqual(watermark["anchor"], "bottom-right")
        self.assertEqual(watermark["inset"], 0.25)
        self.assertEqual(watermark["scale"], 0.02)
        self.assertEqual(watermark["opacity"], 0.0)
        self.assertEqual(len(watermark["text"]), 120)
        loose = export_workflow.clean_watermark(
            {"inset": "wide", "scale": None})
        self.assertEqual(loose["inset"], 0.03)
        self.assertEqual(loose["scale"], 0.12)

    def test_client_builtin_delivers_without_a_sidecar(self):
        recipes = {item["id"]: item for item in export_workflow.all_recipes([])}
        client = recipes["builtin-client"]
        self.assertEqual(client["name"], "Client delivery")
        self.assertEqual(client["format"], "jpeg")
        self.assertEqual(client["quality"], 92)
        self.assertEqual(client["longEdge"], 3000)
        self.assertEqual(client["outputSpace"], "srgb")
        self.assertEqual(client["metadata"], "all-except-location")
        self.assertFalse(client["sidecar"])
        self.assertFalse(client["watermark"]["enabled"])
        self.assertTrue(client["builtin"])

    def test_builtin_recipes_round_trip_through_the_cleaner(self):
        for recipe in export_workflow.all_recipes([]):
            self.assertEqual(export_workflow.clean_recipe(recipe), recipe)

    def test_custom_recipes_keep_the_new_keys(self):
        custom = export_workflow.clean_custom_recipes([{
            "id": "studio", "metadata": "copyright", "sidecar": False,
            "watermark": {"enabled": True, "text": "Studio",
                          "anchor": "center"},
        }])
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["metadata"], "copyright")
        self.assertFalse(custom[0]["sidecar"])
        self.assertEqual(custom[0]["watermark"]["anchor"], "center")
        self.assertEqual(export_workflow.clean_recipe(custom[0]), custom[0])


class WatermarkTests(unittest.TestCase):
    HEIGHT = 400
    WIDTH = 600

    def setUp(self):
        self.image = np.full((self.HEIGHT, self.WIDTH, 3), 0.5, dtype=np.float32)

    def _changed_box(self, result):
        rows, columns = np.nonzero(
            np.any(np.abs(result - self.image) > 1e-4, axis=2))
        self.assertTrue(rows.size, "the watermark drew nothing")
        return (int(rows.min()), int(rows.max()),
                int(columns.min()), int(columns.max()))

    def test_text_watermark_lands_in_each_anchor_region(self):
        expected = {
            "top-left": (True, True), "top-right": (True, False),
            "bottom-left": (False, True), "bottom-right": (False, False),
        }
        for anchor, (top_half, left_half) in expected.items():
            with self.subTest(anchor=anchor):
                result = export_workflow.apply_watermark(self.image, {
                    "enabled": True, "text": "(c) LT", "anchor": anchor,
                })
                self.assertEqual(result.shape, self.image.shape)
                self.assertEqual(result.dtype, np.float32)
                top, bottom, left, right = self._changed_box(result)
                if top_half:
                    self.assertLess(bottom, self.HEIGHT // 2)
                else:
                    self.assertGreaterEqual(top, self.HEIGHT // 2)
                if left_half:
                    self.assertLess(right, self.WIDTH // 2)
                else:
                    self.assertGreaterEqual(left, self.WIDTH // 2)

    def test_centred_watermark_straddles_the_middle(self):
        result = export_workflow.apply_watermark(self.image, {
            "enabled": True, "text": "(c) LT", "anchor": "center",
        })
        top, bottom, left, right = self._changed_box(result)
        self.assertLess(top, self.HEIGHT // 2)
        self.assertGreater(bottom, self.HEIGHT // 2)
        self.assertLess(left, self.WIDTH // 2)
        self.assertGreater(right, self.WIDTH // 2)

    def test_inset_measures_from_the_short_edge(self):
        def corner(inset):
            top, _, left, _ = self._changed_box(
                export_workflow.apply_watermark(self.image, {
                    "enabled": True, "text": "mark", "anchor": "top-left",
                    "inset": inset,
                }))
            return top, left

        near_top, near_left = corner(0.05)
        far_top, far_left = corner(0.15)
        # 0.1 of the 400px short edge, on both axes and both sides of the frame.
        self.assertEqual(far_top - near_top, 40)
        self.assertEqual(far_left - near_left, 40)
        _, low_bottom, _, low_right = self._changed_box(
            export_workflow.apply_watermark(self.image, {
                "enabled": True, "text": "mark", "anchor": "bottom-right",
                "inset": 0.05,
            }))
        _, high_bottom, _, high_right = self._changed_box(
            export_workflow.apply_watermark(self.image, {
                "enabled": True, "text": "mark", "anchor": "bottom-right",
                "inset": 0.15,
            }))
        self.assertEqual(low_bottom - high_bottom, 40)
        self.assertEqual(low_right - high_right, 40)

    def test_scale_drives_the_mark_size(self):
        small = export_workflow.apply_watermark(self.image, {
            "enabled": True, "text": "mark", "scale": 0.05})
        large = export_workflow.apply_watermark(self.image, {
            "enabled": True, "text": "mark", "scale": 0.3})
        small_top, small_bottom, _, _ = self._changed_box(small)
        large_top, large_bottom, _, _ = self._changed_box(large)
        self.assertGreater(large_bottom - large_top, small_bottom - small_top)

    def test_disabled_or_empty_watermark_returns_the_input(self):
        for spec in ({"enabled": False, "text": "skip me"},
                     {"enabled": True, "text": "   "},
                     {"enabled": True, "text": "hi", "opacity": 0},
                     {"enabled": True, "kind": "image",
                      "imagePath": "/no/such.png"},
                     None):
            with self.subTest(spec=spec):
                self.assertIs(export_workflow.apply_watermark(
                    self.image, spec), self.image)

    def test_image_watermark_composites_with_premultiplied_alpha(self):
        with tempfile.TemporaryDirectory() as directory:
            overlay = Path(directory) / "logo.png"
            Image.new("RGBA", (200, 100), (255, 0, 0, 128)).save(overlay)
            result = export_workflow.apply_watermark(self.image, {
                "enabled": True, "kind": "image", "imagePath": str(overlay),
                "anchor": "top-left", "inset": 0.0, "opacity": 1.0,
            })
        top, bottom, left, right = self._changed_box(result)
        self.assertEqual((top, left), (0, 0))
        # 0.12 * max(400, 600) long edge, so a 200x100 logo becomes 72x36.
        self.assertEqual((bottom, right), (35, 71))
        alpha = 128 / 255.0
        self.assertAlmostEqual(
            float(result[0, 0, 0]), 0.5 * (1 - alpha) + alpha, places=4)
        self.assertAlmostEqual(
            float(result[0, 0, 1]), 0.5 * (1 - alpha), places=4)

    def test_watermark_stays_inside_the_frame_and_the_range(self):
        result = export_workflow.apply_watermark(
            np.zeros((60, 60, 3), dtype=np.float32),
            {"enabled": True, "text": "far too wide for this frame",
             "scale": 0.5, "opacity": 1.0})
        self.assertEqual(result.shape, (60, 60, 3))
        self.assertGreaterEqual(float(result.min()), 0.0)
        self.assertLessEqual(float(result.max()), 1.0)


if __name__ == "__main__":
    unittest.main()
