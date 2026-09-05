from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import catalog
import watch_workflow


def write_photo(path: Path, *, size=(32, 24), colour="gray") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, quality=92)
    return path


class WatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.catalog_path = self.root / "catalog.sqlite3"
        self.catalog = catalog.Catalog(self.catalog_path)
        self.addCleanup(self.catalog.close)
        self.catalog.add_source(self.photos)
        self.watch = {
            "id": "capture", "name": "Capture", "path": str(self.photos),
            "enabled": True, "recursive": False, "mode": "catalog",
            "presetId": "", "follow": False,
        }

    def service(self, watch=None, *, presets=None) -> watch_workflow.WatchService:
        configured = watch or self.watch
        return watch_workflow.WatchService(
            self.catalog, lambda: [configured], presets=lambda: presets or [],
            poll_seconds=0.05)

    def test_progressive_write_waits_for_two_matching_polls(self):
        path = write_photo(self.photos / "capture.jpg", size=(20, 20))
        service = self.service()
        service.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 0)

        write_photo(path, size=(40, 20), colour="red")
        service.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 0)
        service.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 1)

    def test_settled_file_registers_once_across_restart(self):
        write_photo(self.photos / "capture.jpg")
        first = self.service()
        first.poll_once(); first.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 1)

        self.catalog.close()
        self.catalog = catalog.Catalog(self.catalog_path)
        second = self.service()
        second.poll_once(); second.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 1)

    def test_ingest_copies_with_verification_and_leaves_source(self):
        capture = self.root / "tether"
        destination = self.photos / "incoming"
        original = write_photo(capture / "frame.jpg", colour="navy")
        ingest = dict(self.watch, mode="ingest", path=str(capture), request={
            "destination": str(destination),
            "folderTemplate": "session",
            "filenameTemplate": "{filename}",
            "verify": "hash",
            "onDuplicate": "skip",
        })
        service = self.service(ingest)
        service.poll_once(); service.poll_once()

        copied = destination / "session" / "frame.jpg"
        self.assertTrue(original.is_file())
        self.assertEqual(copied.read_bytes(), original.read_bytes())
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 1)

    def test_duplicate_header_hash_is_skipped(self):
        first = write_photo(self.photos / "a.jpg", colour="green")
        shutil.copyfile(first, self.photos / "b.jpg")
        service = self.service()
        service.poll_once(); service.poll_once()
        self.assertEqual(self.catalog.query({"limit": 10})["total"], 1)
        self.assertEqual(service.status[0]["handled"], 1)

    def test_missing_folder_is_unavailable_without_error(self):
        watch = dict(self.watch, path=str(self.root / "missing"))
        status = self.service(watch).poll_once()[0]
        self.assertFalse(status["available"])
        self.assertEqual(status["error"], "")

    def test_follow_reports_the_latest_arrival(self):
        watch = dict(self.watch, follow=True)
        service = self.service(watch)
        write_photo(self.photos / "a.jpg", colour="red")
        service.poll_once(); service.poll_once()
        write_photo(self.photos / "b.jpg", colour="blue")
        service.poll_once(); service.poll_once()
        status = service.status[0]
        self.assertTrue(status["follow"])
        self.assertTrue(status["latest"].endswith("b.jpg"))
        self.assertEqual(status["handled"], 2)

    def test_preset_lands_on_the_arrival(self):
        preset = {
            "id": "portrait-look", "includeFilm": True,
            "params": {"film_on": False},
            "grade": {"exposure": 0.75, "contrast": 12},
            "includedGrade": ["exposure"],
        }
        watch = dict(self.watch, presetId="portrait-look")
        write_photo(self.photos / "portrait.jpg")
        service = self.service(watch, presets=[preset])
        service.poll_once(); service.poll_once()

        item = self.catalog.query({"limit": 10})["items"][0]
        state = self.catalog.state_for(item["id"])
        self.assertEqual(state["params"], {"film_on": False})
        self.assertEqual(state["grade"], {"exposure": 0.75})


if __name__ == "__main__":
    unittest.main()
