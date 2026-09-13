# SPDX-License-Identifier: GPL-3.0-only
"""Use the bundled parallel decoder without regressing X-Trans development."""
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
import importlib
import threading

from render_scheduling import PriorityGate, RenderCancelled


class RawDecodeCancelled(RenderCancelled):
    """A superseded capture never publishes partial sensor pixels."""


_cancelled = ContextVar("raw_decode_cancelled", default=None)
_priority = ContextVar("raw_decode_priority", default="interactive")
_retain = ContextVar("raw_decode_retain", default=None)
_retain_pixels = ContextVar("raw_decode_retain_pixels", default=True)
_decode_gate = PriorityGate()


@contextmanager
def cancellation(check, *, priority=None):
    previous = _cancelled.get()
    combined = (lambda: check() or previous()) if check and previous else check or previous
    token = _cancelled.set(combined)
    priority_token = _priority.set(priority or _priority.get())
    try:
        check_cancel()
        yield
    finally:
        _cancelled.reset(token)
        _priority.reset(priority_token)


@contextmanager
def retained(keep):
    """Keep a superseded decode alive while keep() reports a live consumer.

    A prefetch is cancelled by the very navigation it anticipated: the new
    request bumps the generation, the old decode is marked stale, and the new
    request would start the same demosaic from scratch. While another request
    waits on this decode, its result is still wanted, so cancellation is
    deferred until the pixels are published.
    """
    token = _retain.set(keep)
    try:
        yield
    finally:
        _retain.reset(token)


@contextmanager
def unretained_pixels():
    """Decode without displacing the working set from the process-wide cache."""
    token = _retain_pixels.set(False)
    try:
        yield
    finally:
        _retain_pixels.reset(token)


def retain_pixels():
    return bool(_retain_pixels.get())


def _stale(check, keep):
    return bool(check and check()) and not (keep and keep())


def check_cancel():
    if _stale(_cancelled.get(), _retain.get()):
        raise RawDecodeCancelled("RAW decode superseded or cancelled")


def is_cancelled():
    return _stale(_cancelled.get(), _retain.get())


def decoder_busy():
    """True while a demosaic holds the admission slot or one is queued."""
    return _decode_gate.locked()


@contextmanager
def decode_slot():
    """One multithreaded demosaic at a time; visible requests get first admission.

    The slot ends before GPU rendering, allowing the next batch capture to
    decode while the current one renders without oversubscribing CPU threads.
    """
    if not _decode_gate.acquire(priority=_priority.get(), cancelled=is_cancelled):
        raise RawDecodeCancelled("RAW decode cancelled while queued")
    try:
        check_cancel()
        yield
    finally:
        _decode_gate.release()


@contextmanager
def interruptible(raw, decoder):
    """Bridge request cancellation to LibRaw's atomic flag, never kill a thread.

    The monitor owns a strong reference until it joins, before RawPy recycles
    its native object. Older wheels retain the ordinary completion fallback.
    """
    check_cancel()
    # Context variables do not follow the monitor thread; capture both hooks.
    check, keep = _cancelled.get(), _retain.get()
    finished = threading.Event()
    monitor = None
    if check and getattr(decoder, "LIGHTTABLE_RAW_CANCEL", 0) == 1:
        def watch():
            # A hard ten-minute bound also covers a defective native decoder.
            for _ in range(30000):
                if finished.wait(0.02):
                    return
                if _stale(check, keep):
                    raw.request_cancel()
                    return
        monitor = threading.Thread(target=watch, name="lighttable-raw-cancel", daemon=True)
        monitor.start()
    try:
        yield
    except Exception:
        check_cancel()
        raise
    finally:
        finished.set()
        if monitor is not None:
            monitor.join()
    check_cancel()


@contextmanager
def open_raw(path):
    stock = importlib.import_module("rawpy")
    try:
        decoder = importlib.import_module("rawpy_openmp")
    except ImportError:
        decoder = stock
    raw = None
    try:
        raw = decoder.imread(str(path))
        if (decoder is not stock and raw.is_xtrans
                and getattr(decoder, "LIGHTTABLE_XTRANS_WAVEFRONT", 0) != 1):
            # is_xtrans reads only the already-open header; it never unpacks
            # sensor pixels. Bayer takes one header open; X-Trans adds one
            # cheap header read for old wheels lacking the verified schedule.
            raw.close()
            raw = None
            decoder = stock
            raw = stock.imread(str(path))
        with raw:
            yield raw, decoder
    except Exception as error:
        if decoder is not stock and isinstance(error, decoder.LibRawError):
            error_type = getattr(stock, type(error).__name__, stock.LibRawError)
            raise error_type(str(error)) from error
        raise


def native_options(options, decoder):
    """The two extension modules have distinct enum classes with equal values."""
    return {key: getattr(decoder, type(value).__name__)(value.value)
            if isinstance(value, Enum) else value
            for key, value in options.items()}
