# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import media_availability

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
    @mock.patch("film_lab_ai.providers.platform.system", return_value="Darwin")
    def test_multiple_requests_share_one_json_lines_process(self, _system):
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

    def test_executable_macos_helper_is_unavailable_on_linux(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "LightTableVision"
            helper.write_text("not a Linux executable")
            helper.chmod(0o755)
            with mock.patch("film_lab_ai.providers.platform.system", return_value="Linux"):
                provider = VisionProvider(helper)
                self.assertFalse(provider.available)
                with self.assertRaises(RuntimeError):
                    provider.analyze(Path(temporary) / "photo.jpg")
                self.assertIsNone(provider._process)


class AIIndexServiceTests(unittest.TestCase):
    def make_service(self, root, analyzer, fingerprints=None, **options):
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
            **options,
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

    def test_unavailable_inputs_clear_old_errors_without_hashing_or_decoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyzer = FakeAnalyzer()
            states = {'empty.jpg': 'empty', 'cloud.RAF': 'cloud-only',
                      'missing.jpg': 'unavailable', 'bad.jpg': 'local',
                      'good.jpg': 'local'}
            service = self.make_service(
                root, analyzer, {name: name for name in states},
                source_availability=states.get)
            good_preview = (Path(__file__).parent / 'fixtures/photos/portrait.jpg').read_bytes()
            for name in states:
                service.store.record_error(name, name, 'old decode failure', 1)

            def fingerprint(name):
                self.assertEqual(states[name], 'local', 'must not hydrate an input')
                return name

            def preview(name):
                self.assertEqual(states[name], 'local', 'must not decode an input')
                if name == 'bad.jpg':
                    raise ValueError('corrupt image')
                return good_preview

            service._source_key = fingerprint
            service._preview_bytes = preview
            try:
                service.enable()
                status = self.wait_for_scan(service)
                self.assertEqual((status['completed'], status['total']), (5, 5))
                self.assertEqual((status['indexed'], status['errors'], status['skipped']), (1, 1, 3))
                self.assertEqual(status['skippedReasons'],
                                 {'empty': 1, 'cloud-only': 1, 'unavailable': 1})
                self.assertIn('bad.jpg: corrupt image', status['lastError'])
                self.assertEqual(analyzer.calls, [good_preview])
                self.assertEqual(set(service.results(list(states))), {'good.jpg'})
            finally:
                service.shutdown()

    def test_empty_file_is_indexed_when_restored_and_offline_metadata_is_retained(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyzer = FakeAnalyzer()
            library = root / 'photos'
            service = self.make_service(
                root, analyzer,
                source_availability=lambda name: media_availability.index_availability(library / name))
            Image.new('RGB', (32, 24), 'navy').save(library / 'one.jpg')
            (library / 'two.jpg').touch()
            service._preview_bytes = lambda name: (library / name).read_bytes()
            try:
                service.enable()
                status = self.wait_for_scan(service)
                self.assertEqual((status['indexed'], status['errors'], status['skipped']), (1, 0, 1))
                self.assertEqual(status['lastError'], '')
                self.assertEqual(len(analyzer.calls), 1)
                self.assertEqual((library / 'two.jpg').stat().st_size, 0)

                Image.new('RGB', (32, 24), 'red').save(library / 'two.jpg')
                service.start()
                status = self.wait_for_scan(service)
                self.assertEqual((status['indexed'], status['errors'], status['skipped']), (2, 0, 0))
                self.assertEqual(len(analyzer.calls), 2, 'keep the already indexed photo')

                (library / 'one.jpg').unlink()
                service.start()
                status = self.wait_for_scan(service)
                self.assertEqual(status['skippedReasons'], {'unavailable': 1})
                self.assertEqual(len(service.results(['one.jpg', 'two.jpg'])), 2)
                self.assertEqual(len(analyzer.calls), 2, 'preserve existing search metadata')
                service.clear()
                self.assertEqual(service.status()['skipped'], 0)
            finally:
                service.shutdown()

    def test_fingerprint_failure_does_not_stop_the_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary), FakeAnalyzer())
            service._preview_bytes = lambda name: (Path(__file__).parent / 'fixtures/photos/portrait.jpg').read_bytes()
            with mock.patch.object(service, '_source_key',
                                   side_effect=[OSError('read denied'), 'two-v1']):
                try:
                    service.enable()
                    status = self.wait_for_scan(service)
                    self.assertEqual((status['completed'], status['indexed'], status['errors']), (2, 1, 1))
                    self.assertIn('one.jpg: read denied', status['lastError'])
                    self.assertEqual(set(service.results(['one.jpg', 'two.jpg'])), {'two.jpg'})
                finally:
                    service.shutdown()

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

    def test_clear_removes_stray_analysis_previews(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root, FakeAnalyzer())
            service.enable()
            self.wait_for_scan(service)

            orphan = service.root / "photo-orphaned.jpg"
            orphan.write_bytes(b"preview left by an interrupted analysis")
            service.clear()

            self.assertFalse(orphan.exists())
            service.shutdown()

    def test_analysis_leaves_no_preview_files_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root, FakeAnalyzer())
            service.enable()
            self.wait_for_scan(service)

            self.assertEqual(list(service.root.glob("photo-*.jpg")), [])
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
