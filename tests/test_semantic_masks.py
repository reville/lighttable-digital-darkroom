import base64
import io
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
