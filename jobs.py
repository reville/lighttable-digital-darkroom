# SPDX-License-Identifier: GPL-3.0-only
"""Uniform records for LightTable background work."""
from __future__ import annotations

from server_localization import T

import copy
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable


TERMINAL_STATES = {"done", "failed", "cancelled"}
STATES = {"queued", "running", *TERMINAL_STATES}


class JobRegistry:
    def __init__(self, *, maximum: int = 200,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self.maximum = maximum
        self.on_change = on_change
        self._lock = threading.RLock()
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._cancellers: dict[str, Callable[[], None]] = {}

    def create(self, kind: str, *, total: int = 0, state: str = "queued",
               result=None, cancel: Callable[[], None] | None = None) -> dict:
        if state not in STATES:
            raise ValueError(T("unknown job state: {state}", state=f'{state}'))
        now = time.time()
        ident = uuid.uuid4().hex
        record = {
            "id": ident,
            "kind": str(kind),
            "state": state,
            "progress": 0,
            "total": max(0, int(total or 0)),
            "started": now if state == "running" else None,
            "finished": now if state in TERMINAL_STATES else None,
            "errors": [],
            "log": [],
            "result": result,
        }
        with self._lock:
            self._records[ident] = record
            if cancel:
                self._cancellers[ident] = cancel
            self._prune()
        self._emit(record)
        return copy.deepcopy(record)

    def update(self, ident: str, **changes) -> dict:
        with self._lock:
            if ident not in self._records:
                raise KeyError(T("unknown job: {ident}", ident=f'{ident}'))
            record = self._records[ident]
            if record["state"] in TERMINAL_STATES:
                return copy.deepcopy(record)
            if "state" in changes and changes["state"] not in STATES:
                raise ValueError(T("unknown job state: {value}", value=f"{changes['state']}"))
            if changes.get("state") == "running" and not record["started"]:
                changes["started"] = time.time()
            if changes.get("state") in TERMINAL_STATES:
                changes.setdefault("finished", time.time())
            if "log" in changes:
                changes["log"] = list(changes["log"] or [])[-200:]
            if "errors" in changes:
                changes["errors"] = list(changes["errors"] or [])[-200:]
            record.update(changes)
            snapshot = copy.deepcopy(record)
            self._prune()
        self._emit(snapshot)
        return snapshot

    def _prune(self) -> None:
        """Bound history without losing a live job or its cancel callback.

        Called with the registry lock held. Active work may temporarily exceed
        the retention limit; completion brings the history back within it.
        """
        if len(self._records) <= self.maximum:
            return
        for ident, record in list(self._records.items()):
            if record["state"] in TERMINAL_STATES:
                del self._records[ident]
                self._cancellers.pop(ident, None)
                if len(self._records) <= self.maximum:
                    break

    def get(self, ident: str) -> dict | None:
        with self._lock:
            value = self._records.get(ident)
            return copy.deepcopy(value) if value else None

    def list(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(record)
                    for record in reversed(self._records.values())]

    def cancel(self, ident: str) -> dict:
        with self._lock:
            record = self._records.get(ident)
            cancel = self._cancellers.get(ident)
            if not record:
                raise KeyError(T("unknown job: {ident}", ident=f'{ident}'))
            if record["state"] in TERMINAL_STATES:
                return copy.deepcopy(record)
            if cancel is None:
                raise RuntimeError(T("job is not cancellable"))
        # A cooperative worker returns False and owns the terminal transition
        # after its active work and staging files have finished cleaning up.
        if cancel() is False:
            return self.update(ident, cancelRequested=True)
        return self.update(ident, state="cancelled")

    def _emit(self, record: dict) -> None:
        if self.on_change:
            self.on_change(copy.deepcopy(record))
