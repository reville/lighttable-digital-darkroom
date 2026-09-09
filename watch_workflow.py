"""Settled-file watches for hot folders and camera-maker tether utilities.

The watcher never moves or deletes from its source. Files must hold the same
size and nanosecond modification time across two polls before metadata is read
and the file is registered or copied through the verified ingest path.
"""
from __future__ import annotations

from server_localization import T

import hashlib
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

import catalog as catalog_module
import catalog_scan
import ingest_workflow
import file_identity
import media_formats
import media_availability


POLL_SECONDS = 2.0
MAX_ARRIVALS_PER_POLL = 200


def clean_watch(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    path = str(raw.get("path") or "").strip()[:1000]
    mode = str(raw.get("mode") or "catalog").lower()
    if mode not in ("catalog", "ingest"):
        mode = "catalog"
    ident = str(raw.get("id") or "").strip()[:100]
    if not ident:
        ident = hashlib.sha256(
            f"{path}|{mode}|{time.time_ns()}".encode()).hexdigest()[:16]
    request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    return {
        "id": ident, "name": " ".join(str(raw.get("name") or
            Path(path).name or "Watched folder").split())[:80],
        "path": path, "enabled": raw.get("enabled", True) is not False,
        "recursive": bool(raw.get("recursive", False)), "mode": mode,
        "request": request, "presetId": str(raw.get("presetId") or "")[:100],
        "follow": bool(raw.get("follow", False)),
    }


class WatchService:
    def __init__(self, catalog: catalog_module.Catalog,
                 watches: Callable[[], list[dict]], *,
                 presets: Callable[[], list[dict]] | None = None,
                 render_busy: Callable[[], bool] | None = None,
                 activity=None,
                 poll_seconds: float = POLL_SECONDS):
        self.catalog = catalog
        self._watches = watches
        self._presets = presets or (lambda: [])
        self._render_busy = render_busy or (lambda: False)
        self._activity = activity or (lambda: nullcontext(True))
        self._poll_seconds = max(0.05, float(poll_seconds))
        self._candidates: dict[tuple[str, str], tuple[tuple[int, ...], int]] = {}
        self._handled_revisions: dict[tuple[str, str], tuple[int, ...]] = {}
        self._session_paths: set[Path] = set()
        self._status: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lighttable-watch-folders", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self._poll_seconds * 2))
        self._thread = None

    def add_session_path(self, path: Path | str) -> None:
        with self._lock:
            self._session_paths.add(Path(path).expanduser().resolve())

    @property
    def status(self) -> list[dict]:
        watches = [clean_watch(item) for item in self._watches()]
        with self._lock:
            return [dict({"enabled": watch["enabled"], "available": False,
                          "handled": 0, "lastArrival": None, "latest": None,
                          "error": ""}, **self._status.get(watch["id"], {}),
                         id=watch["id"], name=watch["name"],
                         follow=watch["follow"])
                    for watch in watches]

    def _paths(self, watch: dict):
        root = Path(watch["path"]).expanduser()
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if watch["recursive"]:
                            stack.append(Path(entry.path))
                        continue
                    if (not entry.is_file(follow_symlinks=False)
                            or Path(entry.name).suffix.lower()
                            not in media_formats.PHOTO_EXTENSIONS):
                        continue
                except OSError:
                    continue
                yield Path(entry.path)

    def _preset(self, ident: str) -> dict | None:
        if not ident:
            return None
        return next((item for item in self._presets()
                     if str(item.get("id")) == ident), None)

    def _apply_preset(self, image_id: int, preset: dict | None, *, connection=None) -> None:
        if not preset:
            return
        current = self.catalog.state_for(image_id)
        if preset.get("scope") == "look":
            from preset_library import look_patch
            patch = look_patch(preset)
            entry = {key: {**(current.get(key) or {}), **value} for key, value in patch.items()}
            if connection is None:
                self.catalog.save_state(image_id, entry)
            else:
                self.catalog._save_state(connection, image_id, entry)
            return
        entry: dict = {}
        if preset.get("includeFilm"):
            entry["params"] = preset.get("params") or {}
        grade = dict(current.get("grade") or {})
        source_grade = preset.get("grade") or {}
        for key in preset.get("includedGrade") or source_grade.keys():
            if key in source_grade:
                grade[key] = source_grade[key]
        if grade:
            entry["grade"] = grade
        for key in ("masks", "heals", "optics"):
            if preset.get(key):
                entry[key] = preset[key]
        if entry:
            if connection is None:
                self.catalog.save_state(image_id, entry)
            else:
                self.catalog._save_state(connection, image_id, entry)

    def _handle(self, watch: dict, path: Path, digest: str, *,
                expected_signature: str | None = None) -> tuple[int, str]:
        original = {"path": str(path), "content_signature": expected_signature
                    or file_identity.signature_key(path.stat(), path=path)}
        catalog_scan._validate_scan_identity(original)
        content_digest = digest.removeprefix("full:")
        if watch["mode"] == "catalog":
            image_id = catalog_scan.register_file(self.catalog, path,
                expected_signature=original["content_signature"],
                expected_content_hash=content_digest)
        else:
            item = ingest_workflow.describe_file(path)
            request = dict(watch.get("request") or {})
            request.setdefault("onDuplicate", "skip")
            plan = ingest_workflow.build_plan([item], request,
                                              existing_hashes=())
            if not plan["items"]:
                raise ValueError(T("the watched file did not produce an ingest copy"))
            copied = ingest_workflow.copy_item(
                plan["items"][0], verify=str(request.get("verify", "hash")))
            if not copied.get("ok"):
                raise RuntimeError(copied.get("error") or T("watched ingest failed"))
            catalog_scan._validate_scan_identity(original)
            path = Path(copied["destination"])
            image_id = catalog_scan.register_file(self.catalog, path,
                expected_content_hash=content_digest)
        preset = self._preset(watch["presetId"])
        # A watcher acknowledges exactly the bytes that settled. Registration
        # performs its expensive reads outside this short state/ledger writer.
        # Guard both the arrival and its destination so neither an old digest
        # nor its preset is published for a later replacement.
        with self.catalog.write() as conn:
            stored = conn.execute(
                "SELECT f.content_hash, f.content_signature FROM files f"
                " JOIN images i ON i.file_id=f.id WHERE i.id=?", (image_id,)).fetchone()
            if not stored or stored["content_hash"] != content_digest:
                raise OSError(T("watched file changed before acknowledgement: {path}", path=path))
            registered = {"path": str(path), "content_signature": stored["content_signature"]}
            catalog_scan._validate_scan_identity(original)
            catalog_scan._validate_scan_identity(registered)
            self._apply_preset(image_id, preset, connection=conn)
            conn.execute("INSERT OR IGNORE INTO watch_ledger(watch_id, header_hash, handled_at)"
                         " VALUES(?,?,?)", (watch["id"], digest, time.time()))
            catalog_scan._validate_scan_identity(original)
            catalog_scan._validate_scan_identity(registered)
        row = self.catalog.image_row(image_id)
        name = (catalog_module.qualified_name(row["source_id"], row["relpath"])
                if row else path.name)
        return image_id, name

    def _consider(self, watch: dict, path: Path) -> bool:
        key = (watch["id"], str(path))
        try:
            stat = path.stat()
        except OSError:
            return False
        if media_availability.from_stat(stat) != "local":
            self._candidates.pop(key, None)
            raise OSError(media_availability.cloud_message())
        signature = file_identity.stat_signature(stat, path=path)
        signature_key = ":".join(map(str, signature))
        if self._handled_revisions.get(key) == signature:
            return False
        previous, stable = self._candidates.get(key, (None, 0))
        stable = stable + 1 if previous == signature else 1
        self._candidates[key] = (signature, stable)
        if stable < 2:
            return False
        # A successful metadata read is the final settled-file gate.
        catalog_scan.read_metadata(path)
        digest = "full:" + file_identity.content_hash(path,
            expected_revision=(stat.st_size, stat.st_mtime_ns),
            expected_signature=signature_key)
        if self.catalog.watch_handled(watch["id"], digest):
            self._handled_revisions[key] = signature
            return False
        # Convert old prefix-only acknowledgements only when a complete copy
        # still exists. Otherwise a colliding new arrival must be handled.
        legacy_digest = ingest_workflow.header_hash(path)
        if self.catalog.watch_handled(watch["id"], legacy_digest):
            item = ingest_workflow.describe_file(path)
            known = self.catalog.ingest_content_hashes([item])
            if digest.removeprefix("full:") in known.get(legacy_digest, ()):
                original = {"path": str(path),
                            "content_signature": signature_key}
                with self.catalog.write() as conn:
                    catalog_scan._validate_scan_identity(original)
                    conn.execute("INSERT OR IGNORE INTO watch_ledger(watch_id, header_hash, handled_at)"
                                 " VALUES(?,?,?)", (watch["id"], digest, time.time()))
                    catalog_scan._validate_scan_identity(original)
                self._handled_revisions[key] = signature
                return False
        _, name = self._handle(watch, path, digest,
                               expected_signature=signature_key)
        self._handled_revisions[key] = signature
        now = time.time()
        with self._lock:
            status = self._status.setdefault(watch["id"], {})
            status.update(handled=int(status.get("handled", 0)) + 1,
                          lastArrival=now, latest=name, error="")
        return True

    def poll_once(self) -> list[dict]:
        watches = [clean_watch(item) for item in self._watches()]
        for watch in watches:
            if not watch["enabled"]:
                continue
            root = Path(watch["path"]).expanduser()
            available = root.is_dir()
            with self._lock:
                self._status.setdefault(watch["id"], {}).update(
                    enabled=True, available=available)
            if not available:
                continue
            arrivals = 0
            for path in self._paths(watch):
                try:
                    arrivals += int(self._consider(watch, path))
                except Exception as error:
                    with self._lock:
                        self._status.setdefault(watch["id"], {})["error"] = str(error)
                if arrivals >= MAX_ARRIVALS_PER_POLL:
                    break

        with self._lock:
            session_paths = list(self._session_paths)
        session_watch = clean_watch({"id": "session-external", "name": "External edits",
                                     "path": "", "mode": "catalog"})
        for path in session_paths:
            if path.is_file():
                try:
                    self._consider(session_watch, path)
                except Exception:
                    pass
        self.catalog.close()
        return self.status

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._activity() as allowed:
                if allowed and not self._render_busy():
                    self.poll_once()
            self._stop.wait(self._poll_seconds)
        self.catalog.close()
