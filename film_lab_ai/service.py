"""Lifecycle and background worker for the optional local AI index."""

from __future__ import annotations

from server_localization import T

import hashlib
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import durable_io

from . import culling
from .providers import LocalPhotoAnalyzer
from .store import IndexStore


class AIIndexService:
    """A finite, single-worker indexer that is inert until explicitly enabled."""

    def __init__(
        self,
        *,
        library: Path,
        data_root: Path,
        vision_helper: Path,
        list_images: Callable[[], list[str]],
        source_key: Callable[[str], str],
        preview_bytes: Callable[[str], bytes],
        source_availability: Callable[[str], str] = lambda name: "local",
        render_busy: Callable[[], bool] = lambda: False,
        worker_cleanup: Callable[[], None] = lambda: None,
        analyzer=None,
    ):
        identity = hashlib.sha256(str(library.resolve()).encode()).hexdigest()[:16]
        self.root = data_root / identity
        self.settings_path = self.root / "settings.json"
        self.store = IndexStore(self.root / "index.sqlite3")
        self.analyzer = analyzer or LocalPhotoAnalyzer(vision_helper)
        self._list_images = list_images
        self._source_key = source_key
        self._preview_bytes = preview_bytes
        self._source_availability = source_availability
        self._render_busy = render_busy
        self._worker_cleanup = worker_cleanup
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._generation = 0
        self._running = False
        self._completed = 0
        self._total = 0
        self._current = ""
        self._last_error = ""
        self._scan_complete = False
        self._skipped: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        raw = durable_io.load_json(self.settings_path, {})
        return bool(raw.get("enabled", False)) if isinstance(raw, dict) else False

    def _set_enabled(self, enabled: bool) -> None:
        durable_io.atomic_write_json(
            self.settings_path, {"enabled": bool(enabled)})

    def status(self) -> dict:
        stats = self.store.stats()
        with self._lock:
            return {
                "enabled": self.enabled,
                "running": self._running,
                "indexed": stats["indexed"],
                "errors": stats["errors"],
                "skipped": sum(self._skipped.values()),
                "skippedReasons": dict(self._skipped),
                "completed": self._completed,
                "total": self._total,
                "current": self._current,
                "lastError": self._last_error,
                "scanComplete": self._scan_complete,
                "indexRecovery": (
                    T("A damaged local index was preserved and rebuilt.")
                    if self.store.last_recovery else ""
                ),
                "capabilities": self.analyzer.capabilities(),
                "privacy": T("Index data stays in LightTable's local data folder."),
            }

    def results(self, names: list[str]) -> dict[str, dict]:
        return self.store.results(names) if self.enabled else {}

    def enable(self) -> dict:
        self._set_enabled(True)
        self.start()
        return self.status()

    def disable(self) -> dict:
        with self._lock:
            self._generation += 1
            self._scan_complete = False
        self._set_enabled(False)
        return self.status()

    def rebuild(self) -> dict:
        with self._lock:
            self._generation += 1
            self._scan_complete = False
        self.store.reset()
        self._set_enabled(True)
        self.start()
        return self.status()

    def clear(self) -> dict:
        with self._lock:
            self._generation += 1
        self._set_enabled(False)
        self.store.reset()
        with self._lock:
            self._completed = 0
            self._total = 0
            self._current = ""
            self._last_error = ""
            self._scan_complete = False
            self._skipped = {}
        return self.status()

    def start(self) -> None:
        with self._lock:
            if (not self.enabled or self._shutdown.is_set()
                    or (self._thread and self._thread.is_alive())):
                return
            generation = self._generation
            self._scan_complete = False
            self._thread = threading.Thread(
                target=self._run, args=(generation,), daemon=True,
                name="film-lab-local-ai-index",
            )
            self._thread.start()

    def _run(self, generation: int) -> None:
        restart = False
        with self._lock:
            self._running = True
            self._completed = 0
            self._last_error = ""
            self._skipped = {}
        try:
            capabilities = self.analyzer.capabilities()
            if not capabilities.get("vision", {}).get("available"):
                raise RuntimeError(T("the local Vision analyzer has not been built"))
            names = self._list_images()
            with self._lock:
                self._total = len(names)
            self.store.remove_missing(names)
            for name in names:
                if not self._should_continue(generation):
                    break
                # Yield to interactive edits for at most five seconds.  The
                # photo is still processed afterwards, so a scan always ends.
                for _ in range(25):
                    if not self._render_busy() or not self._should_continue(generation):
                        break
                    time.sleep(0.2)
                if not self._should_continue(generation):
                    break
                with self._lock:
                    self._current = name
                fingerprint = ""
                try:
                    # Check before hashing as well as decoding: opening a
                    # cloud placeholder can trigger an unwanted download.
                    availability = self._source_availability(name)
                    if availability != "local":
                        self.store.clear_error(name)
                        with self._lock:
                            self._skipped[availability] = (
                                self._skipped.get(availability, 0) + 1)
                        continue
                    fingerprint = self._source_key(name)
                    if self.store.is_current(name, fingerprint,
                                             culling.ANALYSIS_VERSION):
                        continue
                    self._index_one(name, fingerprint, generation)
                except Exception as error:  # one bad photo must not stop a library
                    if self._should_continue(generation):
                        self.store.record_error(name, fingerprint, str(error), time.time())
                        with self._lock:
                            self._last_error = f"{name}: {str(error)[:180]}"
                finally:
                    with self._lock:
                        self._completed += 1
        except Exception as error:
            with self._lock:
                self._last_error = str(error)[:240]
        finally:
            with self._lock:
                self._running = False
                self._current = ""
                current_generation = generation == self._generation
                self._scan_complete = current_generation and self.enabled
                restart = self.enabled and not current_generation
                self._thread = None
            try:
                self._worker_cleanup()
            except Exception:
                pass
            if restart:
                self.start()

    def _index_one(self, name: str, fingerprint: str, generation: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            preview = self._preview_bytes(name)
            with tempfile.NamedTemporaryFile(
                suffix=".jpg", prefix="photo-", dir=self.root, delete=False,
            ) as handle:
                handle.write(preview)
                temporary = Path(handle.name)
            result = self.analyzer.analyze(temporary)
            # Culling scores replace the raw Vision cues they are drawn from:
            # the cues include a subject mask that is far larger than the
            # verdicts and of no use once they have been computed.
            result["cull"] = self._cull_record(preview, result)
            if self._should_continue(generation):
                self.store.upsert(name, fingerprint, result, time.time(),
                                  culling.ANALYSIS_VERSION)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _cull_record(self, preview: bytes, result: dict) -> dict:
        """Culling verdicts, or nothing. A scoring failure must not cost the
        photo its searchable metadata, which is the index's real job."""
        try:
            return culling.analyze(preview, result)
        except Exception as error:
            with self._lock:
                self._last_error = f"culling: {str(error)[:160]}"
            return {}

    def _should_continue(self, generation: int) -> bool:
        return (
            not self._shutdown.is_set()
            and self.enabled
            and generation == self._generation
        )

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            self._generation += 1
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        shutdown = getattr(self.analyzer, "shutdown", None)
        if callable(shutdown):
            shutdown()
