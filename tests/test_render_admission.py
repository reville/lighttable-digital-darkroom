# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest import mock

import server
from render_scheduling import LatestWorkQueue, PriorityGate


class ResidentAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.gate = PriorityGate(reentrant=True)
        self.stack.enter_context(mock.patch.object(server, "RENDER_LOCK", self.gate))
        self.stack.enter_context(mock.patch.object(server, "RENDER_CONTEXT", threading.local()))
        self.stack.enter_context(mock.patch.object(server, "LATEST_GENERATION", {}))
        self.stack.enter_context(mock.patch.object(server, "guard_local_photo"))
        self.client = server.RustEngineClient(None)

    def test_progress_reports_worker_stages_and_drops_stale_generations(self):
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        with (
            mock.patch.object(self.client, "_start", return_value=process),
            mock.patch.object(self.client, "_readline", return_value='{"ok":true}'),
            mock.patch.object(server, "_render_preview", side_effect=lambda *args: self.client.render({})),
            mock.patch.object(server.EVENTS, "publish") as publish,
        ):
            server.render_preview("photo.jpg", {}, 1100, "rs", "window", 1)
            self.assertEqual([call.args[1]["completed"] for call in publish.call_args_list], [1, 2, 3])
            self.assertTrue(all(call.args[0] == "preview.progress" and
                call.args[1]["generation"] == 1 and call.args[1]["client"] == "window"
                for call in publish.call_args_list))
            publish.reset_mock()
            server.LATEST_GENERATION["window"] = 2
            server.publish_preview_progress("window", 1, "photo.jpg", 4)
            server.publish_preview_progress("window", "invalid", "photo.jpg", 4)
            publish.assert_not_called()

    def wait_for_waiters(self, count):
        with self.gate._condition:
            self.assertTrue(self.gate._condition.wait_for(
                lambda: len(self.gate._waiters) == count, timeout=2))

    def test_stale_preview_never_starts_worker_after_waiting_for_export(self):
        self.gate.acquire(priority="export")
        with (
            mock.patch.object(self.client, "_start") as start,
            mock.patch.object(server, "_render_preview", side_effect=lambda *args: self.client.render({})),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(server.render_preview, "photo.jpg", {}, 1100,
                                 "rs", "window", 1)
            try:
                self.wait_for_waiters(1)
                with server.GENERATION_LOCK:
                    server.LATEST_GENERATION["window"] = 2
                result = future.result(timeout=2)
            finally:
                self.gate.release()
        self.assertTrue(result["cancelled"])
        start.assert_not_called()

    def test_preview_context_prioritizes_worker_dispatch_over_another_export(self):
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        active = threading.Event()
        release = threading.Event()
        order = []

        def reply(*args):
            payload = json.loads(process.stdin.getvalue().splitlines()[-1])
            if payload["label"] == "active":
                active.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release engine")
            order.append(payload["label"])
            return json.dumps({"ok": True})

        with (
            mock.patch.object(self.client, "_start", return_value=process),
            mock.patch.object(self.client, "_readline", side_effect=reply),
            mock.patch.object(server, "_render_preview", side_effect=lambda *args: self.client.render({"label": "preview"})),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            first = pool.submit(self.client.render, {"label": "active"})
            try:
                self.assertTrue(active.wait(2))
                export = pool.submit(self.client.render, {"label": "export"})
                self.wait_for_waiters(1)
                preview = pool.submit(server.render_preview, "photo.jpg", {}, 1100,
                                      "rs", "window", 1)
                self.wait_for_waiters(2)
            finally:
                release.set()
            first.result(timeout=2)
            export.result(timeout=2)
            preview.result(timeout=2)
        self.assertEqual(order, ["active", "preview", "export"])

    def test_stale_refinement_does_not_decode_or_retain_catalog_connection(self):
        with server.GENERATION_LOCK:
            server.LATEST_GENERATION["window"] = 3
        decode = mock.Mock()
        server._run_refinement(decode, (), "window", 2)
        decode.assert_not_called()
        self.assertFalse(self.gate.locked())

    def test_new_generation_replaces_same_photo_pending_refinement(self):
        queue = LatestWorkQueue()
        started, release = threading.Event(), threading.Event()

        def active():
            started.set()
            if not release.wait(3):
                raise TimeoutError("refinement test was not released")

        queue.submit("active", active)
        self.assertTrue(started.wait(2))
        missing = mock.Mock()
        missing.exists.return_value = False
        try:
            with (
                mock.patch.object(server, "RAW_REFINE_POOL", queue),
                mock.patch.object(server, "RAW_REFINE_JOBS", {}),
                mock.patch.object(server, "file_key", return_value="source"),
                mock.patch.object(server, "raw_preview_path", return_value=missing),
                mock.patch.object(server, "catalog_handle", return_value=None),
                mock.patch.object(server, "build_raw_preview") as decode,
            ):
                server.LATEST_GENERATION["window"] = 1
                self.assertTrue(server.schedule_raw_refinement(
                    "photo.dng", 1100, {}, client="window", generation=1))
                obsolete = next(iter(server.RAW_REFINE_JOBS.values()))
                server.LATEST_GENERATION["window"] = 2
                self.assertTrue(server.schedule_raw_refinement(
                    "photo.dng", 1100, {}, client="window", generation=2))
                self.assertTrue(obsolete.cancelled())
                latest = next(iter(server.RAW_REFINE_JOBS.values()))
                release.set()
                latest.result(timeout=2)
                decode.assert_called_once_with("photo.dng", 1100, "full", {})
        finally:
            release.set()
            queue.shutdown()


class PreviewWidthTests(unittest.TestCase):
    def test_requested_widths_are_bounded_and_canonical(self):
        self.assertEqual(server.preview_width(40000), 8000)
        self.assertEqual(server.preview_width(10), 64)
        self.assertEqual(server.preview_width("nonsense"), 1100)
        self.assertEqual(server.preview_width(None), 1100)
        self.assertEqual(server.preview_width(1600), 1600)

    def test_cache_paths_use_the_bounded_width(self):
        with mock.patch.object(server, "file_key", return_value="key"), \
                mock.patch.object(server, "is_raw", return_value=True):
            paths = (
                server.raw_preview_path("frame.dng", 40000, "full", {}),
                server.neutral_preview_path("frame.dng", 40000, 0, {}),
            )
        for path in paths:
            self.assertIn("_8000_", path.name)


if __name__ == "__main__":
    unittest.main()
