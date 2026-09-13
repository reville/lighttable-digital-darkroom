# SPDX-License-Identifier: GPL-3.0-only
"""The server side of camera profiles: folders, the picker, and the default.

The profile folder is a temporary directory of synthetic .dcp files written
by ``camera_profile_write``; the photo's camera identity is stubbed so no RAW
file is needed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import camera_profile
import platform_paths
import server
from camera_profile_write import grid_bytes, write_profile


def _identity(make="NIKON CORPORATION", model="NIKON D7100"):
    key = f"{make.casefold()}|{model.casefold()}"
    return {"key": key, "keys": {"model": key, "serial": key, "iso": key},
            "label": f"{make} {model}", "serial": "", "iso": ""}


class CameraProfileFolderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefs = self.root / "prefs.json"
        patch = mock.patch.object(server, "PREFS_FILE", self.prefs)
        patch.start(); self.addCleanup(patch.stop)

    def test_lighttable_data_folder_is_the_first_default_on_every_platform(self):
        folders = server.camera_profile_default_folders()
        self.assertEqual(folders[0], platform_paths.data_directory() / "CameraProfiles")
        self.assertTrue(all(isinstance(folder, Path) for folder in folders))

    def test_preference_wins_then_the_first_existing_default(self):
        own = self.root / "CameraProfiles"
        with mock.patch.object(server, "camera_profile_default_folders",
                               return_value=[self.root / "missing", own]):
            self.assertIsNone(server.camera_profile_folder())
            own.mkdir()
            self.assertEqual(server.camera_profile_folder(), own)
            chosen = self.root / "Chosen"
            chosen.mkdir()
            self.prefs.write_text(json.dumps({"cameraProfileFolder": str(chosen)}))
            self.assertEqual(server.camera_profile_folder(), chosen)
            self.prefs.write_text(json.dumps({"cameraProfileFolder": str(self.root / "gone")}))
            self.assertIsNone(server.camera_profile_folder())

    def test_bundled_profile_resolves_without_any_folder(self):
        with mock.patch.object(server, "camera_profile_default_folders", return_value=[]):
            self.assertEqual(server.resolve_camera_profile(camera_profile.BUNDLED_STANDARD_NAME),
                             camera_profile.BUNDLED_STANDARD_FILE)
            self.assertIsNone(server.resolve_camera_profile("Nikon D7100 Adobe Standard.dcp"))
            self.assertIsNone(server.resolve_camera_profile("../escape.dcp"))


class CameraProfilePickerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "Profiles"
        self.folder.mkdir()
        self.prefs = Path(self.temporary.name) / "prefs.json"
        self.prefs.write_text(json.dumps({"cameraProfileFolder": str(self.folder)}))
        for target, value in (("PREFS_FILE", self.prefs),
                              ("is_raw", lambda name: True),
                              ("raw_camera_identity", lambda name: _identity())):
            patch = mock.patch.object(server, target, value)
            patch.start(); self.addCleanup(patch.stop)
        identity_grid = grid_bytes(6, 2, 1, lambda h, s, v: (0.0, 1.0, 1.0))
        write_profile(self.folder / "Nikon D7100 Adobe Standard.dcp",
                      name="Adobe Standard", tone_curve=[[0.0, 0.0], [1.0, 1.0]])
        write_profile(self.folder / "Generic Look.dcp", name="Warm Look",
                      camera_model="NIKON D7100",
                      hue_sat_dims=(6, 2, 1), hue_sat_map=identity_grid)
        write_profile(self.folder / "Named.dcp", name="Nikon D7100 Portrait",
                      tone_curve=[[0.0, 0.0], [1.0, 1.0]])
        write_profile(self.folder / "Other Camera.dcp", name="Other",
                      camera_model="Canon EOS 5D", tone_curve=[[0.0, 0.0], [1.0, 1.0]])
        (self.folder / "broken.dcp").write_bytes(b"not a profile")

    def test_lists_the_bundled_look_then_name_and_tag_matches(self):
        result = server.camera_profiles_for("photo.NEF")
        self.assertTrue(result["available"])
        self.assertEqual(result["camera"], "NIKON CORPORATION NIKON D7100")
        files = [item["file"] for item in result["profiles"]]
        self.assertEqual(files[0], camera_profile.BUNDLED_STANDARD_NAME)
        self.assertTrue(result["profiles"][0]["bundled"])
        self.assertEqual(files[1], "Nikon D7100 Adobe Standard.dcp")
        self.assertEqual(set(files[2:]), {"Generic Look.dcp", "Named.dcp"})
        self.assertNotIn("Other Camera.dcp", files)
        self.assertNotIn("broken.dcp", files)
        names = {item["file"]: item["name"] for item in result["profiles"]}
        self.assertEqual(names["Generic Look.dcp"], "Warm Look")
        self.assertEqual(names["Named.dcp"], "Nikon D7100 Portrait")
        self.assertEqual(names["Nikon D7100 Adobe Standard.dcp"], "Adobe Standard")

    def test_bundled_look_is_offered_even_without_a_folder(self):
        self.prefs.write_text(json.dumps({"cameraProfileFolder": str(self.folder / "gone")}))
        with mock.patch.object(server, "camera_profile_default_folders", return_value=[]):
            result = server.camera_profiles_for("photo.NEF")
        self.assertFalse(result["available"])
        self.assertEqual([item["file"] for item in result["profiles"]],
                         [camera_profile.BUNDLED_STANDARD_NAME])

    def test_identity_tags_are_parsed_once_per_file_version(self):
        path = self.folder / "Generic Look.dcp"
        with mock.patch.object(camera_profile, "profile_identity",
                               wraps=camera_profile.profile_identity) as spy:
            server.camera_profile_identity(path)
            server.camera_profile_identity(path)
            self.assertEqual(spy.call_count, 1)
            write_profile(path, name="Cooler Look", camera_model="NIKON D7100",
                          tone_curve=[[0.0, 0.0], [1.0, 1.0]])
            import os
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(server.camera_profile_identity(path)["name"], "Cooler Look")
            self.assertEqual(spy.call_count, 2)


class NewPhotoCameraProfileDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.prefs = Path(self.temporary.name) / "prefs.json"
        patch = mock.patch.object(server, "PREFS_FILE", self.prefs)
        patch.start(); self.addCleanup(patch.stop)

    def test_never_edited_photos_start_with_lighttable_standard(self):
        params, _ = server.effective_new_photo_defaults()
        self.assertEqual(params["camera_profile"], camera_profile.BUNDLED_STANDARD_NAME)
        self.assertEqual(server.default_camera_profile_name({}), camera_profile.BUNDLED_STANDARD_NAME)
        self.assertEqual(server.default_camera_profile_name({"cameraProfile": "junk"}),
                         camera_profile.BUNDLED_STANDARD_NAME)

    def test_preference_can_choose_the_built_in_curve(self):
        self.prefs.write_text(json.dumps({"newPhotoDefaults": {"cameraProfile": "builtin"}}))
        params, _ = server.effective_new_photo_defaults()
        self.assertEqual(params["camera_profile"], "")
        self.prefs.write_text(json.dumps({"newPhotoDefaults": {"cameraProfile": "standard"}}))
        params, _ = server.effective_new_photo_defaults()
        self.assertEqual(params["camera_profile"], camera_profile.BUNDLED_STANDARD_NAME)


if __name__ == "__main__":
    unittest.main()
