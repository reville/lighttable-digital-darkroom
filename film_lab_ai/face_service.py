"""Optional local face matching with a finite, cancellable background scan."""

from __future__ import annotations

from server_localization import T

import hashlib
import threading
from pathlib import Path

import durable_io

from .face_models import FaceAnalyzer, MODEL_ID
from .face_store import FaceStore


class FaceService:
    def __init__(self, *, library: Path, data_root: Path, list_images,
                 source_key, preview_bytes, source_availability=lambda n: "local",
                 render_busy=lambda: False, worker_cleanup=lambda: None,
                 source_identity=lambda n: "", analyzer=None):
        identity = hashlib.sha256(str(library.resolve()).encode()).hexdigest()[:16]
        self.root = data_root / "People" / identity
        self.settings_path = self.root / "settings.json"
        self.store = FaceStore(self.root / "faces.sqlite3")
        self.analyzer = analyzer or FaceAnalyzer(data_root / "Face Models")
        self.list_images, self.source_key, self.preview_bytes = list_images, source_key, preview_bytes
        self.source_availability, self.render_busy = source_availability, render_busy
        self.worker_cleanup = worker_cleanup
        self.source_identity = source_identity
        self.lock = threading.RLock()
        self.thread = None
        self.stop = threading.Event()
        self.generation = 0
        self.progress = dict(running=False, phase="idle", completed=0, total=0,
                             downloaded=0, skipped=0, errors=0, lastError="", scanComplete=False)

    @property
    def enabled(self):
        settings = durable_io.load_json(self.settings_path, {})
        return bool(settings.get("enabled", False)) if isinstance(settings, dict) else False

    def status(self):
        with self.lock:
            return dict(self.progress, enabled=self.enabled, **self.store.stats(),
                        capabilities=self.analyzer.capabilities())

    def labels(self, names):
        return self.store.labels(names)

    def action(self, body):
        action = body.get("action")
        if action in ("enable", "disable", "scan", "clear"):
            with self.lock:
                if action in ("disable", "clear"):
                    self.generation += 1
                enabled = action in ("enable", "scan")
                durable_io.atomic_write_json(self.settings_path, {"enabled": enabled})
                if action == "clear":
                    self.store.clear()
                    self.progress.update(completed=0, total=0, skipped=0, errors=0,
                                         lastError="", scanComplete=False)
            if enabled:
                self.start()
        elif action in ("rename", "hide", "cover", "merge", "reject", "split", "undo"):
            with self.lock:
                self.store.edit(action, body)
        else:
            raise ValueError(T("Unknown face-matching action"))
        return self.status()

    def start(self):
        with self.lock:
            if not self.enabled or self.stop.is_set() or (self.thread and self.thread.is_alive()):
                return
            self.progress.update(running=True, phase="preparing", completed=0, total=0,
                                 downloaded=0, skipped=0, errors=0, lastError="", scanComplete=False)
            self.thread = threading.Thread(target=self._run, args=(self.generation,),
                                           name="lighttable-face-matching", daemon=True)
            self.thread.start()

    def _active(self, generation):
        return not self.stop.is_set() and generation == self.generation and self.enabled

    def _downloaded(self, count):
        with self.lock:
            self.progress.update(downloaded=count)

    def _run(self, generation):
        try:
            if not self.analyzer.capabilities()["available"]:
                raise RuntimeError(T("Face matching needs the OpenCV runtime. Rebuild LightTable with its current runtime requirements."))
            self.analyzer.prepare(lambda: not self._active(generation), self._downloaded)
            if not self._active(generation):
                return
            names = self.list_images()
            identities = {name: self.source_identity(name) for name in names}
            with self.lock:
                if not self._active(generation):
                    return
                self.store.reconcile_names(identities)
                self.store.remove_missing(names)
                self.progress.update(phase="scanning", total=len(names))
            for name in names:
                if not self._active(generation):
                    break
                for _ in range(25):
                    if not self.render_busy() or not self._active(generation):
                        break
                    self.stop.wait(0.2)
                try:
                    if not self._active(generation):
                        break
                    if self.source_availability(name) != "local":
                        with self.lock:
                            self.progress["skipped"] += 1
                        continue
                    fingerprint = self.source_key(name)
                    if self.store.current(name, fingerprint, MODEL_ID):
                        continue
                    faces = self.analyzer.analyze(self.preview_bytes(name))
                    with self.lock:
                        if self._active(generation):
                            self.store.add_photo(name, fingerprint, MODEL_ID, faces, identities[name])
                except Exception as error:
                    with self.lock:
                        self.progress["errors"] += 1
                        self.progress["lastError"] = f"{name}: {str(error)[:200]}"
                finally:
                    with self.lock:
                        self.progress["completed"] += 1
            with self.lock:
                self.progress["scanComplete"] = self._active(generation)
        except InterruptedError:
            pass
        except Exception as error:
            with self.lock:
                self.progress["lastError"] = str(error)[:300]
        finally:
            self.analyzer.shutdown()
            try:
                self.worker_cleanup()
            finally:
                with self.lock:
                    self.progress.update(running=False, phase="idle")
                    self.thread = None
                    restart = self.enabled and generation != self.generation
                if restart:
                    self.start()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            self.generation += 1
            thread = self.thread
        if thread:
            thread.join(timeout=3)
