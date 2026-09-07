from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import mmap
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import numpy as np
import tifffile

import color_pipeline
import export_surface
from render_scheduling import RenderCancelled


@contextmanager
def surface_for(pixels):
    """Model the worker's immutable named allocation without a resource tracker."""
    pixels = np.ascontiguousarray(pixels, dtype=np.float32)
    height, width = pixels.shape[:2]
    libc = export_surface._libc()
    name = f"/lte-{os.getpid():x}-{uuid.uuid4().hex[:8]}"
    fd = libc.shm_open(name.encode(), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    if fd < 0:
        raise OSError("unable to create test shared export")
    try:
        os.ftruncate(fd, pixels.nbytes)
        with mmap.mmap(fd, pixels.nbytes) as mapping:
            mapping.write(pixels.tobytes())
    finally:
        os.close(fd)
    descriptor = {"name": name, "width": width, "height": height,
                  "length": pixels.nbytes, "rowBytes": width * 12,
                  "offset": 0, "format": "rgb32f", "byteOrder": "native"}
    try:
        yield descriptor
    finally:
        libc.shm_unlink(name.encode())


@unittest.skipUnless(export_surface.supported(), "POSIX shared export transport")
class ExportSurfaceTests(unittest.TestCase):
    def assert_unlinked(self, descriptor):
        fd = export_surface._libc().shm_open(descriptor["name"].encode(), os.O_RDONLY, 0)
        if fd >= 0:
            os.close(fd)
        self.assertEqual(fd, -1)

    def test_adoption_keeps_every_float_bit_and_unlinks_name(self):
        bits = np.array([0x3f000123, 0xbe000000, 0x3fa00000,
                         0x7f800000, 0x7fc00123, 0x80000000], np.uint32)
        pixels = bits.view(np.float32).reshape((1, 2, 3))
        with surface_for(pixels) as descriptor:
            adopted = export_surface.adopt_surface(descriptor)
            self.assert_unlinked(descriptor)
        self.assertIsInstance(adopted.base, mmap.mmap)
        self.assertFalse(adopted.flags.writeable)
        np.testing.assert_array_equal(adopted.view(np.uint32).ravel(), bits)
        with self.assertRaises(ValueError):
            adopted[0, 0, 0] = 0.0

    def test_array_can_outlive_scope_and_eviction(self):
        cache = export_surface.SharedFilmCache(max_bytes=72, max_entries=1)
        pixels = np.linspace(0, 1, 18, dtype=np.float32).reshape((2, 3, 3))
        with surface_for(pixels) as descriptor:
            with export_surface.open_surface(descriptor) as active_export:
                cache.put("first", active_export)
        cache.put("second", np.zeros_like(pixels))
        self.assertIsNone(cache.get("first"))
        np.testing.assert_array_equal(active_export, pixels)
        cache.clear()
        np.testing.assert_array_equal(active_export, pixels)
        self.assertEqual(cache.nbytes, 0)

    def test_tiff_and_shared_sources_have_identical_finisher_input(self):
        pixels = np.random.default_rng(5).uniform(-.2, 1.2, (29, 41, 3)).astype(np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render.tif"
            tifffile.imwrite(path, pixels, photometric="rgb")
            expected = color_pipeline.load_float_rgb(path)
        with surface_for(pixels) as descriptor:
            actual = color_pipeline.as_float_rgb(export_surface.adopt_surface(descriptor))
        np.testing.assert_array_equal(actual, expected)

    def test_rejects_bad_layouts_and_releases_valid_worker_names(self):
        for change in ({"length": 4}, {"width": -1}, {"rowBytes": 256},
                       {"format": "rgba8"}, {"byteOrder": "big"}, {"offset": 4},
                       {"height": 100000000}, {"width": "invalid"}):
            with self.subTest(change=change), surface_for(np.zeros((1, 2, 3), np.float32)) as descriptor:
                with self.assertRaises((ValueError, KeyError)):
                    export_surface.adopt_surface(dict(descriptor, **change))
                self.assert_unlinked(descriptor)

    def test_rejects_actual_truncated_allocation(self):
        with surface_for(np.zeros((1, 2, 3), np.float32)) as descriptor:
            # macOS refuses a second ftruncate on a shm object. Instead claim
            # a valid layout larger than the actual page-rounded allocation.
            width = os.sysconf("SC_PAGE_SIZE")
            descriptor.update(width=width, rowBytes=width * 12, length=width * 12)
            with self.assertRaisesRegex(ValueError, "length"):
                export_surface.adopt_surface(descriptor)
            self.assert_unlinked(descriptor)

    def test_discard_releases_unused_result(self):
        with surface_for(np.zeros((1, 2, 3), np.float32)) as descriptor:
            export_surface.discard_surface(descriptor)
            self.assert_unlinked(descriptor)
            export_surface.discard_surface(descriptor)


class SharedFilmCacheTests(unittest.TestCase):
    def test_eviction_and_flush_preserve_float_bits_outside_data_lock(self):
        calls = []
        cache = None

        def write(key, pixels):
            # Another thread can access the cache during a slow write dispatch.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: cache.nbytes).result(timeout=1)
            calls.append((key, pixels.copy()))

        cache = export_surface.SharedFilmCache(max_entries=1, on_evict=write)
        first = np.arange(18, dtype=np.float32).reshape((2, 3, 3)) / 19
        second = np.full((2, 3, 3), .500123, dtype=np.float32)
        cache.put("first", first)
        self.assertEqual(calls, [])
        cache.put("second", second)
        self.assertEqual([key for key, _ in calls], ["first"])
        np.testing.assert_array_equal(calls[0][1].view(np.uint32), first.view(np.uint32))
        cache.flush()
        self.assertEqual([key for key, _ in calls], ["first", "second"])
        np.testing.assert_array_equal(calls[1][1].view(np.uint32), second.view(np.uint32))
        self.assertEqual(cache.nbytes, 0)

    def test_clear_never_dispatches_writes_or_reinserts_running_build(self):
        calls = []
        cache = export_surface.SharedFilmCache(on_evict=lambda *args: calls.append(args))
        entered, release = threading.Event(), threading.Event()

        def build():
            entered.set()
            self.assertTrue(release.wait(2))
            return np.ones((1, 1, 3), np.float32)

        cache.put("cached", np.zeros((1, 1, 3), np.float32))
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(cache.get_or_build, "building", build)
            self.assertTrue(entered.wait(2))
            cache.clear()
            release.set()
            active.result(timeout=2)
        self.assertEqual(calls, [])
        self.assertEqual(cache.nbytes, 0)
        self.assertIsNone(cache.get("building"))

    def test_single_flight_does_not_block_different_keys(self):
        cache = export_surface.SharedFilmCache()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow_build():
            calls.append("first")
            entered.set()
            self.assertTrue(release.wait(2))
            return np.zeros((1, 1, 3), np.float32)

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(cache.get_or_build, "first", slow_build)
            self.assertTrue(entered.wait(2))
            waiter = pool.submit(cache.get_or_build, "first", slow_build)
            independent = pool.submit(cache.get_or_build, "other",
                lambda: np.ones((1, 1, 3), np.float32))
            np.testing.assert_array_equal(independent.result(timeout=1), np.ones((1, 1, 3)))
            release.set()
            np.testing.assert_array_equal(first.result(timeout=2), waiter.result(timeout=2))
        self.assertEqual(calls, ["first"])

    def test_cancelled_owner_does_not_cancel_waiter(self):
        cache = export_surface.SharedFilmCache()
        entered, release = threading.Event(), threading.Event()
        waiter_entered = threading.Event()

        def cancelled_build():
            entered.set()
            self.assertTrue(release.wait(2))
            raise RenderCancelled("owner cancelled")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.get_or_build, "shared", cancelled_build)
            self.assertTrue(entered.wait(2))
            waiter = pool.submit(cache.get_or_build, "shared",
                lambda: np.ones((1, 1, 3), np.float32), waiter_entered.set)
            self.assertTrue(waiter_entered.wait(2))
            release.set()
            with self.assertRaises(RenderCancelled):
                first.result(timeout=2)
            np.testing.assert_array_equal(waiter.result(timeout=2), np.ones((1, 1, 3)))

    def test_cancelled_waiter_stops_without_cancelling_owner(self):
        cache = export_surface.SharedFilmCache()
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()

        def build():
            entered.set()
            self.assertTrue(release.wait(2))
            return np.ones((1, 1, 3), np.float32)

        def check():
            if cancel.is_set():
                raise RenderCancelled("waiter cancelled")

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(cache.get_or_build, "key", build)
            self.assertTrue(entered.wait(2))
            waiter = pool.submit(cache.get_or_build, "key", build, check)
            cancel.set()
            with self.assertRaises(RenderCancelled):
                waiter.result(timeout=1)
            release.set()
            np.testing.assert_array_equal(owner.result(timeout=2), np.ones((1, 1, 3)))

    def test_build_failures_are_retryable_without_retaining_broken_frames(self):
        cache = export_surface.SharedFilmCache()

        def fail():
            raise ValueError("render failed")

        with self.assertRaisesRegex(ValueError, "render failed"):
            cache.get_or_build("key", fail)
        self.assertIsNone(cache.get("key"))
        pixels = cache.get_or_build("key", lambda: np.ones((1, 1, 3), np.float32))
        np.testing.assert_array_equal(pixels, np.ones((1, 1, 3)))

    def test_byte_limit_lru_and_large_frame_bypass(self):
        cache = export_surface.SharedFilmCache(max_bytes=72, max_entries=4)
        frame = np.zeros((1, 1, 3), np.float32)
        for key in ("a", "b", "c"):
            cache.put(key, np.repeat(frame, 2, axis=0))
        self.assertEqual(cache.nbytes, 72)
        self.assertIsNotNone(cache.get("a"))
        cache.put("d", np.repeat(frame, 2, axis=0))
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("a"))
        cache.put("a", np.zeros((9, 9, 3), np.float32))
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.nbytes, 48)

    def test_writable_input_is_snapshotted_without_mutating_owner(self):
        cache = export_surface.SharedFilmCache()
        pixels = np.zeros((1, 1, 3), np.float32)
        cache.put("key", pixels)
        pixels[:] = 1
        np.testing.assert_array_equal(cache.get("key"), np.zeros_like(pixels))
        self.assertFalse(cache.get("key").flags.writeable)


class FilmCacheWriterTests(unittest.TestCase):
    def test_close_flush_runs_synchronously_and_is_idempotent(self):
        written = []
        def write(key, _pixels, commit):
            commit(lambda: written.append((key, threading.current_thread().name)))
        writer = export_surface.FilmCacheWriter(write)
        cache = export_surface.SharedFilmCache(on_evict=writer.submit,
                                              on_clear=writer.invalidate)
        cache.put("retained", np.ones((1, 1, 3), np.float32))
        writer.close(cache.flush)
        writer.close(lambda: self.fail("close flushed twice"))
        self.assertEqual(written, [("retained", threading.current_thread().name)])
        self.assertEqual(cache.nbytes, 0)

    def test_interpreter_exit_persists_retained_frame_after_executor_shutdown(self):
        script = '''
import atexit
from pathlib import Path
import sys
import threading
import numpy as np
import tifffile
import export_surface

root = Path(sys.argv[1])
def write(key, pixels, commit):
    if key == "retained":
        assert threading.current_thread().name == "MainThread"
    stage, destination = root / (key + ".stage"), root / (key + ".tif")
    try:
        tifffile.imwrite(stage, pixels, photometric="rgb")
        commit(lambda: stage.replace(destination))
    finally:
        stage.unlink(missing_ok=True)

writer = export_surface.FilmCacheWriter(write)
cache = export_surface.SharedFilmCache(on_evict=writer.submit,
                                      on_clear=writer.invalidate)
atexit.register(lambda: writer.close(cache.flush))
pixels = np.array([-.125, .500123, 1.25, 0., .25, .75], np.float32).reshape(1, 2, 3)
writer.submit("earlier", pixels)
cache.put("retained", pixels)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = subprocess.run([sys.executable, "-c", script, directory],
                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            expected = np.array([-.125, .500123, 1.25, 0., .25, .75], np.float32).reshape(1, 2, 3)
            for name in ("earlier", "retained"):
                np.testing.assert_array_equal(tifffile.imread(root / (name + ".tif")), expected)
            self.assertEqual(list(root.glob("*.stage")), [])

    def test_slow_writer_accepts_only_one_active_frame(self):
        entered, release, submitting = threading.Event(), threading.Event(), threading.Event()
        published = []

        def write(key, pixels, commit):
            if key == "first":
                entered.set()
                self.assertTrue(release.wait(2))
            commit(lambda: published.append((key, pixels.copy())))

        writer = export_surface.FilmCacheWriter(write)
        pixels = np.ones((1, 1, 3), np.float32)
        try:
            writer.submit("first", pixels)
            self.assertTrue(entered.wait(2))
            with ThreadPoolExecutor(max_workers=1) as pool:
                def submit_second():
                    submitting.set()
                    return writer.submit("second", pixels)
                queued = pool.submit(submit_second)
                self.assertTrue(submitting.wait(2))
                with self.assertRaises(TimeoutError):
                    queued.result(timeout=.05)
                self.assertEqual(published, [])
                release.set()
                self.assertTrue(queued.result(timeout=2))
        finally:
            release.set()
            writer.close()
        self.assertEqual([key for key, _ in published], ["first", "second"])
        self.assertFalse(writer.submit("closed", pixels))

    def test_purge_invalidates_staged_write_and_clear_does_not_resurrect(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(key, pixels, commit):
                stage, final = root / (key + ".stage"), root / (key + ".tif")
                try:
                    tifffile.imwrite(stage, pixels, photometric="rgb")
                    entered.set()
                    self.assertTrue(release.wait(2))
                    outcomes.append(commit(lambda: stage.replace(final)))
                finally:
                    stage.unlink(missing_ok=True)

            writer = export_surface.FilmCacheWriter(write)
            cache = export_surface.SharedFilmCache(max_entries=1,
                on_evict=writer.submit, on_clear=writer.invalidate)
            pixels = np.full((1, 2, 3), .500123, np.float32)
            try:
                cache.put("first", pixels)
                cache.put("second", pixels)
                self.assertTrue(entered.wait(2))
                cache.clear()
                release.set()
            finally:
                release.set()
                writer.close()
            self.assertEqual(outcomes, [False])
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(cache.nbytes, 0)

    def test_writer_failure_does_not_block_next_frame(self):
        published = []
        def write(key, _pixels, commit):
            if key == "broken":
                raise OSError("cache disk unavailable")
            commit(lambda: published.append(key))
        writer = export_surface.FilmCacheWriter(write)
        try:
            pixels = np.zeros((1, 1, 3), np.float32)
            writer.submit("broken", pixels)
            writer.submit("next", pixels)
        finally:
            writer.close()
        self.assertEqual(published, ["next"])
        self.assertIsInstance(writer.last_error, OSError)


if __name__ == "__main__":
    unittest.main()
