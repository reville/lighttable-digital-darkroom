"""Crash markers, startup reporting, photo quarantine, and damaged-file custody.

Everything here is small and local on purpose.  The catalog's own integrity
work lives in ``catalog.py`` and the photo originals are never touched.  This
module records *what the process was doing* so that the next launch can tell an
interrupted session from a clean quit, set aside a photograph that keeps taking
the server down with it, and hand the native shell a specific reason when
startup fails instead of leaving it to time out.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import durable_io

# A photo is set aside after this many crashes were attributed to it.  One
# crash can be a coincidence; two on the same file while decoding it are not.
QUARANTINE_STRIKES = 2
# How long after a render completes the photo still gets the blame for a
# crash.  Longer than this and the process died doing something else.
INFLIGHT_SETTLE_SECONDS = 2.0
CRASH_LEDGER_LIMIT = 50
LOW_DISK_BYTES = 512 * 1024 * 1024
# Exit status the server uses to ask its launcher for a clean relaunch, as
# opposed to a crash (any other non-zero status) or a normal quit (zero).
EXIT_RESTART = 75

SYNC_SERVICE_MARKERS = (
    ("Dropbox", "Dropbox"),
    ("Mobile Documents", "iCloud Drive"),
    ("iCloud Drive", "iCloud Drive"),
    ("Google Drive", "Google Drive"),
    ("GoogleDrive", "Google Drive"),
    ("OneDrive", "OneDrive"),
    ("Box Sync", "Box"),
    ("Box", "Box"),
)


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")


def _write_json(path: Path, value: Any) -> bool:
    """Best-effort durable write; recovery bookkeeping must never raise."""
    for attempt in range(6):
        try:
            durable_io.atomic_write_json(path, value, keep_backup=False)
            return True
        except OSError as error:
            # A launcher polling the phase file, or a Windows virus scanner,
            # can briefly deny replacement. Losing the final ready record
            # leaves a healthy server looking stuck at its previous phase.
            # Keep permanent failures best-effort, with at most 310 ms of delay.
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 5:
                return False
            time.sleep(0.01 * 2 ** attempt)
    return False


# ----------------------------------------------------------------- startup --


def startup_report_path(default_directory: Path) -> Path:
    """Where the launcher expects to read startup phases for this process."""
    configured = os.environ.get("LIGHTTABLE_STARTUP_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(default_directory) / f"startup-{os.getpid()}.json"


class StartupReporter:
    """Write the server's startup phase where the launcher can read it.

    The native shell used to learn only one thing about a failed start: that a
    timeout elapsed.  With a phase file it can show "Opening the catalog…"
    while a large library is checked, stop waiting the moment the server
    reports a failure, and present the failure's reason and remedy instead of
    a generic message.
    """

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.record: dict = {
            "phase": "starting", "detail": "Starting LightTable…",
            "pid": os.getpid(), "startedAt": _now(), "updatedAt": _now(),
        }
        self._lock = threading.Lock()

    def phase(self, name: str, detail: str | None = None, **extra) -> None:
        with self._lock:
            self.record.update({"phase": name, "updatedAt": _now()}, **extra)
            if detail is not None:
                self.record["detail"] = detail
            self._write()

    def failed(self, code: str, message: str, *, hint: str | None = None,
               **extra) -> None:
        with self._lock:
            self.record.update({
                "phase": "failed", "code": code, "detail": message,
                "hint": hint, "updatedAt": _now(), **extra,
            })
            self._write()

    def ready(self, port: int) -> None:
        self.phase("ready", "Ready", port=int(port))

    def _write(self) -> None:
        if self.path is None:
            return
        _write_json(self.path, self.record)

    def remove(self) -> None:
        if self.path is None:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------- session --


class SessionLedger:
    """Know whether the previous run of this library ended on purpose.

    ``session.json`` is written when the server starts and completed when it
    ends.  A session file without an ending at the next launch means the
    process died; ``inflight.json`` then says what it was doing.  The ledger
    of crashes is appended to ``crashes.jsonl`` so a repeating failure is
    visible as a pattern rather than as a series of surprises.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.session_path = self.root / "session.json"
        self.inflight_path = self.root / "inflight.json"
        self.crashes_path = self.root / "crashes.jsonl"
        self.record: dict = {}
        self.previous: dict | None = None
        self._inflight: tuple[str, str] | None = None
        self._depth = 0
        self._settle: threading.Timer | None = None
        self._lock = threading.Lock()
        self._ended = False

    # --- lifecycle

    def begin(self, *, pid: int | None = None, port: int | None = None,
              revision: str | None = None,
              previous_exit: str | None = None) -> dict | None:
        """Record the new session; return details if the last one crashed."""
        previous = durable_io.load_json(self.session_path, None)
        inflight = durable_io.load_json(self.inflight_path, None)
        crashed = None
        if isinstance(previous, dict) and previous.get("startedAt") \
                and not previous.get("endedAt"):
            crashed = {
                "startedAt": previous.get("startedAt"),
                "pid": previous.get("pid"),
                "revision": previous.get("revision"),
                "inflight": inflight if isinstance(inflight, dict) else None,
                "exitStatus": previous_exit,
                "detectedAt": _now(),
            }
            self._append_crash(crashed)
        self.previous = crashed
        self.record = {
            "pid": pid if pid is not None else os.getpid(),
            "port": port, "revision": revision,
            "startedAt": _now(), "endedAt": None, "reason": None,
        }
        self._ended = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _write_json(self.session_path, self.record)
        try:
            self.inflight_path.unlink(missing_ok=True)
        except OSError:
            pass
        return crashed

    def end(self, reason: str = "quit") -> None:
        """Mark a deliberate ending. Safe to call more than once."""
        with self._lock:
            if self._ended or not self.record:
                return
            self._ended = True
            if self._settle is not None:
                self._settle.cancel()
                self._settle = None
            self.record.update({"endedAt": _now(), "reason": reason})
            _write_json(self.session_path, self.record)
            try:
                self.inflight_path.unlink(missing_ok=True)
            except OSError:
                pass

    # --- what the process is doing

    @contextmanager
    def inflight(self, stage: str, name: str) -> Iterator[None]:
        """Blame ``name`` for a crash while it is being processed.

        The marker is only rewritten when the photo changes, so a burst of
        slider renders on one photo costs one small write, and a decode
        nested inside a render of the same photo costs none.  The marker
        names the photo whose processing started most recently; previews
        render one at a time, so that is the one a crash interrupts.  After
        the outermost stage completes the marker is cleared on a short
        delay: a crash while idle is not this photograph's fault.
        """
        if not self.record:
            # No session has begun (library code imported by a tool or a
            # test), so there is nothing a later launch could attribute.
            yield
            return
        key = (str(stage), str(name))
        with self._lock:
            if self._settle is not None:
                self._settle.cancel()
                self._settle = None
            self._depth += 1
            if self._inflight is None or self._inflight[1] != key[1]:
                self._inflight = key
                _write_json(self.inflight_path,
                            {"stage": key[0], "name": key[1], "at": _now()})
        try:
            yield
        finally:
            with self._lock:
                self._depth = max(0, self._depth - 1)
                if self._depth == 0 and not self._ended \
                        and self._inflight is not None:
                    if self._settle is not None:
                        self._settle.cancel()
                    self._settle = threading.Timer(
                        INFLIGHT_SETTLE_SECONDS, self._clear_inflight,
                        (self._inflight,))
                    self._settle.daemon = True
                    self._settle.start()

    def _clear_inflight(self, key: tuple[str, str]) -> None:
        with self._lock:
            if self._inflight != key or self._depth:
                return
            self._inflight = None
            self._settle = None
            try:
                self.inflight_path.unlink(missing_ok=True)
            except OSError:
                pass

    def current_inflight(self) -> dict | None:
        with self._lock:
            if self._inflight is None:
                return None
            return {"stage": self._inflight[0], "name": self._inflight[1]}

    # --- ledger

    def _append_crash(self, entry: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            existing = self.crashes()
            existing.append(entry)
            existing = existing[-CRASH_LEDGER_LIMIT:]
            payload = "".join(json.dumps(item) + "\n" for item in existing)
            durable_io.atomic_write_text(self.crashes_path, payload)
        except OSError:
            pass

    def crashes(self) -> list[dict]:
        try:
            lines = self.crashes_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        entries = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries

    def crash_count(self, window_seconds: float) -> int:
        cutoff = _now() - max(0.0, float(window_seconds))
        return sum(1 for item in self.crashes()
                   if float(item.get("detectedAt") or 0) >= cutoff)

    def summary(self) -> dict:
        crashes = self.crashes()
        return {
            "previousCrash": self.previous,
            "crashes24h": sum(1 for item in crashes
                              if float(item.get("detectedAt") or 0)
                              >= _now() - 24 * 3600),
            "recentCrashes": crashes[-5:],
            "startedAt": self.record.get("startedAt"),
            "inflight": self.current_inflight(),
        }


# -------------------------------------------------------------- quarantine --


class PhotoQuarantine:
    """Photographs set aside because processing them crashed the server.

    A RAW file that upsets the decoder takes the whole Python process with
    it.  Without this list the app would crash again every time the grid
    scrolled past that file.  Entries are keyed by the catalog name, and the
    user can release one from Library Health once the cause is understood.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._items: dict[str, dict] | None = None
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict]:
        if self._items is None:
            loaded = durable_io.load_json(self.path, {})
            self._items = {
                str(name): dict(entry)
                for name, entry in (loaded.items()
                                    if isinstance(loaded, dict) else ())
                if isinstance(entry, dict)
            }
        return self._items

    def _save(self) -> None:
        _write_json(self.path, self._load())

    def strike(self, name: str, stage: str = "render", *,
               at: float | None = None) -> dict:
        with self._lock:
            items = self._load()
            entry = items.setdefault(str(name), {"strikes": 0})
            entry["strikes"] = int(entry.get("strikes", 0)) + 1
            entry["stage"] = str(stage)
            entry["lastAt"] = float(at if at is not None else _now())
            entry["quarantined"] = entry["strikes"] >= QUARANTINE_STRIKES
            self._save()
            return dict(entry, name=str(name))

    def note_previous_crash(self, crashed: dict | None) -> dict | None:
        """Attribute an interrupted session to the photo it was processing."""
        inflight = (crashed or {}).get("inflight") if crashed else None
        if not isinstance(inflight, dict) or not inflight.get("name"):
            return None
        return self.strike(str(inflight["name"]),
                           str(inflight.get("stage") or "render"),
                           at=crashed.get("detectedAt"))

    def is_quarantined(self, name: str) -> bool:
        with self._lock:
            entry = self._load().get(str(name))
            return bool(entry and entry.get("quarantined"))

    def release(self, name: str) -> bool:
        with self._lock:
            items = self._load()
            if str(name) not in items:
                return False
            del items[str(name)]
            self._save()
            return True

    def entries(self) -> list[dict]:
        with self._lock:
            return [dict(entry, name=name)
                    for name, entry in sorted(self._load().items())]

    def quarantined(self) -> list[str]:
        return [entry["name"] for entry in self.entries()
                if entry.get("quarantined")]


# ------------------------------------------------------- damaged file custody --


def quarantine_directory(root: Path | str, label: str = "") -> Path:
    """A fresh, dated folder under ``Recovery`` that nothing else owns."""
    base = Path(root) / "Recovery"
    for attempt in range(50):
        name = _stamp() + (f"-{label}" if label else "")
        if attempt:
            name += f"-{attempt}"
        candidate = base / name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("could not create a recovery folder")


def set_aside(path: Path | str, root: Path | str, *, reason: str,
              label: str = "") -> Path | None:
    """Move a damaged file (and SQLite siblings) into a Recovery folder.

    The bytes are preserved for diagnosis; nothing is ever deleted here.
    Returns the folder, or None when there was nothing to move.
    """
    path = Path(path)
    candidates = [Path(str(path) + suffix) for suffix in ("", "-wal", "-shm")]
    present = [item for item in candidates if item.exists()]
    if not present:
        return None
    folder = quarantine_directory(root, label or path.stem)
    for item in present:
        try:
            os.replace(item, folder / item.name)
        except OSError:
            shutil.move(str(item), str(folder / item.name))
    try:
        (folder / "REASON.txt").write_text(
            f"{datetime.now().isoformat(timespec='seconds')}\n"
            f"Original: {path}\nReason: {reason}\n", encoding="utf-8")
    except OSError:
        pass
    return folder


def inspect_json(path: Path | str) -> dict:
    """Say which copy of a JSON document is in use without changing it."""
    path = Path(path)
    backup = durable_io.backup_path(path)

    def readable(candidate: Path) -> bool:
        try:
            json.loads(candidate.read_text(encoding="utf-8"))
            return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False

    if not path.exists() and not backup.exists():
        return {"path": str(path), "status": "missing"}
    if readable(path):
        return {"path": str(path), "status": "ok"}
    if readable(backup):
        return {"path": str(path), "status": "backup",
                "message": "The file could not be read; its last valid "
                           "backup is in use."}
    return {"path": str(path), "status": "damaged",
            "message": "Neither the file nor its backup could be read; "
                       "defaults are in use."}


def preserve_damaged_json(path: Path | str, root: Path | str | None = None,
                          *, reason: str = "unreadable JSON") -> Path | None:
    """Before a rewrite replaces an unreadable document, keep its bytes."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        return set_aside(path, root or path.parent, reason=reason)
    except OSError:
        return None


# --------------------------------------------------------------- environment --


def disk_status(path: Path | str) -> dict:
    """Free space on the volume holding ``path``; low means stop growing."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as error:
        return {"path": str(path), "error": str(error), "low": False}
    return {
        "path": str(path), "freeBytes": int(usage.free),
        "totalBytes": int(usage.total),
        "low": usage.free < LOW_DISK_BYTES,
    }


def sync_service_for(path: Path | str) -> str | None:
    """Name the file-sync service a path lives in, when one is recognisable.

    SQLite under a syncing folder is a well-known way to lose a database, so
    Library Health calls it out rather than waiting for the damage.
    """
    parts = Path(path).expanduser().parts
    for part in parts:
        for marker, label in SYNC_SERVICE_MARKERS:
            if part == marker or (marker == "Dropbox" and "Dropbox" in part):
                return label
    if len(parts) > 1 and parts[0] == "/" and parts[1] == "Volumes":
        return None
    return None


def log_tail(path: Path | str | None, lines: int = 80) -> list[str]:
    if not path:
        return []
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return text.splitlines()[-max(1, int(lines)):]


def rotate_log(path: Path | str, keep: int = 3) -> None:
    """Shift ``name.log`` to ``name.1.log`` … so a launch never erases the
    evidence of the previous one.  Used by launchers that have no shell."""
    path = Path(path)
    if not path.exists():
        return
    try:
        if path.stat().st_size == 0:
            return
        for index in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.stem}.{index}{path.suffix}")
            newer = (path if index == 1
                     else path.with_name(f"{path.stem}.{index - 1}{path.suffix}"))
            if newer.exists():
                os.replace(newer, older)
    except OSError:
        pass
