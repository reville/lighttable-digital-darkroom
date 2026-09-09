import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import catalog
import catalog_scan
from thumbnail_warmup import ThumbnailWarmup


class ThumbnailWarmupTests(unittest.TestCase):
    def test_scan_callback_observes_committed_rows_and_includes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.dng").write_bytes(b"test source")
            cat = catalog.Catalog(root / "library.sqlite3")
            self.addCleanup(cat.close)
            source = cat.add_source(root)
            seen = []

            def queued(source_id, relpath):
                self.assertFalse(cat.connection.in_transaction)
                self.assertIsNotNone(cat.image_id_for(source_id, relpath))
                seen.append(relpath)

            for _ in range(2):
                result = catalog_scan.scan_source(
                    cat, source, read_metadata_for_new=False,
                    on_local_file=queued)
                self.assertTrue(result["complete"])
            self.assertEqual(seen, ["photo.dng", "photo.dng"])

    def test_busy_queue_is_bounded_deduplicated_and_cancellable(self):
        built = mock.Mock()
        worker = ThumbnailWarmup(built, busy=lambda: True,
                                 capacity=2, interval=0.001)
        self.addCleanup(worker.cancel)
        self.assertTrue(worker.enqueue(1, "a.dng"))
        self.assertFalse(worker.enqueue(1, "a.dng"))
        self.assertTrue(worker.enqueue(1, "b.dng"))
        self.assertFalse(worker.enqueue(1, "c.dng"))
        thread = worker._thread
        worker.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        built.assert_not_called()
        self.assertFalse(worker.enqueue(1, "d.dng"))

    def test_failed_decode_does_not_starve_following_thumbnail(self):
        finished = threading.Event()
        names = []

        def build(name):
            names.append(name)
            if name.endswith("bad.dng"):
                raise ValueError("bad image")
            finished.set()

        worker = ThumbnailWarmup(build, interval=0.005)
        self.addCleanup(worker.cancel)
        worker.enqueue(3, "bad.dng")
        worker.enqueue(3, "good.dng")
        self.assertTrue(finished.wait(1))
        worker.cancel()
        self.assertEqual(names, ["3:bad.dng", "3:good.dng"])

    def test_overflow_prepares_entire_source_with_bounded_pages(self):
        busy = threading.Event()
        busy.set()
        completed = threading.Event()
        seen = set()
        pages = []
        def refill(source, after, limit):
            pages.append((source, after, limit))
            return [(i, f"{i}.jpg") for i in range(after + 1, min(after + limit, 19) + 1)]
        def build(name):
            seen.add(name)
            if len(seen) == 19:
                completed.set()
        worker = ThumbnailWarmup(build, busy=busy.is_set, refill=refill,
                                 capacity=2, interval=0.001)
        self.addCleanup(worker.cancel)
        for i in range(1, 20):
            worker.enqueue(3, f"{i}.jpg")
        self.assertEqual(len(worker._queue), 2)
        busy.clear()
        self.assertTrue(completed.wait(2))
        worker.cancel()
        self.assertEqual(seen, {f"3:{i}.jpg" for i in range(1, 20)})
        self.assertTrue(all(limit == 2 for _, _, limit in pages))
        self.assertGreater(len(pages), 1)

    def test_visible_decoder_contention_is_retried(self):
        built = mock.Mock(side_effect=[False, None])
        worker = ThumbnailWarmup(built, interval=0.001)
        self.addCleanup(worker.cancel)
        worker.enqueue(1, "a.jpg")
        worker._thread.join(1)
        self.assertEqual(built.call_count, 2)
        self.assertFalse(worker._pending)

    def test_busy_worker_expires(self):
        built = mock.Mock()
        worker = ThumbnailWarmup(built, busy=lambda: True,
                                 interval=0.001, lifetime=0.01)
        self.addCleanup(worker.cancel)
        worker.enqueue(1, "a.dng")
        thread = worker._thread
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(list(worker._queue), [])
        built.assert_not_called()


if __name__ == "__main__":
    unittest.main()
