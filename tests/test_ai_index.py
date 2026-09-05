import tempfile
import time
import unittest
from pathlib import Path

from film_lab_ai.providers import VisionProvider
from film_lab_ai.service import AIIndexService
from film_lab_ai.store import IndexStore


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {
            "vision": {"available": True},
            "foundationModels": {"available": False, "reason": "test"},
            "contentMode": "vision",
        }

    def analyze(self, image):
        self.calls.append(Path(image).read_bytes())
        return {
            "caption": "A red bicycle beside a cafe",
            "tags": ["bicycle", "cafe"],
            "ocr": ["OPEN"],
            "faces": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}],
            "provider": "test",
        }


class VisionProviderTests(unittest.TestCase):
    def test_multiple_requests_share_one_json_lines_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "fake-vision"
            helper.write_text("""#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "id": request["id"], "ok": True, "caption": "visible scene",
        "tags": ["scene"], "ocr": [], "faces": [],
    }), flush=True)
""")
            helper.chmod(0o755)
            image = root / "photo.jpg"
            image.write_bytes(b"fixture")
            provider = VisionProvider(helper)
            try:
                self.assertEqual(provider.analyze(image)["tags"], ["scene"])
                first_pid = provider._process.pid
                self.assertEqual(provider.analyze(image)["provider"], "vision")
                self.assertEqual(provider._process.pid, first_pid)
            finally:
                provider.shutdown()
            self.assertIsNone(provider._process)


class AIIndexServiceTests(unittest.TestCase):
    def make_service(self, root, analyzer, fingerprints=None):
        library = root / "photos"
        library.mkdir(exist_ok=True)
        fingerprints = fingerprints or {"one.jpg": "one-v1", "two.jpg": "two-v1"}
        return AIIndexService(
            library=library,
            data_root=root / "application-support",
            vision_helper=root / "unused-helper",
            list_images=lambda: list(fingerprints),
            source_key=lambda name: fingerprints[name],
            preview_bytes=lambda name: f"preview:{name}".encode(),
            analyzer=analyzer,
        )

    def wait_for_scan(self, service):
        for _ in range(200):
            status = service.status()
            if status["scanComplete"]:
                return status
            time.sleep(0.01)
        self.fail("local index did not finish")

    def test_disabled_by_default_and_stores_nothing_in_photo_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root, FakeAnalyzer())
            self.assertFalse(service.enabled)
            self.assertFalse(service.root.exists())
            self.assertEqual(service.results(["one.jpg"]), {})
            self.assertEqual(list((root / "photos").iterdir()), [])

    def test_enable_indexes_search_metadata_and_faces_locally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyzer = FakeAnalyzer()
            service = self.make_service(root, analyzer)
            service.enable()
            status = self.wait_for_scan(service)
            results = service.results(["one.jpg", "two.jpg"])

            self.assertEqual(status["indexed"], 2)
            self.assertEqual(len(analyzer.calls), 2)
            self.assertEqual(results["one.jpg"]["tags"], ["bicycle", "cafe"])
            self.assertEqual(results["one.jpg"]["ocr"], ["OPEN"])
            self.assertEqual(results["one.jpg"]["faceCount"], 1)
            self.assertTrue(service.store.database.is_relative_to(root / "application-support"))
            self.assertEqual(list((root / "photos").iterdir()), [])
            service.shutdown()

    def test_unchanged_photos_are_not_analyzed_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_service(root, FakeAnalyzer())
            first.enable()
            self.wait_for_scan(first)
            first.shutdown()

            second_analyzer = FakeAnalyzer()
            second = self.make_service(root, second_analyzer)
            self.assertTrue(second.enabled)
            second.start()
            self.wait_for_scan(second)
            self.assertEqual(second_analyzer.calls, [])
            second.shutdown()

    def test_pause_hides_results_and_delete_removes_generated_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root, FakeAnalyzer())
            service.enable()
            self.wait_for_scan(service)

            paused = service.disable()
            self.assertFalse(paused["enabled"])
            self.assertEqual(paused["indexed"], 2)
            self.assertEqual(service.results(["one.jpg"]), {})

            cleared = service.clear()
            self.assertFalse(cleared["enabled"])
            self.assertEqual(cleared["indexed"], 0)
            self.assertFalse(service.store.database.exists())
            service.shutdown()

    def test_a_damaged_generated_index_is_preserved_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "index.sqlite3"
            database.write_bytes(b"not a sqlite database")
            store = IndexStore(database)

            self.assertEqual(store.stats(), {"indexed": 0, "errors": 0})
            self.assertIsNotNone(store.last_recovery)
            self.assertEqual(
                (store.last_recovery / "index.sqlite3").read_bytes(),
                b"not a sqlite database",
            )
            self.assertTrue(database.is_file())


if __name__ == "__main__":
    unittest.main()
