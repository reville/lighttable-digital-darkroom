# SPDX-License-Identifier: GPL-3.0-only
"""A matched lens profile switches itself on only for never-saved lens state."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import edits
import server
from lighttable_cli.manifest import schema


def match(found=True, confident=True):
    return {"found": found, "confident": confident, "reason": "x",
            "profile": {"lensModel": "24-70 A"} if found else None, "candidates": []}


class LensProfileDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.prefs = Path(self.temporary.name) / "prefs.json"
        patch = mock.patch.object(server, "PREFS_FILE", self.prefs)
        patch.start(); self.addCleanup(patch.stop)
        exif = mock.patch.object(server, "exif_for", return_value={"Make": "Example"})
        exif.start(); self.addCleanup(exif.stop)

    def prefer(self, value):
        self.prefs.write_text(json.dumps({"newPhotoDefaults": {"lensProfileAuto": value}}))

    def test_preference_defaults_on_and_can_be_switched_off(self):
        self.assertTrue(server.lens_profile_default_enabled())
        self.prefer(False)
        self.assertFalse(server.lens_profile_default_enabled())
        self.prefs.write_text(json.dumps({"newPhotoDefaults": "junk"}))
        self.assertTrue(server.lens_profile_default_enabled())

    def test_never_saved_lens_state_starts_with_a_confident_match_enabled(self):
        with mock.patch.object(server.edits, "lens_match_for", return_value=match()):
            optics = server.default_optics_for("1:a.raf", {"optics": edits.clean_optics(None), "opticsSaved": False})
            raw_record = server.default_optics_for("1:a.raf", {"optics": None})
            report = server.lens_match_for_photo("1:a.raf")
        self.assertTrue(optics["profileEnabled"])
        self.assertTrue(raw_record["profileEnabled"])
        self.assertTrue(report["autoEnabled"])
        self.assertEqual({k: v for k, v in optics.items() if k != "profileEnabled"},
                         {k: v for k, v in edits.clean_optics(None).items() if k != "profileEnabled"})

    def test_ambiguous_or_inferred_matches_stay_off(self):
        for outcome in (match(found=False, confident=False), match(found=True, confident=False)):
            with mock.patch.object(server.edits, "lens_match_for", return_value=outcome):
                self.assertFalse(server.default_optics_for("1:a.raf", {"optics": None})["profileEnabled"])
                self.assertFalse(server.lens_match_for_photo("1:a.raf")["autoEnabled"])

    def test_saved_lens_state_is_never_overridden(self):
        saved = edits.clean_optics({"profileEnabled": False, "distortion": 0.2})
        with mock.patch.object(server.edits, "lens_match_for", return_value=match()) as matcher:
            kept = server.default_optics_for("1:a.raf", {"optics": saved, "opticsSaved": True})
            raw = server.default_optics_for("1:a.raf", {"optics": {"profileEnabled": False}})
        self.assertEqual(kept, saved)
        self.assertFalse(raw["profileEnabled"])
        matcher.assert_not_called()

    def test_preference_off_skips_the_lens_lookup_entirely(self):
        self.prefer(False)
        with mock.patch.object(server.edits, "lens_match_for", return_value=match()) as matcher:
            optics = server.default_optics_for("1:a.raf", {"optics": None})
            report = server.lens_match_for_photo("1:a.raf")
        self.assertFalse(optics["profileEnabled"])
        self.assertFalse(report["autoEnabled"])
        self.assertTrue(report["confident"])
        matcher.assert_called_once()

    def test_entries_report_whether_lens_state_was_saved(self):
        state = {"images": {"a.jpg": {"optics": {"distortion": 0.1}}, "b.jpg": {"grade": {"exposure": 1}}}}
        saved = server.entry_for(state, "a.jpg")
        unsaved = server.entry_for(state, "b.jpg")
        self.assertTrue(saved["opticsSaved"])
        self.assertEqual(saved["optics"]["distortion"], 0.1)
        self.assertFalse(unsaved["opticsSaved"])
        self.assertEqual(unsaved["optics"], edits.clean_optics(None))


class OpticsSchemaTests(unittest.TestCase):
    def test_schema_lists_every_optics_field_with_its_travel(self):
        document = schema({"optics": {"defaults": edits.OPTICS_DEFAULTS,
                                      "ranges": edits.OPTICS_RANGES}})
        optics = document["$defs"]["stateUpdate"]["properties"]["optics"]["properties"]
        self.assertEqual(optics["profileChromatic"], {"type": "boolean"})
        self.assertEqual(optics["defringePurple"], {"type": "number", "minimum": 0.0, "maximum": 1.0})
        self.assertEqual(optics["defringeGreenHueEnd"], {"type": "number", "minimum": 0.0, "maximum": 360.0})
        self.assertEqual(optics["rotate"], {"type": "number", "minimum": -15.0, "maximum": 15.0})
        self.assertEqual(optics["profileOverride"], {"$ref": "#/$defs/lensProfileOverride"})
        self.assertEqual(set(optics), set(edits.OPTICS_DEFAULTS))


if __name__ == "__main__":
    unittest.main()
