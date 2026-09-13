# SPDX-License-Identifier: GPL-3.0-only
"""The learned-denoise preload compiles at idle, once, and never blocks work."""
import os
import threading
import unittest
from unittest import mock

import numpy as np

import enhance_workflow
import server


def reset_preload():
    with server.DENOISE_PRELOAD_LOCK:
        server.DENOISE_PRELOAD.update(state="idle", reason="", seconds=0.0)
    server.DENOISE_PRELOAD_CANCEL.clear()


class PreloadDenoiseTests(unittest.TestCase):
    def test_preload_runs_one_blank_tile_through_the_batch_helper(self):
        seen = {}

        def runner(tiles, mode, params, *, status=None, cancel=None):
            seen.update(count=len(tiles), mode=mode, params=params, cancel=cancel)
            return [np.asarray(tile, dtype=np.float32) for tile in tiles]

        cancel = threading.Event()
        with mock.patch.object(enhance_workflow, "capabilities", return_value={
                "modes": {"denoise": True, "upscale": False}, "reason": ""}), \
                mock.patch.object(enhance_workflow, "helper_batch_runner", side_effect=runner):
            result = enhance_workflow.preload_denoise(cancel=cancel)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["available"])
        self.assertEqual(seen["count"], 1)
        self.assertEqual(seen["mode"], "denoise")
        self.assertEqual(seen["params"]["tile"], enhance_workflow.DEFAULT_TILE)
        self.assertIs(seen["cancel"], cancel)

    def test_preload_reports_unavailable_without_a_model(self):
        with mock.patch.object(enhance_workflow, "capabilities", return_value={
                "modes": {"denoise": False, "upscale": False}, "reason": "no model"}), \
                mock.patch.object(enhance_workflow, "helper_batch_runner") as runner:
            result = enhance_workflow.preload_denoise()
        runner.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "no model")

    def test_preload_reports_helper_failures_instead_of_raising(self):
        with mock.patch.object(enhance_workflow, "capabilities", return_value={
                "modes": {"denoise": True, "upscale": False}, "reason": ""}), \
                mock.patch.object(enhance_workflow, "helper_batch_runner",
                                  side_effect=RuntimeError("denoise cancelled")):
            result = enhance_workflow.preload_denoise()
        self.assertEqual(result, {"ok": False, "available": True,
                                  "error": "denoise cancelled",
                                  "seconds": result["seconds"]})


class ServerPreloadTests(unittest.TestCase):
    def setUp(self):
        reset_preload()
        self.addCleanup(reset_preload)

    def capable(self):
        return mock.patch.object(enhance_workflow, "capabilities", return_value={
            "modes": {"denoise": True, "upscale": False}, "reason": ""})

    def test_scheduled_once_and_only_on_macos_outside_safe_mode(self):
        with mock.patch.object(server.threading, "Thread") as thread:
            with mock.patch.object(server.sys, "platform", "linux"):
                self.assertFalse(server.schedule_denoise_preload())
            with mock.patch.object(server.sys, "platform", "darwin"), \
                    mock.patch.object(server, "SAFE_MODE", True):
                self.assertFalse(server.schedule_denoise_preload())
            with mock.patch.object(server.sys, "platform", "darwin"), \
                    mock.patch.dict(os.environ, {"LIGHTTABLE_DENOISE_PRELOAD": "0"}):
                self.assertFalse(server.schedule_denoise_preload())
            thread.assert_not_called()
            with mock.patch.object(server.sys, "platform", "darwin"), \
                    mock.patch.object(server, "SAFE_MODE", False), \
                    mock.patch.dict(os.environ, {"LIGHTTABLE_DENOISE_PRELOAD": "1"}):
                self.assertTrue(server.schedule_denoise_preload())
                self.assertFalse(server.schedule_denoise_preload())
            self.assertEqual(thread.call_count, 1)
            thread.return_value.start.assert_called_once()
        self.assertEqual(server.denoise_preload_status()["state"], "scheduled")

    def test_worker_waits_for_idle_then_runs_in_the_denoise_pool(self):
        busy_answers = iter([True, True, False])
        calls = []

        def preload(*, cancel):
            calls.append(threading.current_thread().name)
            return {"ok": True, "available": True, "seconds": 0.1, "error": ""}

        with self.capable():
            result = server._denoise_preload_worker(
                busy=lambda: next(busy_answers), preload=preload,
                wait_seconds=5, poll=0.01, settle=0.0)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.current_thread().name)

    def test_worker_skips_when_the_app_never_goes_idle(self):
        with self.capable(), mock.patch.object(server, "DENOISE_POOL") as pool:
            result = server._denoise_preload_worker(
                busy=lambda: True, wait_seconds=0.05, poll=0.01, settle=0.0)
        pool.submit.assert_not_called()
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(result["reason"], "never idle")

    def test_worker_reports_unavailable_without_touching_the_helper(self):
        with mock.patch.object(enhance_workflow, "capabilities", return_value={
                "modes": {"denoise": False}, "reason": "no model"}), \
                mock.patch.object(server, "DENOISE_POOL") as pool:
            result = server._denoise_preload_worker(busy=lambda: False, settle=0.0)
        pool.submit.assert_not_called()
        self.assertEqual((result["state"], result["reason"]), ("unavailable", "no model"))

    def test_cancel_stops_a_waiting_preload_before_the_helper_runs(self):
        with server.DENOISE_PRELOAD_LOCK:
            server.DENOISE_PRELOAD["state"] = "scheduled"
        self.assertTrue(server.cancel_denoise_preload(only_waiting=True))
        with self.capable(), mock.patch.object(server, "DENOISE_POOL") as pool:
            result = server._denoise_preload_worker(busy=lambda: False, settle=0.0)
        pool.submit.assert_not_called()
        self.assertEqual(result["state"], "cancelled")

    def test_cancel_leaves_a_running_compile_alone_when_only_waiting(self):
        with server.DENOISE_PRELOAD_LOCK:
            server.DENOISE_PRELOAD["state"] = "running"
        self.assertFalse(server.cancel_denoise_preload(only_waiting=True))
        self.assertFalse(server.DENOISE_PRELOAD_CANCEL.is_set())
        self.assertTrue(server.cancel_denoise_preload())
        self.assertTrue(server.DENOISE_PRELOAD_CANCEL.is_set())
        with server.DENOISE_PRELOAD_LOCK:
            server.DENOISE_PRELOAD["state"] = "done"
        server.DENOISE_PRELOAD_CANCEL.clear()
        self.assertFalse(server.cancel_denoise_preload())

    def test_worker_failures_never_raise(self):
        with self.capable(), mock.patch.object(
                server.DENOISE_POOL, "submit", side_effect=RuntimeError("pool closed")):
            result = server._denoise_preload_worker(busy=lambda: False, settle=0.0)
        self.assertEqual((result["state"], result["reason"]), ("failed", "pool closed"))


class ExportWorkerCountTests(unittest.TestCase):
    def test_export_workers_scale_with_cores_within_bounds(self):
        for cores, expected in ((2, 2), (8, 2), (12, 3), (16, 4), (64, 4), (None, 2)):
            with mock.patch.dict(os.environ, {"LIGHTTABLE_EXPORT_WORKERS": ""}), \
                    mock.patch.object(server.os, "cpu_count", return_value=cores):
                self.assertEqual(server.export_worker_count(), expected, cores)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_EXPORT_WORKERS": "6"}):
            self.assertEqual(server.export_worker_count(), 6)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_EXPORT_WORKERS": "0"}):
            self.assertEqual(server.export_worker_count(), 1)


if __name__ == "__main__":
    unittest.main()
