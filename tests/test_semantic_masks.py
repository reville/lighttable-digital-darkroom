import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import semantic_masks


class SemanticMaskTests(unittest.TestCase):
    def test_sky_is_top_connected(self):
        image = np.zeros((160, 220, 3), dtype=np.uint8)
        image[:75] = (92, 154, 224)
        image[75:] = (52, 96, 41)
        mask = semantic_masks.sky_mask(image)
        self.assertGreater(float(mask[20].mean()), 220)
        self.assertLess(float(mask[-20].mean()), 25)

    def test_point_object_selects_connected_colour_region(self):
        image = np.full((160, 220, 3), 35, dtype=np.uint8)
        image[40:125, 70:155] = (210, 65, 45)
        mask = semantic_masks.object_mask(image, (0.5, 0.5))
        self.assertGreater(int(mask[80, 110]), 240)
        self.assertLess(int(mask[10, 10]), 10)

    def test_sky_preserves_thin_branches_at_1024_pixels(self):
        image = np.full((768, 1024, 3), (92, 154, 224), dtype=np.uint8)
        image[650:] = (52, 96, 41)
        image[140:650, 499:502] = (35, 27, 22)
        mask = semantic_masks.sky_mask(image)
        self.assertEqual(mask.shape, (768, 1024))
        self.assertLess(int(mask[300, 500]), 10)
        self.assertGreater(int(mask[300, 490]), 240)

    def test_sky_does_not_add_blue_objects_without_blue_at_top(self):
        image = np.full((160, 220, 3), (173, 141, 112), dtype=np.uint8)
        image[70:130, 80:150] = (92, 154, 224)
        mask = semantic_masks.sky_mask(image)
        self.assertLess(int(mask[100, 110]), 10)

    def test_object_preserves_unselected_opening(self):
        image = np.full((160, 220, 3), 35, dtype=np.uint8)
        image[30:135, 55:165] = (210, 65, 45)
        image[65:100, 90:130] = 35
        mask = semantic_masks.object_mask(image, (0.30, 0.5))
        self.assertGreater(int(mask[80, 65]), 240)
        self.assertLess(int(mask[80, 110]), 10)

    def test_object_follows_shading_to_boundary(self):
        image = np.full((160, 220, 3), 35, dtype=np.uint8)
        for x in range(50, 175):
            intensity = (x - 50) / 124.0
            image[35:135, x] = (110 + round(120 * intensity),
                               30 + round(30 * intensity),
                               20 + round(20 * intensity))
        mask = semantic_masks.object_mask(image, (0.65, 0.5))
        self.assertGreater(int(mask[80, 65]), 220)
        self.assertGreater(int(mask[80, 165]), 240)
        self.assertLess(int(mask[80, 30]), 10)

    def test_point_on_thin_detail_is_not_replaced_by_patch_background(self):
        image = np.full((160, 220, 3), 35, dtype=np.uint8)
        image[30:135, 109] = (210, 65, 45)
        mask = semantic_masks.object_mask(image, (109 / 219, 0.5))
        self.assertGreater(int(mask[80, 109]), 240)
        self.assertLess(int(mask[80, 108]), 10)

    def test_refinement_respects_colour_edges_and_fractional_coverage(self):
        image = np.zeros((40, 60, 3), dtype=np.uint8)
        image[:, :30] = (230, 30, 30)
        image[:, 30:] = (30, 90, 30)
        coverage = np.zeros((40, 60), dtype=np.float32)
        coverage[:, 30:] = 0.5
        refined = semantic_masks._finish(
            coverage, semantic_masks._working_image(image))
        self.assertLess(int(refined[20, 29]), 3)
        self.assertAlmostEqual(int(refined[20, 30]), 128, delta=1)
        self.assertAlmostEqual(int(refined[20, 45]), 128, delta=1)

    def test_vision_preserves_soft_alpha_and_thin_detail_without_reading_raw(self):
        class Provider:
            def foreground_mask(self, _image, output):
                values = np.zeros((768, 1024), dtype=np.uint8)
                values[200:600, 300:650] = 128
                values[80:200, 499:502] = 96
                Image.fromarray(values, "L").save(output)
                return True

        image = np.full((768, 1024, 3), 35, dtype=np.uint8)
        image[200:600, 300:650] = (210, 65, 45)
        image[80:200, 499:502] = (210, 65, 45)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.raf"
            source.touch()  # Only Vision decodes this path; use supplied RGB for guidance.
            mask, provider = semantic_masks.generate(
                image, "subject", source_path=source,
                vision_helper=Path("helper"), vision_provider=Provider())
        self.assertEqual(provider, "vision")
        self.assertEqual(mask.shape, (768, 1024))
        self.assertEqual(int(mask[400, 450]), 128)
        self.assertGreaterEqual(int(mask[120, 500]), 90)
        self.assertLess(int(mask[120, 490]), 10)

    def test_portable_masks_bound_large_source_resolution(self):
        image = np.full((1200, 1600, 3), (92, 154, 224), dtype=np.uint8)
        mask = semantic_masks.sky_mask(image)
        self.assertEqual(mask.shape, (768, 1024))

    def test_bitmap_encoding_has_exact_pixel_count(self):
        mask = np.arange(30, dtype=np.uint8).reshape(5, 6)
        encoded = semantic_masks.encode_bitmap(mask)
        self.assertEqual((encoded["width"], encoded["height"]), (6, 5))
        self.assertEqual(len(base64.b64decode(encoded["data"])), 30)

    def test_depth_estimate_is_continuous_and_nearer_toward_foreground(self):
        image = np.zeros((180, 240, 3), dtype=np.uint8)
        image[:80] = (100, 160, 225)
        image[80:] = (70, 95, 45)
        image[95:165, 85:155] = (210, 60, 40)
        depth = semantic_masks.estimated_depth_map(image)
        self.assertEqual(depth.dtype, np.uint8)
        self.assertGreater(len(np.unique(depth)), 32)
        self.assertGreater(float(depth[-30:].mean()), float(depth[:30].mean()))

    def test_embedded_depth_provider_wins_without_binarizing(self):
        class Provider:
            available = True

            def depth_map(self, _image, output):
                Image.fromarray(np.arange(100, dtype=np.uint8).reshape(10, 10),
                                "L").save(output)
                return True

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "portrait.heic"
            source.touch()
            depth, provider = semantic_masks.generate(
                np.zeros((10, 10, 3), dtype=np.uint8), "depth",
                source_path=source, vision_helper=Path("helper"),
                vision_provider=Provider())
        self.assertEqual(provider, "embedded-depth")
        self.assertGreater(len(np.unique(depth)), 32)

    def test_person_parts_share_one_cached_helper_request_and_use_png(self):
        class Provider:
            available = True

            def __init__(self):
                self.calls = 0

            def person_parts(self, image, output):
                self.calls += 1
                for index, name in enumerate(semantic_masks.PERSON_PARTS):
                    mask = np.zeros((1200, 800), dtype=np.uint8)
                    mask[100 + index:700, 200:600] = 255
                    Image.fromarray(mask, "L").save(output / f"{name}.png")
                return {"faces": 2, "provider": "vision",
                        "parts": {name: True for name in semantic_masks.PERSON_PARTS}}

        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portrait.jpg"
            Image.new("RGB", (800, 1200), "gray").save(source)
            cache = root / "parts"
            teeth, _ = semantic_masks.generate(
                np.zeros((1200, 800, 3), np.uint8), "teeth",
                source_path=source, vision_helper=Path("helper"),
                vision_provider=provider, parts_cache=cache)
            eyes, _ = semantic_masks.generate(
                np.zeros((1200, 800, 3), np.uint8), "eyes",
                source_path=source, vision_helper=Path("helper"),
                vision_provider=provider, parts_cache=cache)
            encoded = semantic_masks.encode_bitmap(
                teeth, png=True, max_edge=semantic_masks.MAX_PART_EDGE)
        self.assertEqual(provider.calls, 1)
        self.assertGreater(int(eyes.max()), 0)
        self.assertEqual(encoded["encoding"], "png")
        decoded = Image.open(io.BytesIO(base64.b64decode(encoded["data"])))
        self.assertEqual(max(decoded.size), 1024)

    def test_person_parts_regenerate_old_cache_without_reusing_omitted_parts(self):
        class Provider:
            def __init__(self):
                self.calls = 0

            def person_parts(self, _image, output):
                self.calls += 1
                Image.new("L", (20, 20), 96).save(output / "person.png")
                return {"provider": "vision", "faces": 0}

        provider = Provider()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "parts.json").write_text(json.dumps({"faces": 1}))
            Image.new("L", (20, 20), 255).save(cache / "person.png")
            Image.new("L", (20, 20), 255).save(cache / "hair.png")
            masks, summary = semantic_masks._person_parts(
                Path("source.jpg"), Path("helper"), provider, cache)
            reused, _ = semantic_masks._person_parts(
                Path("source.jpg"), Path("helper"), provider, cache)
            self.assertFalse((cache / "hair.png").exists())
        self.assertEqual(provider.calls, 1)
        self.assertEqual(int(masks["person"][0, 0]), 96)
        self.assertEqual(int(reused["person"][0, 0]), 96)
        self.assertNotIn("hair", masks)
        self.assertEqual(summary["maskVersion"], semantic_masks.PARTS_CACHE_VERSION)

    def test_parts_refuse_without_vision_but_person_falls_back(self):
        image = np.zeros((80, 100, 3), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError,
                                   "People masks need the Vision helper"):
            semantic_masks.generate(image, "teeth")
        person, provider = semantic_masks.generate(image, "person")
        self.assertEqual(provider, "local-segmentation")
        self.assertEqual(person.ndim, 2)


if __name__ == "__main__":
    unittest.main()
