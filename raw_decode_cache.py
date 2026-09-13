# SPDX-License-Identifier: GPL-3.0-only
"""A byte-bounded, process-wide cache of immutable demosaiced sensor pixels.

Both resident engines enter through Python, so this cache shares captures
between preview sizes, 1:1 viewing, export and downstream white balance.
Half-size demosaics remain distinct: shrinking a full decode is not equivalent.
"""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from pathlib import Path
import threading


def source_identity(path):
    try:
        source = Path(path).resolve()
        info = source.stat()
        return (str(source), info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        # Let the decoder report missing/unsupported inputs itself. Synthetic
        # inputs used by callers and tests must not share a fictitious file.
        return None


class DecodedRawCache:
    def __init__(self, max_bytes):
        self.max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._pending = {}
        self._waiters = {}
        self._bytes = 0
        self._generation = 0
        self._hits = self._misses = self._waits = 0

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self._generation += 1
            self._hits = self._misses = self._waits = 0

    def stats(self):
        with self._lock:
            return dict(bytes=self._bytes, entries=len(self._entries),
                        hits=self._hits, misses=self._misses, waits=self._waits,
                        budget=self.max_bytes)

    def waiting(self, key):
        with self._lock:
            return self._waiters.get(key, 0) > 0

    def get_or_build(self, key, build, check_cancel, *, retain=True):
        from raw_decode_runtime import RawDecodeCancelled, retained

        check_cancel()
        if key is None or not self.max_bytes:
            return build()
        # If another request owns this decode, wait without holding the cache
        # lock. Its cancellation must not cancel an independent live consumer,
        # and a live waiter keeps a superseded owner's decode running (adopts
        # it) instead of restarting the same demosaic afterwards.
        while True:
            check_cancel()
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    self._hits += 1
                    return cached
                future = self._pending.get(key)
                owner = future is None
                if owner:
                    future = self._pending[key] = Future()
                    generation = self._generation
                    self._misses += 1
                else:
                    self._waits += 1
                    self._waiters[key] = self._waiters.get(key, 0) + 1
            if owner:
                break
            try:
                while True:
                    check_cancel()
                    try:
                        return future.result(timeout=0.05)
                    except TimeoutError:
                        if future.done():
                            raise
            except RawDecodeCancelled:
                check_cancel()
                continue
            finally:
                with self._lock:
                    remaining = self._waiters.get(key, 0) - 1
                    if remaining > 0:
                        self._waiters[key] = remaining
                    else:
                        self._waiters.pop(key, None)
        try:
            with retained(lambda: self.waiting(key)):
                pixels = build()
            pixels.flags.writeable = False
            with self._lock:
                # Oversized captures can still complete, but do not displace
                # the working set or permanently exceed the configured budget.
                if (retain and generation == self._generation
                        and pixels.nbytes <= self.max_bytes):
                    while self._entries and self._bytes + pixels.nbytes > self.max_bytes:
                        _, old = self._entries.popitem(last=False)
                        self._bytes -= old.nbytes
                    self._entries[key] = pixels
                    self._bytes += pixels.nbytes
                self._pending.pop(key, None)
            future.set_result(pixels)
        except BaseException as error:
            with self._lock:
                self._pending.pop(key, None)
            future.set_exception(error)
            raise
        # Complete pixels are published to every waiter before a superseded
        # owner learns that its own request is obsolete.
        check_cancel()
        return pixels
