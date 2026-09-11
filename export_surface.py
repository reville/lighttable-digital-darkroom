# SPDX-License-Identifier: GPL-3.0-only
"""Read-only float32 export transport and bounded reuse of unbaked film frames.

The ndarray owns its mmap through ``array.base``. Unlinking immediately after
opening avoids named shared-memory leaks; eviction releases only the cache's
reference, so an export already using the array remains valid.
"""
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
import ctypes
import mmap
import os
import re
import threading

import numpy as np

from render_scheduling import RenderCancelled

MAX_SURFACE_BYTES = 1024 * 1024 * 1024


def supported() -> bool:
    return os.name == "posix"


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    # shm_open's mode is variadic; ARM64 macOS passes it on the stack.
    libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.shm_open.restype = ctypes.c_int
    libc.shm_unlink.argtypes = [ctypes.c_char_p]
    libc.shm_unlink.restype = ctypes.c_int
    return libc


def discard_surface(descriptor: dict) -> None:
    """Release a worker result that will not be adopted, e.g. after cancellation."""
    name = str(descriptor.get("name", ""))
    if supported() and re.fullmatch(r"/lte-[0-9a-f]+-[0-9a-f]+", name):
        _libc().shm_unlink(name.encode("ascii"))


def adopt_surface(descriptor: dict) -> np.ndarray:
    """Adopt an immutable full-precision render, retaining all original f32 bits."""
    if not supported():
        raise OSError("shared export transport is unavailable on this platform")
    name = str(descriptor.get("name", ""))
    if not re.fullmatch(r"/lte-[0-9a-f]+-[0-9a-f]+", name):
        raise ValueError("invalid shared export name")
    libc = _libc()
    encoded = name.encode("ascii")
    fd = -1
    mapping = None
    try:
        width, height = int(descriptor["width"]), int(descriptor["height"])
        row, length = int(descriptor["rowBytes"]), int(descriptor["length"])
        if (width <= 0 or height <= 0 or row != width * 12
                or length != row * height or length > MAX_SURFACE_BYTES
                or descriptor.get("offset", 0) != 0
                or descriptor.get("format") != "rgb32f"
                or descriptor.get("byteOrder") != "native"):
            raise ValueError("invalid shared export layout")
        fd = libc.shm_open(encoded, os.O_RDONLY, 0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "shared export unavailable")
        # macOS rounds the allocation's reported size up to a page boundary.
        if os.fstat(fd).st_size < length:
            raise ValueError("shared export length exceeds allocation")
        mapping = mmap.mmap(fd, length, access=mmap.ACCESS_READ)
        array = np.ndarray((height, width, 3), dtype=np.float32, buffer=mapping)
        array.flags.writeable = False
        return array
    except Exception:
        if mapping is not None:
            mapping.close()
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        # Only accept our worker's namespace, including for malformed layouts.
        libc.shm_unlink(encoded)


@contextmanager
def open_surface(descriptor: dict):
    """Adopt a surface for a scope; retained arrays may safely outlive the scope."""
    pixels = adopt_surface(descriptor)
    try:
        yield pixels
    finally:
        # ndarray.base keeps the mapping alive if a caller has cached the array.
        del pixels


class SharedFilmCache:
    """LRU bounded by pixel bytes and entries; active exports retain evicted frames."""

    def __init__(self, max_bytes: int = 512 * 1024 * 1024, max_entries: int = 2,
                 *, on_evict=None, on_clear=None):
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(0, int(max_entries))
        self._frames = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._builds = {}
        self._generation = 0
        self._on_evict = on_evict
        self._on_clear = on_clear
        # Disk dispatch never holds the data lock. This separate lock makes
        # dispatch and clear atomic relative to one another, including a
        # callback blocked waiting for the bounded writer's active frame.
        self._dispatch_lock = threading.RLock()

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    def get(self, key: str) -> np.ndarray | None:
        with self._lock:
            pixels = self._frames.get(key)
            if pixels is not None:
                self._frames.move_to_end(key)
            return pixels

    def put(self, key: str, pixels: np.ndarray) -> None:
        self._put(key, pixels)

    def _put(self, key: str, pixels: np.ndarray, generation=None) -> None:
        if pixels.dtype != np.float32 or pixels.ndim != 3 or pixels.shape[2] != 3:
            raise ValueError("film cache requires HxWx3 float32 pixels")
        # Writable callers must give the cache independent immutable storage.
        if pixels.flags.writeable:
            pixels = pixels.copy()
            pixels.flags.writeable = False
        evicted = []
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            generation = self._generation
            previous = self._frames.pop(key, None)
            if previous is not None:
                self._bytes -= previous.nbytes
            if pixels.nbytes > self.max_bytes or self.max_entries == 0:
                evicted.append((key, pixels))
            else:
                self._frames[key] = pixels
                self._bytes += pixels.nbytes
                while self._bytes > self.max_bytes or len(self._frames) > self.max_entries:
                    victim_key, victim = self._frames.popitem(last=False)
                    self._bytes -= victim.nbytes
                    evicted.append((victim_key, victim))
        self._dispatch(evicted, generation)

    def _dispatch(self, evicted, generation):
        if self._on_evict is None or not evicted:
            return
        with self._dispatch_lock:
            with self._lock:
                if generation != self._generation:
                    return
            for key, pixels in evicted:
                self._on_evict(key, pixels)

    def get_or_build(self, key: str, build, check_cancel=None) -> np.ndarray:
        """Share one build per key while independent keys may render concurrently.

        ``check_cancel`` raises RenderCancelled for the current caller. A waiter
        retries when a different caller's cancelled build fails; ordinary build
        errors still propagate. Oversized frames may be shared by current
        waiters without becoming retained cache entries.
        """
        def check():
            if check_cancel is not None:
                check_cancel()

        while True:
            check()
            with self._lock:
                cached = self.get(key)
                if cached is not None:
                    return cached
                future = self._builds.get(key)
                owner = future is None
                if owner:
                    future = Future()
                    self._builds[key] = future
                    generation = self._generation
            if owner:
                try:
                    pixels = build()
                    self._put(key, pixels, generation)
                except BaseException as error:
                    with self._lock:
                        if self._builds.get(key) is future:
                            self._builds.pop(key, None)
                        future.set_exception(error)
                    raise
                with self._lock:
                    if self._builds.get(key) is future:
                        self._builds.pop(key, None)
                    future.set_result(pixels)
                check()
                return pixels
            try:
                while True:
                    check()
                    try:
                        pixels = future.result(timeout=.05)
                        check()
                        return pixels
                    except TimeoutError:
                        if future.done():
                            # A TimeoutError raised by build is a real failure.
                            raise
            except RenderCancelled:
                check()
                # The owner was superseded; this independent export still
                # needs the film frame and can become the next build owner.
                continue

    def clear(self) -> None:
        """Discard cached frames and invalidate pending writebacks without writing."""
        with self._dispatch_lock:
            with self._lock:
                self._generation += 1
                self._frames.clear()
                self._builds.clear()
                self._bytes = 0
            if self._on_clear is not None:
                self._on_clear()

    def flush(self) -> None:
        """Drain retained frames to the eviction callback, e.g. before shutdown."""
        with self._dispatch_lock:
            with self._lock:
                evicted = list(self._frames.items())
                self._frames.clear()
                self._bytes = 0
                generation = self._generation
            self._dispatch(evicted, generation)


class FilmCacheWriter:
    """One active frame and no queued frames for optional disk-cache writeback.

    ``write(key, pixels, commit)`` writes a temporary file, then calls
    ``commit(publish_callback)`` to publish only if no purge has invalidated the
    frame. The commit and invalidation are serialized, so an old staged file
    cannot reappear after a purge. Cache writes remain best effort; failures
    are retained in ``last_error`` without failing a photograph's export.
    """

    def __init__(self, write):
        self._write = write
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-cache")
        self._slot = threading.BoundedSemaphore(1)
        self._lock = threading.RLock()
        self._close_lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._synchronous = False
        self.last_error = None

    def submit(self, key: str, pixels: np.ndarray) -> bool:
        self._slot.acquire()
        try:
            with self._lock:
                if self._closed:
                    self._slot.release()
                    return False
                generation = self._generation
                synchronous = self._synchronous
                if not synchronous:
                    self._executor.submit(self._run, key, pixels, generation)
        except BaseException:
            self._slot.release()
            raise
        if synchronous:
            self._run(key, pixels, generation)
        return True

    def _run(self, key, pixels, generation):
        def commit(publish):
            with self._lock:
                if generation != self._generation:
                    return False
                publish()
                return True
        try:
            with self._lock:
                current = generation == self._generation
            if current:
                self._write(key, pixels, commit)
        except Exception as error:
            with self._lock:
                self.last_error = error
        finally:
            self._slot.release()

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1

    def close(self, flush=None) -> None:
        """Drain workers, then flush synchronously without starting new threads.

        ThreadPoolExecutor's interpreter shutdown precedes ordinary atexit
        callbacks. A final cache flush must therefore execute its submissions
        on the closing thread, even when no worker was ever started.
        """
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._synchronous = True
            try:
                self._executor.shutdown(wait=True)
                if flush is not None:
                    flush()
            finally:
                # Account for any caller that entered submit during shutdown.
                self._slot.acquire()
                with self._lock:
                    self._closed = True
                self._slot.release()
