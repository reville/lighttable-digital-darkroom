"""Tests for Preview Pre-generation and Batch Semantic Mask queues and endpoints."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server


class BatchOperationsTestCase(unittest.TestCase):
    def test_pregen_queue_start_status_cancel(self):
        queue = server.PreviewPregenQueue()
        self.assertFalse(queue.status()["active"])

        # Test start
        names = ["photo1.jpg", "photo2.jpg", "photo3.jpg"]
        with mock.patch.object(queue, "_worker"):
            queued = queue.start(names, 3840)
            self.assertEqual(queued, 3)
            status = queue.status()
            self.assertTrue(status["active"])
            self.assertEqual(status["total"], 3)

        # Test cancel
        queue.cancel()
        status = queue.status()
        self.assertFalse(status["active"])
        self.assertEqual(len(queue.queue), 0)

    def test_batch_semantic_mask_queue_start_status_cancel(self):
        queue = server.BatchSemanticMaskQueue()
        self.assertFalse(queue.status()["active"])

        names = ["portrait.jpg", "landscape.jpg"]
        categories = ["subject", "sky"]
        with mock.patch.object(queue, "_worker"):
            queued = queue.start(names, categories)
            self.assertEqual(queued, 2)
            status = queue.status()
            self.assertTrue(status["active"])
            self.assertEqual(status["total"], 2)

        queue.cancel()
        status = queue.status()
        self.assertFalse(status["active"])

    def test_batch_endpoints(self):
        handler = server.Handler.__new__(server.Handler)
        responses = []
        handler._json = lambda data, code=200: responses.append((data, code))

        # 1. GET /api/cache/pregenerate/status
        handler.path = "/api/cache/pregenerate/status"
        handler.do_GET()
        res = responses[-1][0]
        self.assertIn("active", res)
        self.assertIn("total", res)

        # 2. GET /api/batch/semantic-masks/status
        handler.path = "/api/batch/semantic-masks/status"
        handler.do_GET()
        res = responses[-1][0]
        self.assertIn("active", res)
        self.assertIn("total", res)

        # 3. POST /api/cache/pregenerate
        handler.path = "/api/cache/pregenerate"
        handler.headers = {"content-type": "application/json"}
        with mock.patch.object(server.PREGEN_QUEUE, "start", return_value=5):
            with mock.patch.object(handler, "_body", return_value={"names": ["a", "b"]}):
                handler.do_POST()
                res = responses[-1][0]
                self.assertTrue(res["ok"])
                self.assertEqual(res["queued"], 5)

        # 4. POST /api/cache/pregenerate/cancel
        handler.path = "/api/cache/pregenerate/cancel"
        with mock.patch.object(handler, "_body", return_value={}):
            handler.do_POST()
            res = responses[-1][0]
            self.assertTrue(res["ok"])

        # 5. POST /api/batch/semantic-masks
        handler.path = "/api/batch/semantic-masks"
        with mock.patch.object(server.BATCH_MASK_QUEUE, "start", return_value=3):
            with mock.patch.object(handler, "_body", return_value={"names": ["a"]}):
                handler.do_POST()
                res = responses[-1][0]
                self.assertTrue(res["ok"])
                self.assertEqual(res["queued"], 3)


if __name__ == "__main__":
    unittest.main()
