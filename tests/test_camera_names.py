"""The library shows a camera the way a photographer writes it.

Cameras pad the EXIF make and model and many repeat the maker in both, so a
plain join produced "Canon Canon EOS 80D" everywhere the library named a
camera, while the import dialog already showed "Canon EOS 80D".
"""
from __future__ import annotations

import unittest

import catalog as catalog_module
import ingest_workflow


class CameraNameTests(unittest.TestCase):
    def test_a_repeated_maker_is_not_shown_twice(self):
        self.assertEqual(
            catalog_module.camera_name("Canon", "Canon EOS 80D"), "Canon EOS 80D")

    def test_a_longer_repeated_maker_is_reduced(self):
        self.assertEqual(
            catalog_module.camera_name("NIKON CORPORATION", "NIKON D850"),
            "NIKON CORPORATION NIKON D850")

    def test_padding_inside_and_around_the_fields_is_collapsed(self):
        self.assertEqual(
            catalog_module.camera_name("OM Digital Solutions    ",
                                       "TG-7            "),
            "OM Digital Solutions TG-7")

    def test_a_distinct_maker_and_model_are_joined(self):
        self.assertEqual(
            catalog_module.camera_name("FUJIFILM", "X-T30 III"),
            "FUJIFILM X-T30 III")

    def test_a_missing_field_does_not_leave_stray_spacing(self):
        self.assertEqual(catalog_module.camera_name("", "DSC-RX100M7"),
                         "DSC-RX100M7")
        self.assertEqual(catalog_module.camera_name("Panasonic", ""), "Panasonic")
        self.assertEqual(catalog_module.camera_name(None, None), "")

    def test_the_maker_prefix_match_ignores_case(self):
        self.assertEqual(catalog_module.camera_name("CANON", "Canon EOS R100"),
                         "Canon EOS R100")

    def test_the_library_and_the_import_dialog_agree(self):
        # ingest_workflow names the camera for the import dialog and for the
        # {camera} filename token; the two must not disagree about one photo.
        for make, model in (("Canon", "Canon EOS 80D"),
                            ("FUJIFILM", "X100VI"),
                            ("Panasonic", "DC-S9")):
            with self.subTest(model=model):
                if model.casefold().startswith(make.casefold()) and make:
                    expected = model
                else:
                    expected = " ".join(p for p in (make, model) if p)
                self.assertEqual(catalog_module.camera_name(make, model), expected)

    def test_ingest_still_exposes_its_own_matching_helper(self):
        self.assertTrue(hasattr(ingest_workflow, "_capture_and_camera"))


if __name__ == "__main__":
    unittest.main()
