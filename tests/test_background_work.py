# SPDX-License-Identifier: GPL-3.0-only
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock

from events import EventBroker
from jobs import JobRegistry


class ConcurrentEventTests(unittest.TestCase):
    def overlapping_publish(self, *, overflow):
        broker = EventBroker(queue_size=1 if overflow else 2)
        subscriber = broker.subscribe("window")
        if overflow:
            broker.publish("state", {"value": 0})
        first_id = 2 if overflow else 1
        paused = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        enqueue = subscriber.events.put_nowait

        def pause_first(record):
            if record["id"] == first_id and (
                    not overflow or record["type"] == "resync"):
                paused.set()
                if not release.wait(5):
                    raise TimeoutError("first publisher was not released")
            enqueue(record)

        def publish_second():
            second_started.set()
            try:
                return broker.publish("state", {"value": 2})
            finally:
                second_finished.set()

        with mock.patch.object(subscriber.events, "put_nowait", pause_first):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(broker.publish, "state", {"value": 1})
                try:
                    self.assertTrue(paused.wait(5), "first publisher did not pause")
                    second = workers.submit(publish_second)
                    self.assertTrue(second_started.wait(5))
                    # Let an unserialized publisher overtake the paused one.
                    # Serialized publishers wait until the release below.
                    second_finished.wait(1)
                finally:
                    release.set()
                first.result(timeout=5)
                second.result(timeout=5)
        return broker, subscriber

    def test_concurrent_events_reach_the_window_in_publish_order(self):
        broker, subscriber = self.overlapping_publish(overflow=False)
        records = [broker.get(subscriber, timeout=0) for _ in range(2)]
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual([r["value"] for r in records], [1, 2])

    def test_concurrent_overflow_never_raises_or_loses_the_resync_marker(self):
        broker, subscriber = self.overlapping_publish(overflow=True)
        record = broker.get(subscriber, timeout=0)
        self.assertEqual((record["type"], record["id"]), ("resync", 3))
        self.assertIsNone(broker.get(subscriber, timeout=0))


class ActiveJobRetentionTests(unittest.TestCase):
    def test_history_pruning_keeps_active_jobs_observable_and_cancellable(self):
        cancelled = []
        registry = JobRegistry(maximum=2)
        active = registry.create("export", state="running",
                                 cancel=lambda: cancelled.append(True))
        old = registry.create("scan", state="done")
        newest = registry.create("scan", state="done")

        self.assertIsNotNone(registry.get(active["id"]))
        self.assertIsNone(registry.get(old["id"]))
        self.assertIsNotNone(registry.get(newest["id"]))
        self.assertEqual(len(registry.list()), 2)
        self.assertEqual(registry.cancel(active["id"])["state"], "cancelled")
        self.assertEqual(cancelled, [True])

    def test_active_overflow_is_pruned_when_work_finishes(self):
        registry = JobRegistry(maximum=2)
        first = registry.create("export", state="running")
        second = registry.create("scan", state="running")
        queued = registry.create("merge", state="queued")
        self.assertEqual(len(registry.list()), 3)

        finished = registry.update(first["id"], state="done")

        self.assertEqual(finished["state"], "done")
        self.assertEqual({r["id"] for r in registry.list()},
                         {second["id"], queued["id"]})
        self.assertEqual(registry.update(queued["id"], state="running")["state"],
                         "running")


if __name__ == "__main__":
    unittest.main()
