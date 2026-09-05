from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from render_scheduling import LatestWorkQueue, PriorityGate


class PriorityGateTests(unittest.TestCase):
    def wait_for_waiters(self, gate, count):
        with gate._condition:
            self.assertTrue(gate._condition.wait_for(
                lambda: len(gate._waiters) == count, timeout=2))

    def test_interactive_overtakes_queued_export_and_prefetch(self):
        gate = PriorityGate()
        order = []
        gate.acquire()

        def enter(priority):
            gate.acquire(priority=priority)
            try:
                order.append(priority)
            finally:
                gate.release()

        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = []
            try:
                for count, priority in enumerate(("prefetch", "export", "interactive"), 1):
                    pending.append(pool.submit(enter, priority))
                    self.wait_for_waiters(gate, count)
                self.assertFalse(gate.acquire(blocking=False, priority="prefetch"))
            finally:
                gate.release()
            for future in pending:
                future.result(timeout=2)
        self.assertEqual(order, ["interactive", "export", "prefetch"])

    def test_superseded_request_never_enters_renderer(self):
        gate = PriorityGate()
        stale = threading.Event()
        gate.acquire()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(gate.acquire, cancelled=stale.is_set)
            try:
                self.wait_for_waiters(gate, 1)
                stale.set()
                self.assertFalse(future.result(timeout=2))
                self.assertTrue(gate.locked())
            finally:
                gate.release()

    def test_reentrant_preview_to_engine_handoff_keeps_exclusive_ownership(self):
        gate = PriorityGate(reentrant=True)
        self.assertTrue(gate.acquire())
        self.assertTrue(gate.acquire(priority="interactive"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertFalse(pool.submit(gate.acquire, False).result(timeout=2))
            gate.release()
            self.assertTrue(gate.locked())
            self.assertFalse(pool.submit(gate.acquire, False).result(timeout=2))
        gate.release()
        self.assertFalse(gate.locked())


class LatestWorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = LatestWorkQueue(max_pending=2)
        self.started = threading.Event()
        self.release = threading.Event()

        def current():
            self.started.set()
            if not self.release.wait(3):
                raise TimeoutError("test did not release active refinement")

        self.active = self.queue.submit("active", current)
        self.assertTrue(self.started.wait(2))

    def tearDown(self):
        self.release.set()
        self.queue.shutdown()

    def test_latest_photo_replaces_pending_refinement_for_one_window(self):
        executed = []
        obsolete = self.queue.submit("window-a", executed.append, "old-photo")
        other = self.queue.submit("window-b", executed.append, "other-window")
        latest = self.queue.submit("window-a", executed.append, "latest-photo")
        self.assertTrue(obsolete.cancelled())
        self.release.set()
        other.result(timeout=2)
        latest.result(timeout=2)
        self.assertEqual(executed, ["other-window", "latest-photo"])

    def test_pending_budget_discards_oldest_speculative_work(self):
        executed = []
        oldest = self.queue.submit("first", executed.append, "oldest")
        second = self.queue.submit("second", executed.append, "second")
        newest = self.queue.submit("third", executed.append, "newest")
        self.assertTrue(oldest.cancelled())
        self.release.set()
        second.result(timeout=2)
        newest.result(timeout=2)
        self.assertEqual(executed, ["second", "newest"])

    def test_generation_is_rechecked_after_waiting_for_worker(self):
        stale = threading.Event()
        executed = []
        future = self.queue.submit(
            "window", executed.append, "stale", cancelled=stale.is_set)
        stale.set()
        self.release.set()
        self.active.result(timeout=2)
        # A sentinel supplies a deterministic worker-completion boundary.
        self.queue.submit("sentinel", lambda: None).result(timeout=2)
        self.assertTrue(future.cancelled())
        self.assertEqual(executed, [])

    def test_cancel_callbacks_can_submit_without_deadlocking(self):
        first = self.queue.submit("window", lambda: None)
        callbacks = []
        first.add_done_callback(lambda _: callbacks.append(
            self.queue.submit("callback", lambda: "done")))
        latest = self.queue.submit("window", lambda: "latest")
        self.release.set()
        self.assertEqual(latest.result(timeout=2), "latest")
        self.assertEqual(callbacks[0].result(timeout=2), "done")


if __name__ == "__main__":
    unittest.main()
