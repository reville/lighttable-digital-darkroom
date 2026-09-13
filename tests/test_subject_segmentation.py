# SPDX-License-Identifier: GPL-3.0-only
"""Cross-platform person/subject mask contracts; no download required."""
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from film_lab_ai import hair_segmentation as hair
from film_lab_ai import subject_segmentation as subject
import semantic_masks


class SubjectSegmentationTests(unittest.TestCase):
    def test_reuses_the_exact_hair_model_asset_and_search_path(self):
        # No second model download, licence file, or packaging entry: this
        # is the same pinned Apache-2.0 asset already fetched for hair.
        self.assertEqual(subject.model_candidates(), hair.model_candidates())

    def test_person_probability_unions_hair_body_face_and_clothes_channels(self):
        values = np.zeros((1, 256, 256, 6), dtype=np.float32)
        # Background channel 0 dominates a patch: must stay excluded.
        values[:, :128, :, 0] = 8
        # Each non-background, non-"other" channel should count as person.
        for channel in (1, 2, 3, 4):
            values[:, 128:, :, channel] = 8
        actual = subject.person_probability(values)
        self.assertEqual(float(actual[10, 10]), 0.0)
        self.assertGreater(float(actual[200, 10]), 0.9)

    def test_other_channel_alone_is_not_person(self):
        values = np.zeros((1, 256, 256, 6), dtype=np.float32)
        values[..., 0] = 4
        values[..., 5] = 8  # "other" (accessories) only
        actual = subject.person_probability(values)
        self.assertFalse(np.any(actual > 0))

    def test_confidently_empty_result_stays_exactly_empty(self):
        values = np.zeros((1, 256, 256, 6), dtype=np.float32)
        values[..., 0] = 8  # background everywhere
        self.assertFalse(subject.person_probability(values).any())

    def test_invalid_model_outputs_are_rejected(self):
        for values in (np.zeros((256, 256, 6)), np.full((1, 256, 256, 6), np.nan)):
            with self.assertRaises(subject.SubjectModelUnavailable):
                subject.person_probability(values)

    def test_model_unavailable_raises_the_module_specific_error(self):
        with tempfile_missing_model() as path:
            with self.assertRaises(subject.SubjectModelUnavailable):
                subject.person_mask(np.zeros((8, 8, 3), np.uint8), model_path=path)

    def test_generate_uses_learned_person_mask_before_the_saliency_heuristic(self):
        probability = np.zeros((256, 256), np.float32)
        probability[64:192, 64:192] = 0.9
        image = np.full((80, 100, 3), 127, np.uint8)
        with mock.patch.object(subject, "person_mask", return_value=probability):
            mask, provider = semantic_masks.generate(image, "person")
        self.assertEqual(provider, "local-person-segmentation")
        self.assertTrue(np.any(mask))

    def test_generate_falls_back_to_heuristic_when_model_finds_no_person(self):
        image = np.zeros((80, 100, 3), dtype=np.uint8)
        with mock.patch.object(subject, "person_mask",
                               side_effect=subject.SubjectModelUnavailable):
            mask, provider = semantic_masks.generate(image, "person")
        self.assertEqual(provider, "local-segmentation")
        self.assertEqual(mask.ndim, 2)

    def test_generate_prefers_learned_person_mask_for_subject_kind_too(self):
        probability = np.zeros((256, 256), np.float32)
        probability[64:192, 64:192] = 0.9
        image = np.full((80, 100, 3), 127, np.uint8)
        with mock.patch.object(subject, "person_mask", return_value=probability):
            mask, provider = semantic_masks.generate(image, "subject")
        self.assertEqual(provider, "local-person-segmentation")
        self.assertTrue(np.any(mask))


class tempfile_missing_model:
    """A path that will not resolve to a real file, for error-path tests."""

    def __enter__(self):
        self.path = Path("/nonexistent/lighttable-subject-model.tflite")
        return self.path

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
