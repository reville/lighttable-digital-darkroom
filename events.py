"""Bounded in-process event fanout for LightTable's local API.

The browser consumes these records over Server-Sent Events.  Publishers never
block on a slow window: when a subscriber falls behind it receives a resync
marker and is expected to refetch authoritative state.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field


@dataclass(eq=False)
class Subscriber:
    client: str
    events: queue.Queue[dict] = field(default_factory=lambda: queue.Queue(128))


class EventBroker:
    def __init__(self, *, maximum_subscribers: int = 8,
                 queue_size: int = 128) -> None:
        self.maximum_subscribers = maximum_subscribers
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: set[Subscriber] = set()
        self._sequence = 0

    def subscribe(self, client: str = "") -> Subscriber:
        with self._lock:
            if len(self._subscribers) >= self.maximum_subscribers:
                raise RuntimeError("too many event subscribers")
            subscriber = Subscriber(
                str(client)[:80], queue.Queue(self.queue_size))
            self._subscribers.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event_type: str, payload: dict | None = None) -> dict:
        with self._lock:
            self._sequence += 1
            record = {
                "id": self._sequence,
                "type": str(event_type),
                "time": time.time(),
                **(dict(payload) if payload else {}),
            }
            # Keep publication order and overflow recovery under one lock.
            # Otherwise another producer can overtake this record or fill the
            # emptied queue before its resync marker is inserted. Queue calls
            # remain nonblocking, so a slow window never holds up producers.
            for subscriber in self._subscribers:
                try:
                    subscriber.events.put_nowait(record)
                except queue.Full:
                    # A slow or suspended web view does not get an arbitrarily
                    # old replay. Drop its backlog and tell it to refetch once.
                    while True:
                        try:
                            subscriber.events.get_nowait()
                        except queue.Empty:
                            break
                    subscriber.events.put_nowait({
                        "id": record["id"], "type": "resync",
                        "time": record["time"], "reason": "subscriber-overflow",
                    })
        return record

    def get(self, subscriber: Subscriber, timeout: float = 15.0) -> dict | None:
        try:
            return subscriber.events.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def window_connected(self) -> bool:
        with self._lock:
            return any(subscriber.client for subscriber in self._subscribers)


def encode_sse(record: dict) -> bytes:
    """Encode one record without allowing payload newlines to break framing."""
    event_type = str(record.get("type", "message")).replace("\n", "")
    ident = str(record.get("id", "")).replace("\n", "")
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    return f"id: {ident}\nevent: {event_type}\ndata: {payload}\n\n".encode()
