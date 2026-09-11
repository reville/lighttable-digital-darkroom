# SPDX-License-Identifier: GPL-3.0-only
"""Priority admission and bounded, replaceable work for interactive previews.

Running image/GPU operations finish normally. Priority and cancellation apply
at the next admission boundary, so obsolete work cannot jump ahead of a frame
the user is waiting to see.
"""
from __future__ import annotations

import heapq
import itertools
import threading
from collections import OrderedDict
from concurrent.futures import Future


class RenderCancelled(RuntimeError):
    """A queued render was superseded before it entered the renderer."""


class PriorityGate:
    """A lock-compatible gate with FIFO ordering within each priority."""

    PRIORITIES = {"interactive": 0, "refine": 1, "background": 2,
                  "export": 2, "prefetch": 3}

    def __init__(self, *, reentrant=False):
        self._condition = threading.Condition()
        self._active = False
        self._waiters = []
        self._sequence = itertools.count()
        self._reentrant = reentrant
        self._owner = None
        self._depth = 0

    def acquire(self, blocking=True, *, priority="interactive", cancelled=None):
        with self._condition:
            if cancelled and cancelled():
                return False
            if self._reentrant and self._owner == threading.get_ident():
                self._depth += 1
                return True
            rank = self.PRIORITIES.get(priority, self.PRIORITIES["background"])
            ticket = (rank, next(self._sequence))
            if not blocking and (self._active or self._waiters):
                return False
            heapq.heappush(self._waiters, ticket)
            self._condition.notify_all()
            try:
                while self._active or self._waiters[0] != ticket:
                    if cancelled and cancelled():
                        return False
                    self._condition.wait(0.05 if cancelled else None)
                if cancelled and cancelled():
                    return False
                self._active = True
                self._owner = threading.get_ident()
                self._depth = 1
                return True
            finally:
                self._waiters.remove(ticket)
                heapq.heapify(self._waiters)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if not self._active:
                raise RuntimeError("release unlocked priority gate")
            if self._reentrant and self._owner != threading.get_ident():
                raise RuntimeError("priority gate belongs to another thread")
            self._depth -= 1
            if self._depth:
                return
            self._active = False
            self._owner = None
            self._condition.notify_all()

    def locked(self):
        with self._condition:
            return self._active

    def __enter__(self):
        self.acquire(priority="background")
        return self

    def __exit__(self, *exc):
        self.release()


class LatestWorkQueue:
    """One worker, at most N pending jobs, newest request per owner wins.

    A running job is never killed. A newer request replaces pending work for
    its window; independent windows retain their own pending refinement.
    Callbacks run outside the queue lock, including Future cancellation hooks.
    """

    def __init__(self, max_pending=16, name="lighttable-refine"):
        self.max_pending = max(1, int(max_pending))
        self.name = name
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._thread = None
        self._closed = False

    def submit(self, owner, function, *args, cancelled=None):
        future = Future()
        discarded = []
        with self._condition:
            if self._closed:
                raise RuntimeError("refinement queue is closed")
            previous = self._pending.pop(owner, None)
            if previous:
                discarded.append(previous[0])
            self._pending[owner] = (future, function, args, cancelled)
            while len(self._pending) > self.max_pending:
                discarded.append(self._pending.popitem(last=False)[1][0])
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name=self.name)
                self._thread.start()
            self._condition.notify()
        for old in discarded:
            old.cancel()
        return future

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed and not self._pending:
                    return
                _, (future, function, args, cancelled) = self._pending.popitem(last=False)
            if cancelled and cancelled():
                future.cancel()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)

    def shutdown(self, wait=True, *, cancel_futures=True):
        with self._condition:
            self._closed = True
            discarded = list(self._pending.values()) if cancel_futures else []
            if cancel_futures:
                self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        for future, *_ in discarded:
            future.cancel()
        if wait and thread is not None:
            thread.join()
