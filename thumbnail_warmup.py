# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, disposable source-thumbnail work; never joins the catalog scan."""

from __future__ import annotations

from collections import deque
import ctypes
import sys
import threading
import time
from typing import Callable


class ThumbnailWarmup:
    """One background decoder with a bounded queue and cooperative cancellation.

    The on-demand thumbnail route remains authoritative. Overflow retains a
    catalog cursor per source instead of an unbounded list of paths. An idle
    worker exits instead of retaining a thread.
    Individual decodes cannot be interrupted; cancellation discards queued work.
    """

    def __init__(self, build: Callable[[str], object], *,
                 busy: Callable[[], bool] = lambda: False,
                 refill: Callable[[int, int, int], list[tuple[int, str]]] | None = None,
                 capacity: int = 256, interval: float = 0.05,
                 lifetime: float = 1800):
        self._build = build
        self._busy = busy
        self._refill = refill
        self._overflow: dict[int, int] = {}
        self._capacity = max(1, int(capacity))
        self._interval = max(0.001, float(interval))
        self._lifetime = max(0.001, float(lifetime))
        self._queue: deque[str] = deque()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue(self, source_id: int, relpath: str) -> bool:
        name = f"{source_id}:{relpath}"
        with self._lock:
            if self._stop.is_set() or name in self._pending:
                return False
            if len(self._pending) >= self._capacity:
                if self._refill:
                    self._overflow[source_id] = 0
                return False
            self._queue.append(name)
            self._pending.add(name)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="lighttable-thumbnail-warmup",
                    daemon=True)
                self._thread.start()
        return True

    def cancel(self) -> None:
        """Permanently stop accepting work and discard queued thumbnails."""
        self._stop.set()
        with self._lock:
            self._queue.clear()
            self._pending.clear()
            self._overflow.clear()

    def _refill_queue(self) -> None:
        with self._lock:
            if self._queue or not self._overflow or self._stop.is_set():
                return
            source, cursor = next(iter(self._overflow.items()))
        try:
            rows = self._refill(source, cursor, self._capacity)
        except Exception:
            rows = []  # Removed/unavailable catalogs cannot strand a worker.
        with self._lock:
            if self._stop.is_set() or self._overflow.get(source) != cursor:
                return
            if not rows:
                self._overflow.pop(source, None)
                return
            for ident, relpath in rows:
                name = f"{source}:{relpath}"
                if name not in self._pending:
                    if len(self._pending) >= self._capacity:
                        break
                    self._queue.append(name)
                    self._pending.add(name)
                self._overflow[source] = ident

    def _run(self) -> None:
        if sys.platform == "darwin":
            try:
                # Apply QoS to this thread only; os.nice would demote the UI too.
                set_qos = ctypes.CDLL(None).pthread_set_qos_class_self_np
                set_qos.argtypes = [ctypes.c_uint, ctypes.c_int]
                set_qos.restype = ctypes.c_int
                set_qos(0x09, 0)  # QOS_CLASS_BACKGROUND
            except (AttributeError, OSError):
                pass
        deadline = time.monotonic() + self._lifetime
        while time.monotonic() < deadline and not self._stop.wait(self._interval):
            if self._busy():
                continue
            self._refill_queue()
            with self._lock:
                if not self._queue:
                    if self._overflow:
                        continue
                    self._thread = None
                    return
                name = self._queue.popleft()
            retry = False
            try:
                retry = self._build(name) is False
            except Exception:
                # Unavailable/corrupt sources must not stop grid fill; an
                # explicit request will report its normal source error later.
                pass
            finally:
                with self._lock:
                    if retry and not self._stop.is_set():
                        self._queue.appendleft(name)
                    else:
                        self._pending.discard(name)
        with self._lock:
            self._queue.clear()
            self._pending.clear()
            self._overflow.clear()
            self._thread = None
