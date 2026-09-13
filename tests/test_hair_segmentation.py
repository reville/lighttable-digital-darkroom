# SPDX-License-Identifier: GPL-3.0-only
"""Model contracts and full-image integration; no download required for unit tests."""
import hashlib
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from film_lab_ai import hair_segmentation as hair
import semantic_masks


class HairSegmentationTests(unittest.TestCase):
    def test_input_keeps_entire_portrait_and_uses_embedded_normalization(self):
        image = np.zeros((512, 128, 3), dtype=np.uint8)
        image[:64] = (255, 0, 0)
        image[-64:] = (0, 255, 0)
        tensor = hair._input_tensor(image)
        self.assertEqual(tensor.shape, (1, 256, 256, 3))
        self.assertEqual(tensor.dtype, np.float32)
        np.testing.assert_array_equal(tensor[0, 0, 0], (1, -1, -1))
        np.testing.assert_array_equal(tensor[0, -1, -1], (-1, 1, -1))

    def test_logits_use_stable_softmax_hair_channel_and_soft_coverage(self):
        values = np.zeros((1, 256, 256, 6), dtype=np.float32)
        values[..., 1] = np.log(15)  # hair probability = 15 / (15 + 5)
        actual = hair.hair_probability(values)
        np.testing.assert_allclose(actual, (0.75 - 0.05) / 0.95, rtol=1e-6)
        np.testing.assert_allclose(hair.hair_probability(values + 1000), actual, atol=2e-5)
        self.assertTrue(np.all((actual > 0) & (actual < 1)))

    def test_low_confidence_background_does_not_receive_adjustments(self):
        logits = np.zeros((1, 256, 256, 6), dtype=np.float32)
        logits[0, 20:80, 20:80, 1] = 8
        logits[:, 120:, :, 0] = 8  # background elsewhere
        mask = hair.hair_probability(logits)
        self.assertGreater(float(mask[40, 40]), 0.99)
        self.assertEqual(float(mask[150, 40]), 0)
        self.assertFalse(hair.hair_probability(np.zeros_like(logits)).any())

    def test_invalid_model_outputs_are_rejected(self):
        for values in (np.zeros((256, 256, 6)), np.full((1, 256, 256, 6), np.nan)):
            with self.assertRaises(hair.HairModelUnavailable):
                hair.hair_probability(values)

    def test_model_integrity_checks_size_and_content_before_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / hair.MODEL_FILE
            good = b"pinned test model"
            path.write_bytes(good)
            with mock.patch.object(hair, "MODEL_BYTES", len(good)), \
                    mock.patch.object(hair, "MODEL_SHA256", hashlib.sha256(good).hexdigest()):
                self.assertEqual(hair.model_bytes(path), good)
                path.write_bytes(b"x" * len(good))
                with self.assertRaises(hair.HairModelUnavailable):
                    hair.model_bytes(path)
                path.write_bytes(b"short")
                with self.assertRaises(hair.HairModelUnavailable):
                    hair.model_bytes(path)

    def test_user_denoiser_does_not_hide_bundled_hair_model(self):
        with mock.patch.object(hair, "APP", Path("/bundle/Resources/LightTable")), \
                mock.patch.object(hair.platform_paths, "model_directory", return_value=Path("/user/Models")):
            candidates = hair.model_candidates()
        self.assertEqual(candidates[0], Path("/user/Models") / hair.MODEL_FILE)
        self.assertIn(Path("/bundle/Resources/models") / hair.MODEL_FILE, candidates)

    def test_concurrent_requests_cannot_mix_interpreter_inputs(self):
        active = []
        class Engine:
            def set_tensor(self, _index, data):
                self.input = data.copy()
            def invoke(self):
                active.append(threading.get_ident())
                time.sleep(0.005)
            def get_tensor(self, _index):
                values = np.zeros((1, 256, 256, 6), np.float32)
                values[..., 1] = 4 + float(self.input.mean())
                return values
        engine = Engine()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model"
            path.touch()
            with mock.patch.object(hair, "_interpreter", return_value=(engine, 0, 1)):
                def run(value):
                    return hair.hair_mask(np.full((8, 8, 3), value, np.uint8), model_path=path)
                expected = [run(0), run(255)]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    actual = list(pool.map(run, [0, 255] * 3))
        for index, result in enumerate(actual):
            np.testing.assert_array_equal(result, expected[index % 2])

    def test_hair_model_takes_priority_without_vision_and_keeps_long_hair(self):
        probability = np.zeros((256, 256), np.float32)
        probability[20:245, 35:65] = 0.8
        image = np.full((683, 1024, 3), 127, np.uint8)
        with mock.patch.object(hair, "hair_mask", return_value=probability), \
                mock.patch.object(semantic_masks, "_person_parts") as vision:
            mask, provider = semantic_masks.generate(image, "hair")
        vision.assert_not_called()
        self.assertEqual(provider, "local-hair-segmentation")
        self.assertEqual(mask.shape, image.shape[:2])
        self.assertGreater(int(mask[600, 180]), 150)
        self.assertEqual(int(mask[600, 800]), 0)

    def test_empty_learned_result_does_not_invent_a_crown(self):
        with mock.patch.object(hair, "hair_mask", return_value=np.zeros((256, 256), np.float32)), \
                mock.patch.object(semantic_masks, "_person_parts") as vision:
            mask, provider = semantic_masks.generate(np.zeros((40, 40, 3), np.uint8), "hair")
        self.assertFalse(mask.any())
        vision.assert_not_called()
        self.assertEqual(provider, "local-hair-segmentation")

    def test_unavailable_model_preserves_labelled_vision_fallback(self):
        masks = {"hair": np.full((10, 10), 200, np.uint8)}
        with mock.patch.object(hair, "hair_mask", side_effect=hair.HairModelUnavailable), \
                mock.patch.object(semantic_masks, "_person_parts", return_value=(masks, {})):
            mask, provider = semantic_masks.generate(np.zeros((40, 40, 3), np.uint8), "hair",
                source_path=Path("photo.jpg"), vision_helper=Path("helper"))
        self.assertEqual(provider, "vision-estimated")
        self.assertTrue(mask.any())

    @unittest.skipUnless(os.environ.get("LIGHTTABLE_TEST_HAIR_MODEL"), "Optional pinned model not supplied")
    def test_real_model_selects_hair_below_face_without_skin_or_clothes(self):
        path = Path(__file__).parent / "fixtures/photos/portrait.jpg"
        with Image.open(path) as source:
            image = np.asarray(source.convert("RGB"))
        model = Path(os.environ["LIGHTTABLE_TEST_HAIR_MODEL"])
        # Inspect actual saved-output pixels on the long-hair regression photo,
        # including mirrored framing. These samples supplement visual review;
        # they do not establish accuracy on other hairstyles or photographs.
        for mirrored in (False, True):
            rgb = image[:, ::-1].copy() if mirrored else image
            with mock.patch.object(hair, "model_candidates", return_value=[model]):
                mask, provider = semantic_masks.generate(rgb, "hair")
            self.assertEqual(provider, "local-hair-segmentation")
            for x, y in ((0.446, 0.684), (0.695, 0.668)):
                x = 1 - x if mirrored else x
                self.assertGreater(int(mask[round(y * 682), round(x * 1023)]), 128)
            for x, y in ((0.525, 0.41), (0.511, 0.63), (0.39, 0.86), (0.15, 0.5)):
                x = 1 - x if mirrored else x
                self.assertLessEqual(int(mask[round(y * 682), round(x * 1023)]), 5)


if __name__ == "__main__":
    unittest.main()
