# SPDX-License-Identifier: GPL-3.0-only
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import color_pipeline
from raw_decode_cache import DecodedRawCache
import raw_decode_runtime as runtime


class SharedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "capture.raf"
        self.source.write_bytes(b"original capture")
        self.pixels = np.arange(80 * 60 * 3, dtype=np.uint16).reshape(60, 80, 3)
        self.decode = mock.Mock(side_effect=lambda **options:
                                self.pixels[::2, ::2].copy() if options["half_size"]
                                else self.pixels.copy())
        self.raw = SimpleNamespace(sizes=SimpleNamespace(width=80),
                                   daylight_whitebalance=[2, 1, 1.5, 1],
                                   postprocess=self.decode)
        import rawpy
        @contextmanager
        def opened(_):
            yield self.raw, rawpy
        patch = mock.patch.object(runtime, "open_raw", opened)
        patch.start(); self.addCleanup(patch.stop)
        cache = mock.patch.object(color_pipeline, "RAW_DEMOSAIC_CACHE", DecodedRawCache(1024 * 1024))
        cache.start(); self.addCleanup(cache.stop)

    def test_custom_white_balance_reuses_unmodified_daylight_capture(self):
        daylight = color_pipeline.decode_raw(self.source, {"wb_mode": "daylight"})
        for temperature, tint in [(3200, 0), (5300, 0.2), (7200, -0.3)]:
            params = dict(wb_mode="custom", wb_temperature=temperature, wb_tint=tint)
            output = color_pipeline.decode_raw(self.source, params)
            reference = color_pipeline.apply_custom_raw_white_balance(
                self.pixels.astype(np.float32) / 65535, temperature, tint)
            np.testing.assert_array_equal(output, (reference * 65535 + 0.5).astype(np.uint16))
        self.assertEqual(self.decode.call_count, 1)
        np.testing.assert_array_equal(daylight, self.pixels)
        self.assertFalse(daylight.flags.writeable)

    def test_preview_widths_share_half_capture_but_full_capture_stays_distinct(self):
        a = color_pipeline.decode_raw(self.source, max_width=20)
        b = color_pipeline.decode_raw(self.source, max_width=40)
        full = color_pipeline.decode_raw(self.source)
        again = color_pipeline.decode_raw(self.source)
        self.assertIs(a, b)
        self.assertIs(full, again)
        self.assertEqual(self.decode.call_count, 2)
        self.assertEqual(full.shape, self.pixels.shape)

    def test_develop_curve_is_downstream_but_sensor_decisions_invalidate(self):
        color_pipeline.decode_raw(self.source, {"developProfile": "linear"})
        color_pipeline.decode_raw(self.source, {"developProfile": "soft"})
        self.assertEqual(self.decode.call_count, 1)
        color_pipeline.decode_raw(self.source, {"raw_profile": "detail"})
        color_pipeline.decode_raw(self.source, {"raw_highlight_recovery": "blend"})
        color_pipeline.decode_raw(self.source, {"wb_mode": "daylight"})
        self.assertEqual(self.decode.call_count, 4)

    def test_replaced_source_is_not_a_cache_hit(self):
        color_pipeline.decode_raw(self.source)
        self.source.write_bytes(b"different source contents")
        color_pipeline.decode_raw(self.source)
        self.assertEqual(self.decode.call_count, 2)

    def test_atomic_replacement_after_open_does_not_poison_new_source(self):
        import rawpy
        opens = []
        @contextmanager
        def opened(_):
            old = not opens
            opens.append(True)
            captured = self.pixels.copy() + int(not old)
            if old:
                replacement = self.source.with_suffix(".replacement")
                replacement.write_bytes(b"replacement capture")
                replacement.replace(self.source)
            yield SimpleNamespace(sizes=self.raw.sizes,
                                  postprocess=lambda **_: captured), rawpy
        with mock.patch.object(runtime, "open_raw", opened):
            color_pipeline.decode_raw(self.source)
            output = color_pipeline.decode_raw(self.source)
        np.testing.assert_array_equal(output, self.pixels + 1)

    def test_learned_denoise_keeps_dictionary_cancellation_contract(self):
        status = {"cancelled": False}
        runner = mock.Mock(side_effect=lambda tile, *_: tile)
        color_pipeline.decode_raw(self.source, {"learned_denoise": True},
                                  learned_denoise_runner=runner,
                                  learned_denoise_cancel=status)
        self.assertGreater(runner.call_count, 0)
        status["cancelled"] = True
        with self.assertRaisesRegex(RuntimeError, "denoise cancelled"):
            color_pipeline.decode_raw(self.source, {"learned_denoise": True},
                                      learned_denoise_runner=runner,
                                      learned_denoise_cancel=status)

    def test_concurrent_preview_and_export_decode_once(self):
        started, release = threading.Event(), threading.Event()
        def decode(**_):
            started.set()
            self.assertTrue(release.wait(2))
            return self.pixels.copy()
        self.decode.side_effect = decode
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(color_pipeline.decode_raw, self.source)
            self.assertTrue(started.wait(1))
            b = pool.submit(color_pipeline.decode_raw, self.source)
            release.set()
            np.testing.assert_array_equal(a.result(2), b.result(2))
        self.assertEqual(self.decode.call_count, 1)


class CacheBudgetTests(unittest.TestCase):
    def test_lru_and_oversized_inputs_never_exceed_retained_budget(self):
        cache = DecodedRawCache(12)
        def get(key, size=6):
            return cache.get_or_build(key, lambda: np.zeros(size, dtype=np.uint8), lambda: None)
        a, b = get("a"), get("b")
        self.assertIs(a, get("a"))
        get("c")
        self.assertIs(a, get("a"))
        self.assertIsNot(b, get("b"))
        get("too large", 13)
        self.assertEqual(cache.stats()["bytes"], 12)
        self.assertEqual(cache.stats()["entries"], 2)

    def test_cancelled_owner_does_not_poison_a_live_waiter(self):
        cache = DecodedRawCache(100)
        started, release = threading.Event(), threading.Event()
        def stale():
            started.set()
            self.assertTrue(release.wait(2))
            raise runtime.RawDecodeCancelled("superseded")
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(cache.get_or_build, "a", stale, lambda: None)
            self.assertTrue(started.wait(1))
            b = pool.submit(cache.get_or_build, "a", lambda: np.ones(3), lambda: None)
            release.set()
            with self.assertRaises(runtime.RawDecodeCancelled):
                a.result(2)
            np.testing.assert_array_equal(b.result(2), np.ones(3))


    def test_live_waiter_adopts_a_superseded_owner_decode(self):
        # A prefetch is cancelled by the navigation it anticipated. While the
        # navigation's own request waits on that decode, it must finish once
        # and serve both, instead of restarting the demosaic from scratch.
        cache = DecodedRawCache(100)
        stale = threading.Event()
        started, waiter_arrived = threading.Event(), threading.Event()
        builds = []
        def owner():
            with runtime.cancellation(stale.is_set):
                def build():
                    builds.append("owner")
                    started.set()
                    self.assertTrue(waiter_arrived.wait(2))
                    runtime.check_cancel()  # LibRaw-style mid-decode check
                    return np.ones(3)
                return cache.get_or_build("a", build, runtime.check_cancel)
        def waiter():
            with runtime.cancellation(lambda: False):
                return cache.get_or_build(
                    "a", lambda: builds.append("waiter") or np.zeros(3), runtime.check_cancel)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(owner)
            self.assertTrue(started.wait(1))
            stale.set()
            b = pool.submit(waiter)
            for _ in range(200):
                if cache.waiting("a"):
                    break
                time.sleep(0.005)
            self.assertTrue(cache.waiting("a"))
            waiter_arrived.set()
            np.testing.assert_array_equal(b.result(2), np.ones(3))
            with self.assertRaises(runtime.RawDecodeCancelled):
                a.result(2)
        self.assertEqual(builds, ["owner"])
        self.assertFalse(cache.waiting("a"))
        self.assertEqual(cache.stats()["entries"], 1)

    def test_superseded_owner_without_waiters_still_cancels(self):
        cache = DecodedRawCache(100)
        stale = threading.Event()
        def build():
            stale.set()
            runtime.check_cancel()
            return np.ones(3)
        with runtime.cancellation(stale.is_set):
            with self.assertRaises(runtime.RawDecodeCancelled):
                cache.get_or_build("a", build, runtime.check_cancel)
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertFalse(cache.waiting("a"))

    def test_unretained_decode_serves_the_request_without_entering_the_cache(self):
        cache = DecodedRawCache(100)
        pixels = cache.get_or_build("a", lambda: np.ones(3), lambda: None, retain=False)
        np.testing.assert_array_equal(pixels, np.ones(3))
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertEqual(cache.stats()["bytes"], 0)


class ImportWarmupDecodeTests(SharedCaptureTests):
    def test_import_warmup_decode_is_not_retained_but_later_requests_are(self):
        with runtime.unretained_pixels():
            color_pipeline.decode_raw(self.source, max_width=20)
        self.assertEqual(color_pipeline.RAW_DEMOSAIC_CACHE.stats()["entries"], 0)
        color_pipeline.decode_raw(self.source, max_width=20)
        self.assertEqual(color_pipeline.RAW_DEMOSAIC_CACHE.stats()["entries"], 1)
        self.assertEqual(self.decode.call_count, 2)


class CooperativeCancellationTests(unittest.TestCase):
    def test_retained_decode_defers_native_cancel_until_no_consumer_waits(self):
        cancelled, native_cancelled = threading.Event(), threading.Event()
        waiting = threading.Event()
        waiting.set()
        raw = SimpleNamespace(request_cancel=native_cancelled.set)
        decoder = SimpleNamespace(LIGHTTABLE_RAW_CANCEL=1)
        with runtime.cancellation(cancelled.is_set), runtime.retained(waiting.is_set):
            with self.assertRaises(runtime.RawDecodeCancelled):
                with runtime.interruptible(raw, decoder):
                    cancelled.set()
                    self.assertFalse(native_cancelled.wait(0.1))
                    self.assertFalse(runtime.is_cancelled())
                    waiting.clear()
                    self.assertTrue(native_cancelled.wait(1))
                    self.assertTrue(runtime.is_cancelled())
        self.assertFalse(runtime.is_cancelled())

    def test_active_native_decode_stops_without_retry_or_partial_cache(self):
        cancelled, native_cancelled, started = (threading.Event() for _ in range(3))
        class NativeError(Exception):
            pass
        raw = SimpleNamespace(request_cancel=native_cancelled.set)
        decoder = SimpleNamespace(LIGHTTABLE_RAW_CANCEL=1)
        def decode():
            with runtime.cancellation(cancelled.is_set):
                with runtime.interruptible(raw, decoder):
                    started.set()
                    self.assertTrue(native_cancelled.wait(2))
                    raise NativeError("native cancellation")
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(decode)
            self.assertTrue(started.wait(1))
            cancelled.set()
            with self.assertRaises(runtime.RawDecodeCancelled):
                work.result(1)
        self.assertFalse(any(t.name == "lighttable-raw-cancel" for t in threading.enumerate()))

    def test_completed_or_legacy_decoder_does_not_receive_late_cancel(self):
        raw = mock.Mock()
        with runtime.cancellation(lambda: False):
            with runtime.interruptible(raw, SimpleNamespace(LIGHTTABLE_RAW_CANCEL=1)):
                pass
        with runtime.cancellation(lambda: False):
            with runtime.interruptible(raw, SimpleNamespace()):
                pass
        raw.request_cancel.assert_not_called()
