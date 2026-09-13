# SPDX-License-Identifier: GPL-3.0-only
"""Subject/person segmentation must ride the existing hair model packaging.

No new model file, licence, or fetch step is introduced: subject_segmentation
reuses the identical pinned selfie_multiclass asset that scripts/fetch-hair-
model.py already downloads, verifies, and copies into every platform's
release bundle (see tests/test_hair_packaging.py for that inventory).
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SubjectPackagingTests(unittest.TestCase):
    def test_no_second_model_asset_is_introduced_for_subject_person_masks(self):
        source = (ROOT / "film_lab_ai" / "subject_segmentation.py").read_text()
        self.assertNotIn("MODEL_URL", source)
        self.assertNotIn(".tflite", source)
        self.assertIn("hair_segmentation", source)

    def test_third_party_notices_documents_the_second_use_of_the_model(self):
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("Selfie Multiclass Segmentation", notices)
        self.assertIn("subject", notices.lower())

    def test_build_scripts_still_only_reference_one_tflite_asset(self):
        for filename in ("scripts/build-release.sh",
                         "scripts/update-personal-app.sh",
                         "scripts/windows/build-release.ps1",
                         "scripts/linux/build-release.py"):
            path = ROOT / filename
            if not path.is_file():
                continue
            with self.subTest(filename=filename):
                text = path.read_text()
                self.assertLessEqual(text.count("selfie_multiclass_256x256.tflite"), 1,
                                     "subject/person reuse must not add a second copy")


if __name__ == "__main__":
    unittest.main()
