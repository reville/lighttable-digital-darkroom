"""Real catalog/validator regression tests for background mask interleavings."""
import threading
from unittest import mock

import numpy as np

import edits
import semantic_masks
import server
from jobs import JobRegistry
from test_server_catalog import CatalogServerTestCase


class BackgroundMaskTests(CatalogServerTestCase):
    def setUp(self):
        super().setUp()
        self.name = self.qualified("a.jpg")
        self.payload = {"bitmap": semantic_masks.encode_bitmap(np.ones((8, 8), dtype=np.float32)),
                        "provider": "fixture"}
        self.manual = edits.clean_masks([{"id": "manual", "type": "radial", "x": .5, "y": .5}])[0]
        self.patches = [mock.patch.object(server, "JOBS", JobRegistry()),
                        mock.patch.object(server, "queue_sidecar"),
                        mock.patch.object(server, "_queue_mirror")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_batch(self, detector, categories=None):
        queue = server.BatchSemanticMaskQueue()
        with mock.patch.object(server, "semantic_mask_payload", side_effect=detector):
            queue.start([self.name], categories or ["subject"])
            queue.thread.join(5)
            self.assertFalse(queue.thread.is_alive())
        return queue

    def test_all_detectors_fail_without_claiming_success(self):
        queue = self.run_batch(lambda *a, **kw: (_ for _ in ()).throw(ValueError("No subject found")))
        status = queue.status()
        self.assertEqual((status["completed"], status["failed"], status["masked"]), (0, 1, 0))
        self.assertIn("No subject found", status["errors"][0])
        self.assertEqual(server.JOBS.get(queue.job_id)["state"], "failed")
        self.assertEqual(server.catalog_entry_for(self.name)["masks"], [])

    def test_manual_save_during_detection_survives_and_batch_undo_is_selective(self):
        def detect(*args, **kwargs):
            server.save_image_state(self.name, {"masks": [self.manual], "grade": {"exposure": .3}})
            return self.payload
        queue = self.run_batch(detect)
        self.assertEqual(queue.status()["completed"], 1)
        saved = server.catalog_entry_for(self.name)
        self.assertEqual(len(saved["masks"]), 2)
        later = dict(self.manual, id="later")
        server.save_image_state(self.name, {"masks": [*saved["masks"], later], "grade": {"exposure": .7}})
        result = server.undo_mask_batch({"jobId": queue.job_id})
        self.assertEqual(result["count"], 1)
        saved = server.catalog_entry_for(self.name)
        self.assertEqual([m["id"] for m in saved["masks"]], ["manual", "later"])
        self.assertEqual(saved["grade"], {"exposure": .7})
        self.assertTrue(self.catalog.history_for(server.catalog_image_id(self.name)))

    def test_cancel_discards_inflight_result_and_rejects_an_overlapping_batch(self):
        entered, release = threading.Event(), threading.Event()
        queue = server.BatchSemanticMaskQueue()
        def detect(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.payload
        with mock.patch.object(server, "semantic_mask_payload", side_effect=detect):
            try:
                queue.start([self.name], ["subject"])
                self.assertTrue(entered.wait(5))
                ident = queue.job_id
                server.JOBS.cancel(ident)
                self.assertTrue(queue.status()["active"])
                with self.assertRaisesRegex(ValueError, "already running"):
                    queue.start([self.name], ["sky"])
            finally:
                release.set()
                queue.thread.join(5)
        self.assertEqual(server.JOBS.get(ident)["state"], "cancelled")
        self.assertEqual(server.catalog_entry_for(self.name)["masks"], [])
        with mock.patch.object(server, "semantic_mask_payload", return_value=self.payload):
            queue.start([self.name], ["sky"])
            queue.thread.join(5)
        self.assertNotEqual(queue.job_id, ident)
        self.assertEqual(queue.status()["completed"], 1)

    def test_rotation_changed_during_detection_preserves_existing_masks(self):
        def detect(*args, **kwargs):
            server.save_image_state(self.name, {"params": {"rotate": 90}, "masks": [self.manual]})
            return self.payload
        queue = self.run_batch(detect)
        self.assertEqual(queue.status()["failed"], 1)
        self.assertEqual([m["id"] for m in server.catalog_entry_for(self.name)["masks"]], ["manual"])

    def test_mask_limit_does_not_silently_drop_existing_or_generated_masks(self):
        existing = [dict(self.manual, id=str(i)) for i in range(edits.MAX_MASKS)]
        server.save_image_state(self.name, {"masks": existing})
        queue = self.run_batch(lambda *a, **kw: self.payload)
        self.assertEqual((queue.status()["completed"], queue.status()["failed"]), (0, 1))
        self.assertEqual(server.catalog_entry_for(self.name)["masks"], existing)

    def test_partial_category_failure_is_explicit_and_successful_mask_is_retained(self):
        def detect(name, category, **kwargs):
            if category == "sky":
                raise ValueError("No sky found")
            return self.payload
        queue = self.run_batch(detect, ["subject", "sky"])
        self.assertEqual((queue.status()["completed"], queue.status()["failed"], queue.status()["masked"]), (0, 1, 1))
        self.assertEqual(len(server.catalog_entry_for(self.name)["masks"]), 1)
        self.assertEqual(queue.status()["results"][0]["errors"][0]["category"], "sky")

    def test_batch_undo_survives_a_restart_and_photo_rename(self):
        queue = self.run_batch(lambda *a, **kw: self.payload)
        result = server.rename_photos({'names': [self.name], 'template': 'Renamed'})
        self.assertEqual(result['renamed'], 1)
        # Reopen the connection and lose the in-memory jobs, as on restart.
        self.catalog.close()
        with mock.patch.object(server, 'JOBS', JobRegistry()):
            undone = server.undo_mask_batch({'jobId': queue.job_id})
        self.assertEqual(undone['count'], 1)
        self.assertEqual(server.catalog_entry_for(self.qualified('Renamed.jpg'))['masks'], [])
