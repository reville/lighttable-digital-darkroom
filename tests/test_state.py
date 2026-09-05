import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class StateCleaningTests(unittest.TestCase):
    def test_keywords_are_trimmed_deduplicated_and_ordered(self):
        self.assertEqual(
            server.clean_keywords([" portrait ", "PORTRAIT", "blue   sky", ""]),
            ["portrait", "blue sky"],
        )

    def test_versions_keep_only_valid_edit_snapshots(self):
        versions = server.clean_versions([
            {"id": "v1", "name": " First  look ", "created": "2026-09-01",
             "params": {}, "grade": {"texture": 0.35},
             "crop": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
             "masks": [{"type": "radial", "grade": {"exposure": 1}}],
             "heals": [{"target": [0.4, 0.4], "source": [0.6, 0.4]}],
             "optics": {"vertical": 0.2}},
            {"name": "   "},
            "not a version",
        ])
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["name"], "First look")
        self.assertEqual(versions[0]["grade"]["texture"], 0.35)
        self.assertEqual(versions[0]["crop"]["w"], 0.8)
        self.assertEqual(versions[0]["masks"][0]["type"], "radial")
        self.assertEqual(versions[0]["heals"][0]["target"], [0.4, 0.4])
        self.assertEqual(versions[0]["optics"]["vertical"], 0.2)

    def test_legacy_preset_migrates_as_complete_film_style(self):
        preset = server.clean_preset({
            "name": "Old Look", "params": {"profile_enabled": False},
            "grade": {"contrast": 0.25},
        })
        self.assertTrue(preset["includeFilm"])
        self.assertEqual(preset["presetType"], "style")
        self.assertIn("contrast", preset["includedGrade"])
        self.assertIn("sharpness", preset["includedGrade"])

    def test_tool_preset_includes_only_changed_grade_controls(self):
        preset = server.clean_preset({
            "name": "Texture Tool", "presetType": "tool",
            "includeFilm": False,
            "grade": {"texture": 0.4},
        })
        self.assertFalse(preset["includeFilm"])
        self.assertEqual(preset["includedGrade"], ["texture"])

    def test_preset_keeps_local_edits_and_lens_geometry(self):
        preset = server.clean_preset({
            "name": "Local work", "includeFilm": False,
            "masks": [{"type": "linear", "grade": {"contrast": 0.4}}],
            "heals": [{"mode": "clone", "target": [0.3, 0.3]}],
            "optics": {"profileEnabled": True, "horizontal": -0.25},
        })
        self.assertEqual(preset["masks"][0]["type"], "linear")
        self.assertEqual(preset["heals"][0]["mode"], "clone")
        self.assertTrue(preset["optics"]["profileEnabled"])
        self.assertEqual(preset["optics"]["horizontal"], -0.25)


class FolderLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_folder = server.FOLDER
        server.FOLDER = Path(self.temp.name)

    def tearDown(self):
        server.FOLDER = self.old_folder
        self.temp.cleanup()

    def touch(self, relative: str, content: bytes = b"photo") -> Path:
        path = server.FOLDER / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_recursive_library_uses_relative_paths_and_folder_counts(self):
        self.touch("cover.jpg")
        self.touch("Trips/Paris/frame.jpg")
        self.touch(".hidden/ignored.jpg")
        self.touch("film-exports/ignored.jpg")

        names = server.list_images()
        self.assertEqual(names, ["cover.jpg", "Trips/Paris/frame.jpg"])
        rows = {row["path"]: row for row in server.folder_rows(names)}
        self.assertEqual(rows[""]["directCount"], 1)
        self.assertEqual(rows[""]["totalCount"], 2)
        self.assertEqual(rows["Trips"]["totalCount"], 1)
        self.assertEqual(rows["Trips/Paris"]["directCount"], 1)

    def test_browser_payload_hides_videos_in_folder_mode(self):
        self.touch("cover.jpg")
        self.touch("Clips/behind-scenes.mov", b"video")

        with mock.patch.object(server, "CATALOG", None), \
                mock.patch.object(server, "AI_INDEX", None):
            rows, snapshot = server.library_payload()

        self.assertEqual([row["name"] for row in rows], ["cover.jpg"])
        self.assertEqual(snapshot["names"], ["cover.jpg"])
        self.assertEqual(snapshot["total"], 1)
        folders = {row["path"]: row for row in snapshot["folders"]}
        self.assertEqual(folders["Clips"]["totalCount"], 0)

    def test_source_path_rejects_parent_traversal(self):
        outside = Path(self.temp.name).parent / "outside.jpg"
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaises(ValueError):
            server.src_path("../outside.jpg")

    def test_rename_folder_moves_edit_state_with_photos(self):
        self.touch("Old/frame.jpg")
        server.state_path().write_text(json.dumps({
            "images": {"Old/frame.jpg": {"rating": 5}},
        }))

        renamed = server.rename_subfolder("Old", "New")

        self.assertEqual(renamed, "New")
        self.assertTrue((server.FOLDER / "New/frame.jpg").is_file())
        state = json.loads(server.state_path().read_text())
        self.assertEqual(state["images"]["New/frame.jpg"]["rating"], 5)
        self.assertNotIn("Old/frame.jpg", state["images"])

    def test_move_photos_preserves_xmp_and_edit_state(self):
        self.touch("Inbox/frame.jpg")
        self.touch("Inbox/frame.xmp", b"metadata")
        (server.FOLDER / "Archive").mkdir()
        server.state_path().write_text(json.dumps({
            "images": {"Inbox/frame.jpg": {"status": "approved"}},
        }))

        moved = server.move_images(["Inbox/frame.jpg"], "Archive")

        self.assertEqual(moved, ["Archive/frame.jpg"])
        self.assertTrue((server.FOLDER / "Archive/frame.jpg").is_file())
        self.assertTrue((server.FOLDER / "Archive/frame.xmp").is_file())
        state = json.loads(server.state_path().read_text())
        self.assertEqual(state["images"]["Archive/frame.jpg"]["status"], "approved")

    def test_state_failure_rolls_back_a_photo_move_without_losing_sidecars(self):
        self.touch("Inbox/frame.jpg")
        self.touch("Inbox/frame.xmp", b"metadata")
        (server.FOLDER / "Archive").mkdir()
        server.state_path().write_text(json.dumps({
            "images": {"Inbox/frame.jpg": {"rating": 5}},
        }))

        with mock.patch.object(server, "write_state",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                server.move_images(["Inbox/frame.jpg"], "Archive")

        self.assertTrue((server.FOLDER / "Inbox/frame.jpg").is_file())
        self.assertEqual((server.FOLDER / "Inbox/frame.xmp").read_bytes(),
                         b"metadata")
        self.assertFalse((server.FOLDER / "Archive/frame.jpg").exists())
        self.assertFalse((server.FOLDER / "Archive/frame.xmp").exists())

    def test_state_save_is_atomic_and_leaves_no_temporary_file(self):
        server.save_image_state("frame.jpg", {"rating": 4})

        state = json.loads(server.state_path().read_text())
        self.assertEqual(state["images"]["frame.jpg"]["rating"], 4)
        self.assertEqual(list(server.FOLDER.glob(".*.tmp")), [])

    def test_invalid_external_state_does_not_replace_last_valid_memory(self):
        server.save_image_state("frame.jpg", {"rating": 5})
        self.assertEqual(server.load_state()["images"]["frame.jpg"]["rating"], 5)
        server.state_path().write_text("{broken external write")

        recovered = server.load_state()

        self.assertEqual(recovered["images"]["frame.jpg"]["rating"], 5)

    def test_cold_start_recovers_the_last_valid_state_backup(self):
        server.save_image_state("frame.jpg", {"rating": 4})
        server.save_image_state("frame.jpg", {"rating": 5})
        server.state_path().write_text("{interrupted external write")
        server._STATE_CACHE_PATH = None
        server._STATE_CACHE_STAMP = None
        server._STATE_CACHE = {"images": {}}

        recovered = server.load_state()

        self.assertEqual(recovered["images"]["frame.jpg"]["rating"], 4)
        self.assertTrue(Path(str(server.state_path()) + ".backup").is_file())


if __name__ == "__main__":
    unittest.main()
