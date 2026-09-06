#!/usr/bin/env python
"""LightTable: local photo library and spektrafilm render server.

Serves one local source root (LIGHTTABLE_DIR), including its folder hierarchy.
Previews render in-process so numba/LUT caches stay warm; full-resolution
exports run as subprocesses.
"""
from __future__ import annotations

import atexit
import copy
import faulthandler
import hashlib
import io
import json
import os
import queue
import secrets
import shutil
import select
import signal
import struct
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import numpy as np
from PIL import Image, ImageStat

if os.environ.get("LIGHTTABLE_DIAGNOSTICS"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import film_pipeline as fp  # noqa: E402
import grade  # noqa: E402
import edits  # noqa: E402
import color_pipeline  # noqa: E402
import calibration_target  # noqa: E402
import preset_io  # noqa: E402
import export_workflow  # noqa: E402
import library_workflow  # noqa: E402
import catalog as catalog_module  # noqa: E402
import catalog_scan  # noqa: E402
import watch_workflow  # noqa: E402
import media_formats  # noqa: E402
import durable_io  # noqa: E402
import recovery  # noqa: E402
from film_lab_ai import AIIndexService  # noqa: E402
from film_lab_ai.providers import LocalPhotoAnalyzer, VisionProvider  # noqa: E402
import platform_image  # noqa: E402
from events import EventBroker, encode_sse  # noqa: E402
from jobs import JobRegistry  # noqa: E402
from validation import ValidationError, clean_state_patch  # noqa: E402
from render_scheduling import LatestWorkQueue, PriorityGate, RenderCancelled  # noqa: E402

FOLDER = Path(os.environ.get("LIGHTTABLE_DIR", "")).expanduser()
PORT = int(os.environ.get("LIGHTTABLE_PORT", "8321"))
BOUND_HOST = "127.0.0.1"
STARTED_AT = time.time()
INSTANCE_TOKEN = secrets.token_urlsafe(32)
EXPORT_DIR_NAME = "film-exports"
SESSION_EXPORT_DESTINATIONS: set[Path] = set()

EVENTS = EventBroker(maximum_subscribers=8)
JOBS = JobRegistry(on_change=lambda record: EVENTS.publish("job", record))
REQUEST_LOG_LOCK = threading.Lock()
UI_STATE_LOCK = threading.RLock()
UI_STATE: dict = {}
UI_PENDING_LOCK = threading.RLock()
UI_PENDING: dict[str, dict] = {}


class APIError(RuntimeError):
    """An intentional HTTP error with a stable machine-readable code."""

    def __init__(self, status: int, message: str, code: str, *,
                 field: str | None = None, details=None) -> None:
        self.status = int(status)
        self.code = str(code)
        self.field = field
        self.details = details
        super().__init__(message)


def instance_directory() -> Path:
    configured = os.environ.get("LIGHTTABLE_INSTANCE_DIR")
    if configured:
        return Path(configured).expanduser()
    if IS_WINDOWS:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return root / "LightTable" / "instances"
    return Path.home() / "Library/Application Support/LightTable/instances"


def instance_path(port: int | None = None) -> Path:
    return instance_directory() / f"{int(port if port is not None else PORT)}.json"

CACHE = Path(os.environ.get("LIGHTTABLE_CACHE_DIR", str(APP / "cache"))).expanduser()
for sub in ("tiff", "neutral", "render", "edit", "orig", "thumb", "rust",
            "export-film", "semantic"):
    (CACHE / sub).mkdir(parents=True, exist_ok=True)

# Every format the bundled LibRaw decodes. The eight original entries covered a
# fraction of its 1275 cameras, so bodies from Pentax, Hasselblad, Phase One,
# Leica, Samsung, and older Canon were refused by the whitelist rather than by
# the decoder. Foveon .x3f stays out until its demosaic is verified.
# `scripts/camera-list.py` generates the canonical site's support list from
# the linked library, so this set and that page cannot drift apart silently.
RAW_EXTS = media_formats.RAW_EXTENSIONS
# Video is catalogued, searched, and played, but never edited: the film and
# grade pipelines are stills-only and pretending otherwise would be worse than
# leaving these files out.
VIDEO_EXTS = media_formats.VIDEO_EXTENSIONS
PROCESSED_EXTS = media_formats.PROCESSED_EXTENSIONS
EXTS = media_formats.PHOTO_EXTENSIONS
IS_WINDOWS = os.name == "nt"
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def is_raw(name: str) -> bool:
    return Path(library_workflow.source_name(name)).suffix.lower() in RAW_EXTS


def is_video(name: str) -> bool:
    return Path(library_workflow.source_name(name)).suffix.lower() in VIDEO_EXTS


# ------------------------------------------------------------- catalog ----
#
# The catalog spans every source folder and lives in Application Support, so a
# read-only volume, a network share, or a folder someone else owns can all be
# browsed. `LIGHTTABLE_DIR` still works and still means "open this folder":
# it is registered as a source at startup. Setting LIGHTTABLE_CATALOG=0 keeps
# the pre-catalog per-folder behaviour, which is what the older state tests
# exercise.

CATALOG_ENABLED = os.environ.get("LIGHTTABLE_CATALOG", "1") != "0"
CATALOG_MIRROR = os.environ.get("LIGHTTABLE_CATALOG_MIRROR", "1") != "0"
# Safe mode opens the library with every background service switched off:
# no scan, no watched folders, no local AI indexing, no engine warm-up. It is
# what the shell offers after repeated crashes so a person can reach Library
# Health and fix things without the app doing anything on its own.
SAFE_MODE = os.environ.get("LIGHTTABLE_SAFE_MODE") == "1"
# Headless and command-line servers have nobody to ask, so they may restore
# the newest verified backup on their own the way the app used to.
AUTO_RECOVER = os.environ.get("LIGHTTABLE_AUTO_RECOVER") == "1"
CATALOG: "catalog_module.Catalog | None" = None
SCANNER: "catalog_scan.ScanService | None" = None
PRIMARY_SOURCE_ID: int | None = None
CATALOG_NOTICE: dict | None = None
LAUNCH_NOTICE: dict | None = None
WATCH_SERVICE: "watch_workflow.WatchService | None" = None
CATALOG_PROCESS_LOCK = None
HTTPD: ThreadingHTTPServer | None = None
RESTART_REQUESTED = False
RESTART_LOCK = threading.Lock()
# The reporter learns its file in main(); importing the module writes nothing.
STARTUP = recovery.StartupReporter(None)
SESSION = recovery.SessionLedger(catalog_module.default_catalog_path().parent)
PHOTO_QUARANTINE = recovery.PhotoQuarantine(
    catalog_module.default_catalog_path().parent / "photo-quarantine.json")
HEALTH_LOCK = threading.Lock()
HEALTH_STATE: dict = {"verify": None, "maintenance": None, "disk": None}
MAINTENANCE_INTERVAL = float(os.environ.get(
    "LIGHTTABLE_MAINTENANCE_INTERVAL", str(15 * 60)))
FULL_VERIFY_INTERVAL = 24 * 60 * 60


class CatalogLockedError(RuntimeError):
    """Another live server holds this catalog's process lease."""

    def __init__(self, message: str, holder: dict | None = None) -> None:
        super().__init__(message)
        self.holder = holder


def _catalog_lease_holder(catalog_path: Path) -> dict | None:
    """Find the registered instance that has this catalog open, if any."""
    wanted = str(catalog_path)
    try:
        candidates = sorted(instance_directory().glob("*.json"))
    except OSError:
        return None
    for path in candidates:
        record = durable_io.load_json(path, None)
        if not isinstance(record, dict) or record.get("catalog") != wanted:
            continue
        pid = record.get("pid")
        try:
            os.kill(int(pid), 0)
        except (TypeError, ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass  # exists, owned by another user: still live
        return {"pid": int(pid), "port": record.get("port"),
                "folder": record.get("folder"),
                "headless": bool(record.get("headless")),
                "url": f"http://{record.get('host', BOUND_HOST)}:"
                       f"{record.get('port')}/"}
    return None


def acquire_catalog_process_lock() -> None:
    """Hold an OS-backed lease so two servers cannot write one catalog."""
    global CATALOG_PROCESS_LOCK
    if not CATALOG_ENABLED or CATALOG_PROCESS_LOCK is not None:
        return
    catalog_path = catalog_module.default_catalog_path()
    lock_path = catalog_path.with_suffix(".sqlite3.process-lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if IS_WINDOWS:
            import msvcrt
            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        holder = _catalog_lease_holder(catalog_path)
        raise CatalogLockedError(
            f"the catalog is already open by another LightTable server: "
            f"{catalog_path}", holder) from error
    CATALOG_PROCESS_LOCK = handle


def catalog_handle() -> "catalog_module.Catalog | None":
    """The open catalog, or None when running in per-folder mode."""
    return CATALOG


def _damaged_catalog_notice(catalog_path: Path, error: Exception) -> dict:
    backups = catalog_module.list_backups(
        _backup_directory_for(catalog_path), summarize=0)
    return {
        "status": "damaged",
        "message": str(error),
        "path": str(catalog_path),
        "backups": len(backups),
        "recovery": (
            "Photos are open in folder mode and the damaged catalog has not "
            "been changed. Open Library Health to salvage it, restore a "
            "backup, or start a new catalog."),
    }


def _backup_directory_for(catalog_path: Path) -> Path:
    raw = " ".join(str(load_preferences().get(
        "backupDirectory", "")).split()).strip()
    return Path(raw).expanduser() if raw else catalog_path.parent / "Backups"


def open_catalog() -> "catalog_module.Catalog | None":
    """Open the catalog and make the launch folder one of its sources.

    A damaged catalog is reported, not replaced: restoring a backup discards
    every edit made since it was taken, and only the person whose library it
    is can decide that. The app opens in folder mode with the damage intact
    and Library Health offers the choices. Headless servers opt back into
    automatic recovery with ``LIGHTTABLE_AUTO_RECOVER=1``.
    """
    global CATALOG, SCANNER, PRIMARY_SOURCE_ID, CATALOG_NOTICE
    if not CATALOG_ENABLED:
        return None
    if CATALOG is not None:
        return CATALOG
    SCANNER = None
    PRIMARY_SOURCE_ID = None
    CATALOG_NOTICE = None
    catalog_path = catalog_module.default_catalog_path()
    STARTUP.phase("catalog", "Opening the catalog…")
    try:
        CATALOG = catalog_module.Catalog(catalog_path)
        STARTUP.phase("catalog", "Checking the catalog…")
        if not CATALOG.quick_check_ok():
            raise RuntimeError("catalog failed its integrity check")
    except Exception as error:
        if CATALOG is not None:
            try:
                CATALOG.close()
            except Exception:
                pass
        CATALOG = None
        if isinstance(error, catalog_module.CatalogVersionError):
            # A newer catalog is not damaged. Never replace or quarantine it
            # merely because an older application cannot read its schema.
            CATALOG_NOTICE = {
                "status": "incompatible",
                "message": str(error),
                "recovery": "The catalog was left unchanged.",
            }
            print(f"LightTable: {error}; falling back to folder mode")
            return None
        if not AUTO_RECOVER:
            CATALOG_NOTICE = _damaged_catalog_notice(catalog_path, error)
            print(f"LightTable: catalog damaged ({error}); opening in folder "
                  "mode until a recovery choice is made in Library Health")
            return None
        try:
            recovered = catalog_module.recover_latest_backup(catalog_path)
            if recovered:
                CATALOG = catalog_module.Catalog(catalog_path)
                if not CATALOG.integrity_ok():
                    raise RuntimeError("restored catalog failed its integrity check")
                CATALOG_NOTICE = {"status": "recovered", **recovered}
                print("LightTable: restored the catalog from a verified backup; "
                      f"the damaged files are in {recovered['quarantine']}")
            else:
                raise RuntimeError("no valid catalog backup is available")
        except Exception as recovery_error:
            # A damaged catalog must not stop the app from opening photographs.
            CATALOG_NOTICE = {
                "status": "unavailable",
                "message": str(error),
                "recovery": str(recovery_error),
            }
            print(f"LightTable: catalog unavailable ({error}); "
                  f"falling back to folder mode ({recovery_error})")
            CATALOG = None
            return None
    if FOLDER.is_dir():
        PRIMARY_SOURCE_ID = CATALOG.add_source(FOLDER)
    SCANNER = catalog_scan.ScanService(CATALOG, render_busy=RENDER_LOCK.locked)
    return CATALOG


def guard_photo(name: str) -> None:
    """Refuse to process a photo that has been set aside after crashes."""
    source = library_workflow.source_name(name)
    if PHOTO_QUARANTINE.is_quarantined(source):
        raise APIError(
            423, "this photo was set aside after it crashed LightTable "
                 "repeatedly; release it from Library Health to try again",
            "quarantined", details={"name": source})


def request_restart(reason: str = "recovery") -> None:
    """Stop serving so the launcher relaunches a fresh process.

    The exit status tells the shell this was asked for, so it relaunches
    immediately without counting it as a crash.  Headless servers simply
    exit; the person or tool that started them starts them again.
    """
    global RESTART_REQUESTED
    with RESTART_LOCK:
        if RESTART_REQUESTED:
            return
        RESTART_REQUESTED = True
    print(f"LightTable: restarting ({reason})")
    SESSION.end(f"restart:{reason}")

    def shut_down() -> None:
        time.sleep(0.35)  # let the response that asked for this go out first
        if HTTPD is not None:
            HTTPD.shutdown()
    threading.Thread(target=shut_down, daemon=True,
                     name="lighttable-restart").start()


def source_root(source_id: int) -> Path | None:
    cat = catalog_handle()
    if cat is None:
        return None
    source = cat.source_by_id(source_id)
    return Path(source["path"]) if source else None


def resolve_name(name: str) -> tuple[Path, int | None, str, str | None]:
    """Resolve an API image name to (path, source_id, relpath, copy_ident).

    Accepts both the qualified `4:trip/frame.RAF` form and the legacy form
    relative to the launch folder, so a client that has not been updated keeps
    working against the same server.
    """
    source_id, relpath, copy_ident = catalog_module.parse_name(name)
    relpath = library_workflow.source_name(relpath)
    if source_id is not None:
        root = source_root(source_id)
        if root is None:
            raise ValueError(f"unknown source in name: {name}")
    else:
        root = FOLDER
    resolved = (root / relpath).resolve()
    root = root.resolve()
    if root not in resolved.parents or not resolved.is_file() \
            or resolved.suffix.lower() not in EXTS:
        raise ValueError(f"bad image name: {name}")
    return resolved, source_id, relpath, copy_ident


def catalog_image_id(name: str) -> int | None:
    cat = catalog_handle()
    if cat is None:
        return None
    source_id, relpath, copy_ident = catalog_module.parse_name(name)
    if source_id is None:
        source_id = PRIMARY_SOURCE_ID
        relpath = library_workflow.source_name(relpath)
        if catalog_module.VIRTUAL_MARKER in name:
            copy_ident = name.split(catalog_module.VIRTUAL_MARKER, 1)[1]
    if source_id is None:
        return None
    return cat.image_id_for(source_id, library_workflow.source_name(relpath),
                            copy_ident)

# ---------------------------------------------------------------- state ----

STATE_LOCK = threading.RLock()
PREFS_LOCK = threading.RLock()
PRESETS_LOCK = threading.RLock()


def clean_crop(c):
    """Normalised crop rect {x,y,w,h} in 0..1, or None for the full frame."""
    if not c:
        return None
    try:
        x = max(0.0, min(1.0, float(c.get("x", 0.0))))
        y = max(0.0, min(1.0, float(c.get("y", 0.0))))
        w = max(0.01, min(1.0 - x, float(c.get("w", 1.0))))
        h = max(0.01, min(1.0 - y, float(c.get("h", 1.0))))
    except (TypeError, ValueError):
        return None
    if x == 0.0 and y == 0.0 and w == 1.0 and h == 1.0:
        return None
    return {"x": round(x, 5), "y": round(y, 5),
            "w": round(w, 5), "h": round(h, 5)}


def clean_keywords(values) -> list[str]:
    """Short, unique, user-authored search tags for one photo."""
    if not isinstance(values, list):
        return []
    out = []
    seen = set()
    for value in values[:100]:
        text = " ".join(str(value).split()).strip()[:60]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def clean_versions(values) -> list[dict]:
    """Validate named, non-destructive editing checkpoints."""
    if not isinstance(values, list):
        return []
    out = []
    for raw in values[:50]:
        if not isinstance(raw, dict):
            continue
        name = " ".join(str(raw.get("name", "")).split()).strip()[:80]
        if not name:
            continue
        ident = str(raw.get("id", ""))[:100] or hashlib.md5(
            (name + str(raw.get("created", ""))).encode()).hexdigest()
        out.append({
            "id": ident,
            "name": name,
            "created": str(raw.get("created", ""))[:40],
            "params": fp.clean_params(raw.get("params", {})),
            "grade": grade.clean(raw.get("grade", {})),
            "crop": clean_crop(raw.get("crop")),
            "masks": edits.clean_masks(raw.get("masks")),
            "heals": edits.clean_heals(raw.get("heals")),
            "optics": edits.clean_optics(raw.get("optics")),
            "provenance": raw.get("provenance") or renderer_provenance(),
        })
    return out


def entry_for(st, name):
    e = st["images"].get(name, {})
    return {
        "status": e.get("status", "pending"),
        "rating": int(e.get("rating", 0) or 0),
        "params": e.get("params"),
        "grade": e.get("grade"),
        "crop": e.get("crop"),
        "masks": edits.clean_masks(e.get("masks")),
        "heals": edits.clean_heals(e.get("heals")),
        "optics": edits.clean_optics(e.get("optics")),
        "keywords": clean_keywords(e.get("keywords", [])),
        "versions": clean_versions(e.get("versions", [])),
        "provenance": e.get("provenance"),
        "label": clean_label(e.get("label")),
    }


PRESETS_FILE = Path(os.environ.get(
    "LIGHTTABLE_PRESETS_FILE", str(APP / "presets.json"))).expanduser()
PREFS_FILE = Path(os.environ.get(
    "LIGHTTABLE_PREFS_FILE", str(APP / "prefs.json"))).expanduser()
AI_DATA_ROOT = Path(os.environ.get(
    "LIGHTTABLE_AI_DIR",
    str((PREFS_FILE.parent / "AI Index")
        if "LIGHTTABLE_PREFS_FILE" in os.environ
        else Path.home() / "Library/Application Support/LightTable/AI Index"),
)).expanduser()
VISION_HELPER = Path(os.environ.get(
    "LIGHTTABLE_VISION_HELPER", str(APP / "build/LightTableVision"),
)).expanduser()
VISION_PROVIDER = VisionProvider(VISION_HELPER)
AI_INDEX: AIIndexService | None = None


def load_json_file(p: Path, default):
    return durable_io.load_json(p, default)


def load_export_recipes() -> list[dict]:
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        custom = prefs.get("exportRecipes", []) if isinstance(prefs, dict) else []
        return export_workflow.all_recipes(custom)


def load_preferences() -> dict:
    """Return the durable preference object, never a malformed root value."""
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        return dict(prefs) if isinstance(prefs, dict) else {}


def effective_new_photo_defaults() -> tuple[dict, dict]:
    """Resolve the default edit assigned only when a photo has no saved state."""
    params = dict(fp.DEFAULT_PARAMS)
    default_grade = dict(grade.DEFAULTS)
    prefs = load_preferences()
    configured = prefs.get("newPhotoDefaults")
    configured = configured if isinstance(configured, dict) else {}
    preset_name = " ".join(str(configured.get("preset", "")).split())
    if preset_name:
        preset = next((item for item in load_presets()
                       if item["name"] == preset_name), None)
        if preset:
            params.update(preset.get("params") or {})
            default_grade.update(preset.get("grade") or {})
    # Film simulation is opt-in for new photos, even when a default preset
    # carries a Film state. Keep DEFAULT_PARAMS unchanged so older saved edits
    # that predate profile_enabled retain their look.
    params["profile_enabled"] = configured.get("filmEnabled") is True
    workflow = str(configured.get("workflow", ""))
    if workflow in ("authentic", "creative"):
        params["workflow_mode"] = workflow
    develop_profile = str(configured.get("developProfile", ""))
    if develop_profile in ("standard", "linear"):
        params["developProfile"] = develop_profile
    return fp.clean_params(params), grade.clean(default_grade)


def configured_backup_directory(cat) -> Path:
    raw = " ".join(str(load_preferences().get(
        "backupDirectory", "")).split()).strip()
    return Path(raw).expanduser() if raw else cat.path.parent / "Backups"


def automatic_backup_max_age() -> float | None:
    frequency = str(load_preferences().get("backupFrequency", "daily"))
    return {"hourly": 60 * 60, "daily": 24 * 60 * 60,
            "weekly": 7 * 24 * 60 * 60}.get(frequency)


def configured_watches() -> list[dict]:
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        raw = prefs.get("watches", []) if isinstance(prefs, dict) else []
    return [watch_workflow.clean_watch(item) for item in raw[:100]
            if isinstance(item, dict)]


def watch_action(body: dict) -> dict:
    action = str(body.get("action", "list"))
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        if not isinstance(prefs, dict):
            prefs = {}
        watches = [watch_workflow.clean_watch(item)
                   for item in (prefs.get("watches") or [])[:100]
                   if isinstance(item, dict)]
        if action == "save":
            watch = watch_workflow.clean_watch(body.get("watch"))
            if not watch["path"]:
                raise ValueError("choose a folder to watch")
            watches = [item for item in watches if item["id"] != watch["id"]]
            watches.append(watch)
        elif action == "delete":
            ident = str(body.get("id", ""))
            watches = [item for item in watches if item["id"] != ident]
        elif action != "list":
            raise ValueError("unknown watch action")
        if action != "list":
            prefs["watches"] = watches
            durable_io.atomic_write_json(PREFS_FILE, prefs)
    return {"ok": True, "watches": watches,
            "status": WATCH_SERVICE.status if WATCH_SERVICE else []}


def update_export_recipes(body: dict) -> list[dict]:
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        if not isinstance(prefs, dict):
            prefs = {}
        custom = export_workflow.clean_custom_recipes(
            prefs.get("exportRecipes", []))
        action = body.get("action")
        if action == "save":
            raw = dict(body.get("recipe") or {})
            if not raw.get("id"):
                raw["id"] = hashlib.sha256(
                    f"{raw.get('name')}|{time.time_ns()}".encode()
                ).hexdigest()[:16]
            recipe = export_workflow.clean_recipe(raw)
            recipe["builtin"] = False
            custom = [item for item in custom if item["id"] != recipe["id"]]
            custom.append(recipe)
        elif action == "delete":
            ident = str(body.get("id", ""))
            custom = [item for item in custom if item["id"] != ident]
        else:
            raise ValueError("unknown export-recipe action")
        prefs["exportRecipes"] = custom
        durable_io.atomic_write_json(PREFS_FILE, prefs)
        return export_workflow.all_recipes(custom)


def clean_preset(raw: dict) -> dict | None:
    """Normalise old LightTable presets and imported partial presets."""
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name", "")).split()).strip()[:120]
    if not name:
        return None
    source = str(raw.get("source", "lighttable"))[:40]
    preset_type = "tool" if raw.get("presetType") == "tool" else "style"
    # Presets saved before scopes existed always contained the complete film
    # recipe, so retain that behaviour during migration.
    include_film = bool(raw.get("includeFilm", "includeFilm" not in raw))
    grade_raw = raw.get("grade") if isinstance(raw.get("grade"), dict) else {}
    cleaned_grade = grade.clean(grade_raw)
    valid_grade_keys = set(grade.DEFAULTS) | set(grade.CURVE_KEYS) | \
        {"hsl", *grade.ADVANCED_KEYS}
    supplied = raw.get("includedGrade")
    if isinstance(supplied, list):
        included_grade = [str(key) for key in supplied
                          if str(key) in valid_grade_keys]
    elif preset_type == "tool":
        included_grade = [
            key for key in grade.DEFAULTS
            if abs(cleaned_grade.get(key, grade.DEFAULTS[key])
                   - grade.DEFAULTS[key]) > 1e-6
        ]
        included_grade += [key for key in (*grade.CURVE_KEYS, "hsl", *grade.ADVANCED_KEYS)
                           if key in cleaned_grade]
    else:
        included_grade = list(grade.DEFAULTS)
        included_grade += [key for key in (*grade.CURVE_KEYS, "hsl", *grade.ADVANCED_KEYS)
                           if key in cleaned_grade]
    included_grade = list(dict.fromkeys(included_grade))
    conversion = raw.get("conversion") if isinstance(raw.get("conversion"), dict) else {}
    ignored = conversion.get("ignored") if isinstance(conversion.get("ignored"), list) else []
    notes = conversion.get("notes") if isinstance(conversion.get("notes"), list) else []
    return {
        "id": str(raw.get("id") or hashlib.md5(name.encode()).hexdigest())[:100],
        "name": name,
        "source": source,
        "presetType": preset_type,
        "includeFilm": include_film,
        "recommendedFilmOff": bool(raw.get(
            "recommendedFilmOff", source != "lighttable" and not include_film)),
        "params": fp.clean_params(raw.get("params", {})) if include_film else {},
        "grade": cleaned_grade,
        "includedGrade": included_grade,
        "masks": edits.clean_masks(raw.get("masks")),
        "heals": edits.clean_heals(raw.get("heals")),
        "optics": edits.clean_optics(raw.get("optics")),
        "conversion": {
            "mapped": len(included_grade),
            "ignored": [str(value)[:100] for value in ignored[:100]],
            "notes": [str(value)[:240] for value in notes[:20]],
        },
    }


def load_presets() -> list[dict]:
    with PRESETS_LOCK:
        items = load_json_file(PRESETS_FILE, [])
        if not isinstance(items, list):
            return []
        return [cleaned for item in items if (cleaned := clean_preset(item))]


def save_presets(items: list[dict]) -> list[dict]:
    with PRESETS_LOCK:
        cleaned = [preset for item in items if (preset := clean_preset(item))]
        cleaned.sort(key=lambda item: item["name"].casefold())
        durable_io.atomic_write_json(PRESETS_FILE, cleaned)
        return cleaned


def merge_imported_presets(current: list[dict], imported: list[dict]) -> list[dict]:
    """Append imports with stable, human-readable duplicate names."""
    with PRESETS_LOCK:
        taken = {item["name"].casefold() for item in current}
        for raw in imported:
            item = clean_preset(raw)
            if not item:
                continue
            base = item["name"]
            candidate = base
            suffix = 2
            while candidate.casefold() in taken:
                candidate = f"{base} ({suffix})"
                suffix += 1
            item["name"] = candidate
            item["id"] = str(raw.get("id") or hashlib.md5(
                (candidate + str(time.time_ns())).encode()).hexdigest())
            taken.add(candidate.casefold())
            current.append(item)
        return save_presets(current)


def state_path() -> Path:
    return FOLDER / ".lighttable-state.json"


_STATE_CACHE_PATH: Path | None = None
_STATE_CACHE_STAMP: tuple[int, int] | None = None
_STATE_CACHE: dict = {"images": {}}


def load_state() -> dict:
    """Return the current state from memory, refreshing external changes."""
    global _STATE_CACHE_PATH, _STATE_CACHE_STAMP, _STATE_CACHE
    path = state_path()
    with STATE_LOCK:
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if path != _STATE_CACHE_PATH or stamp != _STATE_CACHE_STAMP:
            try:
                loaded = durable_io.load_json(path, None)
                if not isinstance(loaded, dict):
                    raise ValueError("state root must be an object")
                loaded.setdefault("images", {})
                _STATE_CACHE = loaded
            except Exception:
                # Preserve the last valid in-memory state if an external write
                # is incomplete or corrupt. A first launch still starts empty.
                if path != _STATE_CACHE_PATH:
                    _STATE_CACHE = {"images": {}}
            _STATE_CACHE_PATH = path
            _STATE_CACHE_STAMP = stamp
        return copy.deepcopy(_STATE_CACHE)


def write_state(state: dict) -> None:
    """Atomically replace state and refresh the process-local parsed copy."""
    global _STATE_CACHE_PATH, _STATE_CACHE_STAMP, _STATE_CACHE
    path = state_path()
    durable_io.atomic_write_json(path, state)
    stat = path.stat()
    _STATE_CACHE_PATH = path
    _STATE_CACHE_STAMP = (stat.st_mtime_ns, stat.st_size)
    _STATE_CACHE = copy.deepcopy(state)


def save_image_state(name: str, entry: dict) -> None:
    save_image_states({name: entry})


def save_image_states(entries: dict[str, dict]) -> None:
    """Merge one or more image edits and persist one atomic state snapshot."""
    cat = catalog_handle()
    if cat is not None:
        updates, versions, names = {}, {}, []
        for name, entry in entries.items():
            image_id = catalog_image_id(name)
            if image_id is None:
                continue
            payload = dict(entry)
            version = payload.pop("versions", None)
            updates[image_id] = payload
            if version is not None:
                versions[image_id] = version
            names.append(name)
        cat.save_states(updates)
        for image_id, version in versions.items():
            cat.save_versions(image_id, version)
        for name in names:
            queue_sidecar(name)
        _queue_mirror()
        return
    with STATE_LOCK:
        st = load_state()
        for name, entry in entries.items():
            st["images"].setdefault(name, {}).update(entry)
        write_state(st)


def expand_paired_metadata(entries: dict[str, dict]) -> dict[str, dict]:
    """Opt-in metadata coupling at the API boundary; never expand pixel edits.

    Exports and Trash deliberately do not call this. Existing divergent pairs
    are not modified just by enabling the preference.
    """
    prefs = load_preferences()
    if prefs.get("pairRawJPEG", True) is False or prefs.get("linkPairedMetadata") is not True:
        return entries
    cat = catalog_handle()
    expanded = {name: dict(entry) for name, entry in entries.items()}
    keys = {"status", "rating", "label", "keywords"}
    for name, entry in entries.items():
        metadata = {key: value for key, value in entry.items() if key in keys}
        if not metadata:
            continue
        if cat is not None:
            image_id = catalog_image_id(name)
            companions = cat.paired_image_names(image_id) if image_id is not None else []
            previous = cat.mark_metadata_for(image_id) if image_id is not None else {}
        else:
            source, _, _, virtual = resolve_name(name)
            if virtual or (not is_raw(name) and source.suffix.casefold() not in {".jpg", ".jpeg"}):
                continue
            members = [candidate for candidate in source.parent.iterdir()
                       if candidate.is_file() and candidate.stem.casefold() == source.stem.casefold()
                       and (candidate.suffix.casefold() in RAW_EXTS or
                            candidate.suffix.casefold() in {".jpg", ".jpeg"})]
            companions = []
            if len(members) == 2 and sum(candidate.suffix.casefold() in RAW_EXTS for candidate in members) == 1:
                companions = [candidate.relative_to(FOLDER.resolve()).as_posix()
                              for candidate in members if candidate != source]
            previous = load_state().get("images", {}).get(name, {})
        # UI edit snapshots contain unchanged marks. Couple only actual mark
        # changes in those snapshots; a pixel adjustment must not synchronize
        # divergent legacy metadata merely because linking was just enabled.
        # Explicit metadata-only API/CLI actions still set the pair directly.
        if set(entry) & {"params", "grade", "crop", "masks", "heals", "optics"}:
            defaults = {"status": "pending", "rating": 0, "label": "none", "keywords": []}
            metadata = {key: value for key, value in metadata.items()
                        if value != previous.get(key, defaults[key])}
        for companion in companions:
            target = expanded.setdefault(companion, {})
            for key, value in metadata.items():
                if key in target and target[key] != value:
                    raise ValueError("Paired RAW and JPEG received conflicting metadata; choose one capture decision")
                target[key] = copy.deepcopy(value)
    return expanded


_MIRROR_TIMER: threading.Timer | None = None


def catalog_mirror_enabled() -> bool:
    return CATALOG_MIRROR and load_preferences().get("catalogMirror", True) is not False


def _queue_mirror(delay: float = 5.0, retries: int = 2) -> None:
    """Rewrite the per-folder state file a few seconds after the last edit.

    The mirror exists so a folder can still carry its own edits to another
    machine. It is deliberately best-effort and debounced: the catalog is the
    real store, and a read-only volume must not surface an error here.
    """
    global _MIRROR_TIMER
    cat = catalog_handle()
    if cat is None:
        return
    with STATE_LOCK:
        if _MIRROR_TIMER is not None:
            _MIRROR_TIMER.cancel()

        prefs = load_preferences()
        if not catalog_mirror_enabled() and not prefs.get("writeSidecars"):
            _MIRROR_TIMER = None
            return

        def run() -> None:
            try:
                if cat is not catalog_handle():
                    return
                if catalog_mirror_enabled():
                    for source in cat.sources():
                        if source["available"]:
                            catalog_scan.mirror_state_file(cat, source["id"])
                if load_preferences().get("writeSidecars"):
                    write_pending_sidecars()
                    if retries and sidecar_sync_status()["pending"]:
                        _queue_mirror(delay=30.0, retries=retries - 1)
            finally:
                cat.close()


        _MIRROR_TIMER = threading.Timer(delay, run)
        _MIRROR_TIMER.daemon = True
        _MIRROR_TIMER.start()


_SIDECAR_PREFIX = "sidecar.pending:"
_SIDECAR_WRITE_LOCK = threading.Lock()


def queue_sidecar(name: str) -> None:
    """Persist the outbox entry by image ID, so renames and restarts are safe."""
    if library_workflow.is_virtual(name):
        return  # A virtual edit must never replace its original's XMP.
    cat = catalog_handle()
    if cat is None:
        return
    image_id = catalog_image_id(name)
    if image_id is None:
        return
    with cat.write() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     (_SIDECAR_PREFIX + str(image_id), json.dumps({
                         "revision": time.time_ns(), "error": ""})))


def _pending_sidecars(cat) -> list:
    return cat.connection.execute(
        "SELECT key, value FROM meta WHERE key >= ? AND key < ? ORDER BY key",
        (_SIDECAR_PREFIX, _SIDECAR_PREFIX + "\uffff")).fetchall()


def sidecar_sync_status() -> dict:
    cat = catalog_handle()
    if cat is None:
        return {"pending": 0, "failed": 0, "errors": []}
    rows = _pending_sidecars(cat)
    errors = []
    for row in rows:
        record = json.loads(row["value"])
        if record.get("error"):
            image = cat.image_row(int(row["key"][len(_SIDECAR_PREFIX):]))
            errors.append({"name": image["relpath"] if image else "Missing photo",
                           "error": record["error"]})
    return {"pending": len(rows), "failed": len(errors), "errors": errors[:100]}


def write_pending_sidecars() -> int:
    """Flush the durable outbox; failed or newer edits remain ready to retry."""
    import xmp_sidecar

    cat = catalog_handle()
    if cat is None:
        return 0
    if not _SIDECAR_WRITE_LOCK.acquire(blocking=False):
        return 0
    written = 0
    try:
        for pending in _pending_sidecars(cat):
            errors = []
            try:
                image_id = int(pending["key"][len(_SIDECAR_PREFIX):])
                image = cat.image_row(image_id)
                if image is None or image["virtual"]:
                    # Catalog removal means the user no longer requests sync.
                    succeeded = True
                else:
                    name = catalog_module.qualified_name(
                        image["source_id"], image["relpath"])
                    path = src_path(name)
                    if not path.is_file():
                        raise OSError("Original is unavailable. Reconnect its folder and retry.")
                    record = dict(cat.state_for(image_id), iptc=cat.iptc_for(image_id))
                    succeeded = xmp_sidecar.write_sidecar(path, record, errors=errors)
                    if succeeded:
                        written += 1
            except Exception as error:  # retain the outbox entry for retry
                succeeded = False
                errors.append(str(error))
            with cat.write() as conn:
                if succeeded:
                    conn.execute("DELETE FROM meta WHERE key=? AND value=?",
                                 (pending["key"], pending["value"]))
                else:
                    updated = dict(json.loads(pending["value"]),
                                   error="; ".join(errors) or "XMP could not be written.")
                    conn.execute("UPDATE meta SET value=? WHERE key=? AND value=?",
                                 (json.dumps(updated), pending["key"], pending["value"]))
        EVENTS.publish("sidecars", sidecar_sync_status())
    finally:
        _SIDECAR_WRITE_LOCK.release()
    return written


def catalog_entry_for(name: str) -> dict:
    """The full edit record for one image, read from whichever store is live."""
    cat = catalog_handle()
    if cat is None:
        return entry_for(load_state(), name)
    image_id = catalog_image_id(name)
    if image_id is None:
        return entry_for({"images": {}}, name)
    state = cat.state_for(image_id)
    return {
        "status": state.get("status", "pending"),
        "rating": int(state.get("rating", 0) or 0),
        "label": state.get("label", "none"),
        "params": state.get("params"),
        "grade": state.get("grade"),
        "crop": state.get("crop"),
        "masks": edits.clean_masks(state.get("masks")),
        "heals": edits.clean_heals(state.get("heals")),
        "optics": edits.clean_optics(state.get("optics")),
        "keywords": clean_keywords(state.get("keywords", [])),
        "versions": clean_versions(state.get("versions", [])),
        "provenance": state.get("provenance"),
    }


LABEL_VALUES = ("none", "red", "yellow", "green", "blue", "purple")


def clean_label(value) -> str:
    text = str(value or "none").strip().casefold()
    return text if text in LABEL_VALUES else "none"


def require_catalog() -> "catalog_module.Catalog":
    cat = catalog_handle()
    if cat is None:
        raise ValueError("the catalog is not available in folder mode")
    return cat


def catalog_folder_rows(source_id: int | None = None) -> list[dict]:
    """Folder tree with browsable still counts, computed in SQL."""
    cat = catalog_handle()
    if cat is None:
        return folder_rows()
    where, params = " WHERE src.active=1", []
    if source_id is not None:
        where += " AND fo.source_id=?"
        params.append(source_id)
    rows = cat.connection.execute(
        "SELECT fo.id, fo.source_id, fo.relpath, fo.name,"
        " (SELECT COUNT(*) FROM files f"
        "  WHERE f.folder_id=fo.id AND f.missing=0"
        "   AND f.kind!='video') AS count"
        f" FROM folders fo JOIN sources src ON src.id=fo.source_id{where}"
        " ORDER BY fo.source_id, fo.relpath", params
    ).fetchall()
    return [dict(row) for row in rows]


# Background job state for the long-running workflows added alongside the
# catalog. Each follows the existing export/merge pattern: a lock, a status
# dict the UI polls, and a worker thread.
INGEST_LOCK = threading.RLock()
INGEST: dict = {"running": False, "done": 0, "total": 0, "errors": [],
                "copied": 0, "bytes": 0, "cancelled": False}
INGEST_VOLUMES: list[dict] = []
IMPORT_LOCK = threading.RLock()
IMPORT_JOB: dict = {"running": False, "stage": "", "done": 0, "total": 0,
                    "result": None, "error": None}


def cancel_ingest_job() -> None:
    with INGEST_LOCK:
        INGEST["cancelled"] = True


def library_state(st: dict | None = None) -> dict:
    return library_workflow.clean_library_state(st or load_state())


def library_item_names(st: dict | None = None) -> list[str]:
    st = st or load_state()
    physical = list_images()
    available = set(physical)
    copies = [item["name"] for item in library_state(st)["virtualCopies"]
              if item["source"] in available]
    return physical + copies


def _catalog_image_ids(names) -> list[int]:
    ids = []
    for name in names if isinstance(names, list) else []:
        image_id = catalog_image_id(str(name))
        if image_id is not None and image_id not in ids:
            ids.append(image_id)
    return ids


def update_catalog_library(body: dict) -> dict:
    """Apply the legacy library command shape to the catalog store.

    The browser still uses the compact command vocabulary for stacks and
    virtual copies. Keeping that vocabulary is harmless; letting it write the
    retired folder JSON file while boot reads SQLite is not.
    """
    cat = require_catalog()
    action = str(body.get("action", ""))
    result: dict = {}
    if action in ("create_collection", "create_smart_collection"):
        rules = dict(body.get("rules") or {})
        if "flag" in rules and "status" not in rules:
            flag = str(rules.pop("flag"))
            if flag in ("pending", "approved", "skipped"):
                rules["status"] = flag
        mapped = {
            "action": "create_smart" if action == "create_smart_collection"
            else "create",
            "name": body.get("name", "Collection"),
            "rules": rules,
            "imageIds": _catalog_image_ids(body.get("members", [])),
        }
        result.update(catalog_collections_action(mapped))
    elif action == "delete_collection":
        result.update(catalog_collections_action(
            {"action": "delete", "id": body.get("id")}))
    elif action == "add_to_collection":
        result.update(catalog_collections_action({
            "action": "add", "id": body.get("id"),
            "imageIds": _catalog_image_ids(body.get("members", [])),
        }))
    elif action == "create_stack":
        result["id"] = cat.add_stack(
            str(body.get("name", "Photo stack")),
            _catalog_image_ids(body.get("members", [])))
        result["ok"] = True
    elif action == "toggle_stack":
        cat.toggle_stack(int(body["id"]))
        result["ok"] = True
    elif action == "unstack":
        cat.delete_stack(int(body["id"]))
        result["ok"] = True
    elif action == "create_virtual":
        selected_id = catalog_image_id(str(body.get("name", "")))
        row = cat.image_row(selected_id) if selected_id is not None else None
        if not row:
            raise ValueError("unknown image")
        base_id = cat.image_id_for(int(row["source_id"]), row["relpath"])
        if base_id is None:
            raise ValueError("unknown source image")
        ident = "copy-" + hashlib.sha256(
            str(time.time_ns()).encode()).hexdigest()[:14]
        display = " ".join(str(body.get(
            "displayName", "Virtual copy")).split())[:100] or "Virtual copy"
        copy_id = cat.add_virtual_copy(base_id, ident, display)
        name = catalog_module.qualified_name(
            int(row["source_id"]), row["relpath"], ident)
        source = catalog_module.qualified_name(
            int(row["source_id"]), row["relpath"])
        result.update(ok=True, copy={
            "id": ident, "name": name, "source": source,
            "displayName": display, "created": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **catalog_entry_for(name), "raw": is_raw(source),
            "virtual": True, "sourceName": source,
            "catalogId": copy_id,
        })
    elif action == "delete_virtual":
        image_id = catalog_image_id(str(body.get("name", "")))
        if image_id is None:
            raise ValueError("unknown virtual copy")
        cat.delete_virtual_copy(image_id)
        result["ok"] = True
    else:
        raise ValueError("unknown library action")
    _queue_mirror()
    result["library"] = current_library_state()
    return result


def update_library(body: dict) -> dict:
    if catalog_handle() is not None:
        return update_catalog_library(body)
    with STATE_LOCK:
        st = load_state()
        st.setdefault("images", {})
        clean = library_state(st)
        collections = clean["collections"]
        stacks = clean["stacks"]
        copies = clean["virtualCopies"]
        action = str(body.get("action", ""))
        result = {}
        if action in ("create_collection", "create_smart_collection"):
            raw = {
                "id": "collection-" + hashlib.sha256(
                    str(time.time_ns()).encode()).hexdigest()[:14],
                "name": body.get("name", "Collection"),
                "type": "smart" if action == "create_smart_collection" else "regular",
                "members": body.get("members", []), "rules": body.get("rules", {}),
            }
            collections = library_workflow.clean_collections([*collections, raw])
        elif action == "delete_collection":
            collections = [item for item in collections
                           if item["id"] != str(body.get("id", ""))]
        elif action == "add_to_collection":
            ident = str(body.get("id", ""))
            additions = [str(value) for value in body.get("members", [])]
            for item in collections:
                if item["id"] == ident and item["type"] == "regular":
                    item["members"] = list(dict.fromkeys(
                        [*item["members"], *additions]))[:10000]
        elif action == "create_stack":
            raw = {
                "id": "stack-" + hashlib.sha256(
                    str(time.time_ns()).encode()).hexdigest()[:14],
                "name": body.get("name", "Photo stack"),
                "members": body.get("members", []), "collapsed": True,
            }
            stacks = library_workflow.clean_stacks([*stacks, raw])
        elif action == "toggle_stack":
            ident = str(body.get("id", ""))
            for item in stacks:
                if item["id"] == ident:
                    item["collapsed"] = not item["collapsed"]
        elif action == "unstack":
            ident = str(body.get("id", ""))
            stacks = [item for item in stacks if item["id"] != ident]
        elif action == "create_virtual":
            source = library_workflow.source_name(str(body.get("name", "")))
            src_path(source)
            ident = "copy-" + hashlib.sha256(
                str(time.time_ns()).encode()).hexdigest()[:14]
            virtual = library_workflow.clean_virtual_copies([{
                "id": ident, "source": source,
                "displayName": body.get("displayName", "Virtual copy"),
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }])[0]
            source_entry = entry_for(st, str(body.get("name", source)))
            st["images"][virtual["name"]] = source_entry
            copies.append(virtual)
            result["copy"] = dict(virtual, **entry_for(st, virtual["name"]),
                                  raw=is_raw(source), virtual=True,
                                  sourceName=source)
        elif action == "delete_virtual":
            name = str(body.get("name", ""))
            if not library_workflow.is_virtual(name):
                raise ValueError("not a virtual copy")
            copies = [item for item in copies if item["name"] != name]
            st["images"].pop(name, None)
            for item in collections:
                item["members"] = [member for member in item["members"] if member != name]
            for item in stacks:
                item["members"] = [member for member in item["members"] if member != name]
            stacks = library_workflow.clean_stacks(stacks)
        else:
            raise ValueError("unknown library action")
        st["collections"] = collections
        st["stacks"] = stacks
        st["virtualCopies"] = copies
        write_state(st)
    return dict(result, library=library_state(st))


# ------------------------------------------------------------- pipeline ----

RENDER_LOCK = PriorityGate(reentrant=True)  # preview and resident export share admission
RENDER_CONTEXT = threading.local()
HELPER_LOCK = threading.Lock()          # serialize deferred browser helpers
TIFF_BUILD_LOCK = threading.RLock()     # virtual copies can share one source decode
GENERATION_LOCK = threading.Lock()
LATEST_GENERATION: dict[str, int] = {}
# Thumbnails shell out to sips; a fresh folder can request hundreds at once,
# so cap the concurrent decoders rather than forking one per request.
THUMB_SEM = threading.Semaphore(4)
_LINEAR_CACHE: OrderedDict = OrderedDict()   # (name, width) -> float array
_LINEAR_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_LINEAR_CACHE_BYTES", str(256 * 1024 * 1024)))
_ORIG_MEAN_CACHE: OrderedDict = OrderedDict()
_ORIG_MEAN_CACHE_MAX_ENTRIES = 128
RAW_REFINE_POOL = LatestWorkQueue(max_pending=16)
atexit.register(RAW_REFINE_POOL.shutdown, wait=False)
RAW_REFINE_LOCK = threading.RLock()
RAW_REFINE_JOBS: dict[tuple, object] = {}
NEUTRAL_REFINE_JOBS: dict[tuple, object] = {}
LIBRARY_CACHE_LOCK = threading.Lock()
LIBRARY_CACHE: dict = {}
LIBRARY_CACHE_TTL_SECONDS = float(os.environ.get(
    "LIGHTTABLE_LIBRARY_CACHE_SECONDS", "2"))


def _library_folder_rows(names: list[str], directories: set[str]) -> list[dict]:
    direct: dict[str, int] = {"": 0}
    total: dict[str, int] = {"": 0}
    directories.add("")
    for name in names:
        parent = Path(name).parent.as_posix()
        if parent == ".":
            parent = ""
        directories.add(parent)
        direct[parent] = direct.get(parent, 0) + 1
        total[""] = total.get("", 0) + 1
        if parent:
            parts = Path(parent).parts
            for i in range(1, len(parts) + 1):
                ancestor = Path(*parts[:i]).as_posix()
                directories.add(ancestor)
                total[ancestor] = total.get(ancestor, 0) + 1
    return [{
        "path": rel,
        "name": FOLDER.name if not rel else Path(rel).name,
        "depth": 0 if not rel else len(Path(rel).parts),
        "directCount": direct.get(rel, 0),
        "totalCount": total.get(rel, direct.get(rel, 0)),
    } for rel in sorted(
        directories,
        key=lambda value: tuple(part.casefold() for part in Path(value).parts),
    )]


def invalidate_library_cache() -> None:
    with LIBRARY_CACHE_LOCK:
        LIBRARY_CACHE.clear()


def library_snapshot() -> dict:
    """Scan files and folders once, then reuse the result during UI refresh bursts."""
    now = time.monotonic()
    folder_identity = str(FOLDER.resolve())
    with LIBRARY_CACHE_LOCK:
        if (LIBRARY_CACHE.get("folder") == folder_identity and
                LIBRARY_CACHE.get("expires", 0.0) > now):
            return LIBRARY_CACHE

    folder_root = FOLDER.resolve()
    excluded_roots = {(FOLDER / EXPORT_DIR_NAME).resolve(),
                      *SESSION_EXPORT_DESTINATIONS}
    for recipe in load_export_recipes():
        try:
            excluded_roots.add(export_workflow.resolve_destination(
                FOLDER, recipe.get("destination")))
        except ValueError:
            pass
    names = []
    mtimes = {}
    directories = {""}
    for p in FOLDER.rglob("*"):
        try:
            rel = p.relative_to(FOLDER)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if EXPORT_DIR_NAME in rel.parts:
                continue
            lexical = folder_root / rel
            if any(lexical == root or root in lexical.parents
                   for root in excluded_roots):
                continue
            # rglob does not follow directory symlinks. Only a symlinked file
            # can therefore escape the lexical tree and needs the slower real
            # path check; ordinary library entries avoid a resolve syscall.
            if p.is_symlink():
                resolved = p.resolve()
                if any(resolved == root or root in resolved.parents
                       for root in excluded_roots):
                    continue
            if p.is_dir():
                directories.add(rel.as_posix())
            elif p.is_file() and p.suffix.lower() in EXTS:
                name = rel.as_posix()
                names.append(name)
                mtimes[name] = p.stat().st_mtime
        except OSError:
            continue
    names.sort(key=str.casefold)
    snapshot = {
        "folder": folder_identity,
        "expires": now + max(0.0, LIBRARY_CACHE_TTL_SECONDS),
        "names": names,
        "mtimes": mtimes,
        "folders": _library_folder_rows(names, directories),
    }
    with LIBRARY_CACHE_LOCK:
        LIBRARY_CACHE.clear()
        LIBRARY_CACHE.update(snapshot)
        return LIBRARY_CACHE


def list_images() -> list[str]:
    """Return source-relative POSIX paths for every supported local photo."""
    return list(library_snapshot()["names"])


def catalog_image_names() -> list[str]:
    """Physical still names across every catalog source, in bounded pages."""
    cat = catalog_handle()
    if cat is None:
        return [name for name in list_images() if not is_video(name)]
    names: list[str] = []
    offset = 0
    while True:
        page = cat.query({"limit": 5000, "offset": offset,
                          "sort": {"field": "name", "dir": "asc"}})
        names.extend(item["name"] for item in page["items"]
                     if not item["virtual"] and item["kind"] != "video")
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            return names


def folder_rows(images: list[str] | None = None) -> list[dict]:
    """Flatten the on-disk directory tree for the desktop-style source rail."""
    snapshot = library_snapshot()
    names = list(snapshot["names"] if images is None else images)
    if names == snapshot["names"]:
        return [dict(row) for row in snapshot["folders"]]
    return _library_folder_rows(names, {""})


def folder_path(relative: str, *, allow_root: bool = True) -> Path:
    """Resolve a source-relative folder without allowing traversal/symlink escape."""
    raw = str(relative or "").replace("\\", "/").strip("/")
    if raw in ("", "."):
        if allow_root:
            return FOLDER.resolve()
        raise ValueError("the source root cannot be changed here")
    candidate = (FOLDER / raw).resolve()
    root = FOLDER.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("bad folder path")
    if not candidate.is_dir():
        raise ValueError("folder not found")
    return candidate


def clean_folder_name(value) -> str:
    name = " ".join(str(value or "").split()).strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("invalid folder name")
    if name.startswith("."):
        raise ValueError("hidden folder names are not supported")
    if IS_WINDOWS:
        if any(ord(character) < 32 or character in '<>:"|?*'
               for character in name):
            raise ValueError("invalid folder name")
        if name.rstrip(". ") != name:
            raise ValueError("folder names cannot end with a dot or space")
        if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ValueError("that folder name is reserved by Windows")
    return name[:120]


def _remap_state_paths(old_prefix: str, new_prefix: str) -> None:
    """Move persisted edit records along with a renamed folder or photo."""
    _remap_state_paths_many([(old_prefix, new_prefix)])


def _remap_state_paths_many(remaps: list[tuple[str, str]]) -> None:
    """Commit a batch of path changes in one durable state-file replacement."""
    with STATE_LOCK:
        st = load_state()
        images = st.get("images", {})
        changed = False
        for old_prefix, new_prefix in remaps:
            for old in list(images):
                if old == old_prefix or old.startswith(
                        old_prefix.rstrip("/") + "/"):
                    suffix = old[len(old_prefix):]
                    images[new_prefix + suffix] = images.pop(old)
                    changed = True
        if changed:
            def remap(value):
                for old_prefix, new_prefix in remaps:
                    if value == old_prefix or value.startswith(
                            old_prefix.rstrip("/") + "/"):
                        return new_prefix + value[len(old_prefix):]
                return value
            for copy in st.get("virtualCopies", []):
                if isinstance(copy, dict):
                    copy["source"] = remap(str(copy.get("source", "")))
                    copy["name"] = remap(str(copy.get("name", "")))
            for collection in st.get("collections", []):
                if isinstance(collection, dict):
                    collection["members"] = [remap(str(value))
                                             for value in collection.get("members", [])]
            for stack in st.get("stacks", []):
                if isinstance(stack, dict):
                    stack["members"] = [remap(str(value))
                                        for value in stack.get("members", [])]
        if changed:
            write_state(st)


def _photo_move_plan(source: Path, target: Path) -> dict:
    """Preflight one original and the two recognized XMP naming forms."""
    source, target = Path(source), Path(target)
    if source == target:
        return {"source": source, "target": target, "sidecars": []}
    if not source.is_file():
        raise ValueError(f"source photo is missing: {source.name}")
    if target.exists():
        raise ValueError(f"{target.name} already exists in that folder")
    pairs = []
    for sidecar, sidecar_target in (
        (source.with_suffix(".xmp"), target.with_suffix(".xmp")),
        (Path(str(source) + ".xmp"), Path(str(target) + ".xmp")),
    ):
        if not sidecar.is_file() or (sidecar, sidecar_target) in pairs:
            continue
        if sidecar_target.exists():
            raise ValueError(
                f"{sidecar_target.name} already exists; no files were moved")
        pairs.append((sidecar, sidecar_target))
    return {"source": source, "target": target, "sidecars": pairs}


def _stage_photo_move(plan: dict) -> None:
    """Copy sidecars first, then no-clobber move the original.

    The old sidecars are retained until the catalog/state commit succeeds. A
    crash at any point therefore leaves a usable sidecar beside either name.
    """
    source, target = plan["source"], plan["target"]
    if source == target:
        return
    copied = []
    try:
        for sidecar, sidecar_target in plan["sidecars"]:
            durable_io.copy_file_no_replace(sidecar, sidecar_target)
            copied.append(sidecar_target)
        durable_io.move_file_no_replace(source, target)
    except Exception:
        for sidecar_target in reversed(copied):
            sidecar_target.unlink(missing_ok=True)
        raise


def _rollback_photo_moves(plans: list[dict]) -> list[str]:
    errors = []
    for plan in reversed(plans):
        source, target = plan["source"], plan["target"]
        try:
            if target.exists() and not source.exists():
                durable_io.move_file_no_replace(target, source)
            if source.exists():
                for _, sidecar_target in plan["sidecars"]:
                    sidecar_target.unlink(missing_ok=True)
        except OSError as error:
            errors.append(f"{target.name}: {error}")
    return errors


def _finish_photo_moves(plans: list[dict]) -> list[str]:
    """Remove superseded sidecars; duplicates are safer than deletion failure."""
    warnings = []
    for plan in plans:
        for sidecar, _ in plan["sidecars"]:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as error:
                warnings.append(
                    f"left the old sidecar {sidecar.name} in place: {error}")
    return warnings


def create_subfolder(parent: str, name) -> str:
    base = folder_path(parent)
    destination = base / clean_folder_name(name)
    if destination.exists():
        raise ValueError("a folder with that name already exists")
    destination.mkdir()
    invalidate_library_cache()
    return destination.relative_to(FOLDER.resolve()).as_posix()


def rename_subfolder(relative: str, name) -> str:
    source = folder_path(relative, allow_root=False)
    destination = source.with_name(clean_folder_name(name))
    if destination.exists():
        raise ValueError("a folder with that name already exists")
    old_rel = source.relative_to(FOLDER.resolve()).as_posix()
    source.rename(destination)
    new_rel = destination.relative_to(FOLDER.resolve()).as_posix()
    try:
        cat = catalog_handle()
        if cat is not None and PRIMARY_SOURCE_ID is not None:
            batch = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16]
            cat.rename_folder(PRIMARY_SOURCE_ID, old_rel, new_rel, batch=batch)
        else:
            _remap_state_paths(old_rel, new_rel)
    except Exception:
        try:
            destination.rename(source)
        except OSError as rollback_error:
            raise RuntimeError(
                "folder moved but catalog update and filesystem rollback failed; "
                f"it remains at {destination}: {rollback_error}")
        raise
    invalidate_library_cache()
    return new_rel


def move_images(names: list[str], destination: str) -> list[str]:
    """Move local originals (and adjacent XMP sidecars) into a source folder."""
    dest = folder_path(destination)
    if any(library_workflow.is_virtual(name) for name in names):
        raise ValueError("virtual copies cannot move originals")
    resolved = [resolve_name(name) for name in names]
    sources = [item[0] for item in resolved]
    targets = [dest / src.name for src in sources]
    if len({str(path) for path in targets}) != len(targets):
        raise ValueError("the selection contains duplicate file names")
    plans = [_photo_move_plan(source, target)
             for source, target in zip(sources, targets)]
    staged = []
    try:
        for plan in plans:
            _stage_photo_move(plan)
            staged.append(plan)
        new_rels = [target.relative_to(FOLDER.resolve()).as_posix()
                    for target in targets]
        cat = catalog_handle()
        if cat is not None:
            if PRIMARY_SOURCE_ID is None:
                raise RuntimeError("the primary catalog source is unavailable")
            records = []
            for item, new_rel in zip(resolved, new_rels):
                _, source_id, old_rel, _ = item
                records.append((source_id or PRIMARY_SOURCE_ID, old_rel,
                                PRIMARY_SOURCE_ID, new_rel))
            cat.relocate_files(records)
        else:
            old_rels = [source.relative_to(FOLDER.resolve()).as_posix()
                        for source in sources]
            _remap_state_paths_many(list(zip(old_rels, new_rels)))
    except Exception as error:
        rollback_errors = _rollback_photo_moves(staged)
        if rollback_errors:
            raise RuntimeError(
                f"move failed ({error}); recovery also needs attention: "
                + "; ".join(rollback_errors)) from error
        raise
    _finish_photo_moves(staged)
    moved = new_rels
    invalidate_library_cache()
    return moved


def src_path(name: str) -> Path:
    return resolve_name(name)[0]


_HEADER_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def content_hash(path: Path) -> str:
    """Cached content identity for a file, keyed by its stat signature."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _HEADER_HASH_CACHE.get(key)
    if cached is None:
        cached = catalog_scan.header_hash(path)
        if len(_HEADER_HASH_CACHE) > 20000:
            _HEADER_HASH_CACHE.clear()
        _HEADER_HASH_CACHE[key] = cached
    return cached


def file_key(name: str) -> str:
    """Cache identity for one source file.

    Derived from content rather than location, so moving or renaming a file in
    Finder keeps its decoded input, previews, and renders instead of silently
    orphaning every cache entry. The modification time stays in the key because
    an edit in place must still invalidate, and it is preserved by a move.
    """
    src = src_path(name)
    stat = src.stat()
    identity = f"{content_hash(src)}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.md5(identity.encode()).hexdigest()


_TIFF_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_TIFF_CACHE_BYTES", str(1536 * 1024 * 1024)))
_NEUTRAL_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_NEUTRAL_CACHE_BYTES", str(1536 * 1024 * 1024)))
_NEUTRAL_PREVIEW_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_NEUTRAL_PREVIEW_CACHE_BYTES", str(512 * 1024 * 1024)))
_RUST_INPUT_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_RUST_INPUT_CACHE_BYTES", str(512 * 1024 * 1024)))
_EXPORT_FILM_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_EXPORT_CACHE_BYTES", str(1024 * 1024 * 1024)))
_EDIT_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_EDIT_CACHE_BYTES", str(512 * 1024 * 1024)))
_NATIVE_SURFACE_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_NATIVE_SURFACE_CACHE_BYTES", str(512 * 1024 * 1024)))
_RENDER_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_RENDER_CACHE_BYTES", str(1024 * 1024 * 1024)))
_ORIGINAL_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_ORIGINAL_CACHE_BYTES", str(512 * 1024 * 1024)))
_THUMB_CACHE_MAX_BYTES = int(os.environ.get(
    "LIGHTTABLE_THUMB_CACHE_BYTES", str(512 * 1024 * 1024)))
_BASE_CACHE_BUDGET_BYTES = sum((
    _TIFF_CACHE_MAX_BYTES, _NEUTRAL_CACHE_MAX_BYTES,
    _NEUTRAL_PREVIEW_CACHE_MAX_BYTES, _RUST_INPUT_CACHE_MAX_BYTES,
    _EXPORT_FILM_CACHE_MAX_BYTES, _EDIT_CACHE_MAX_BYTES,
    _NATIVE_SURFACE_CACHE_MAX_BYTES, _RENDER_CACHE_MAX_BYTES,
    _ORIGINAL_CACHE_MAX_BYTES, _THUMB_CACHE_MAX_BYTES,
))
NATIVE_BROWSER_HELPER_MAX_WIDTH = int(os.environ.get(
    "LIGHTTABLE_NATIVE_HELPER_WIDTH", "1100"))
NATIVE_BROWSER_HELPER_OUTPUT_WIDTH = int(os.environ.get(
    "LIGHTTABLE_NATIVE_HELPER_OUTPUT_WIDTH", "256"))


def configured_cache_budget_bytes() -> int:
    """Unified on-disk budget, distributed across the existing cache tiers."""
    try:
        value = float(load_preferences().get(
            "cacheBudgetGB", _BASE_CACHE_BUDGET_BYTES / (1024 ** 3)))
    except (TypeError, ValueError):
        value = _BASE_CACHE_BUDGET_BYTES / (1024 ** 3)
    return int(max(2.0, min(64.0, value)) * (1024 ** 3))


def scaled_cache_limit(base_limit: int) -> int:
    # Tiny explicit limits are used by tests, diagnostics, and constrained
    # deployments. Treat them as authoritative instead of inflating them to
    # the normal production floor.
    if base_limit < 1024 * 1024:
        return max(1, base_limit)
    ratio = configured_cache_budget_bytes() / max(1, _BASE_CACHE_BUDGET_BYTES)
    return max(32 * 1024 * 1024, int(base_limit * ratio))


def cache_status() -> dict:
    used, files = 0, 0
    if CACHE.is_dir():
        for path in CACHE.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    used += path.stat().st_size
                    files += 1
            except OSError:
                continue
    return {"path": str(CACHE.resolve()), "usedBytes": used, "files": files,
            "budgetBytes": configured_cache_budget_bytes()}


def purge_generated_cache() -> dict:
    """Delete only generated files below this instance's exact cache root."""
    removed, bytes_removed = 0, 0
    root = CACHE.resolve()
    if not root.is_dir():
        return {"ok": True, "removed": 0, "bytesRemoved": 0,
                **cache_status()}
    for path in root.rglob("*"):
        try:
            resolved = path.resolve()
            if root not in resolved.parents or not path.is_file() \
                    or path.is_symlink():
                continue
            size = path.stat().st_size
            path.unlink()
            removed += 1
            bytes_removed += size
        except OSError:
            continue
    return {"ok": True, "removed": removed,
            "bytesRemoved": bytes_removed, **cache_status()}


def prune_cache(folder: Path, pattern: str, max_bytes: int) -> None:
    """Keep newest cache files within a byte budget instead of an item count."""
    # Same-directory staging files deliberately keep the true extension so
    # image encoders choose the right format. They begin with a dot and must
    # never be mistaken for completed cache entries by a concurrent pruner.
    max_bytes = scaled_cache_limit(max_bytes)
    snapshots = []
    for path in folder.glob(pattern):
        if path.name.startswith("."):
            continue
        try:
            snapshots.append((path.stat().st_mtime, path))
        except OSError:
            # Concurrent builders and pruners are allowed to win this race.
            continue
    files = [path for _, path in sorted(
        snapshots, key=lambda item: item[0], reverse=True)]
    used = 0
    for path in files:
        try:
            size = path.stat().st_size
            if used + size <= max_bytes:
                used += size
            else:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


_CACHE_PRUNE_LOCK = threading.Lock()
_CACHE_PRUNE_LAST: dict[tuple[str, str], float] = {}


def prune_cache_throttled(folder: Path, pattern: str, max_bytes: int,
                          interval: float = 30.0) -> None:
    key = (str(folder), pattern)
    now = time.monotonic()
    with _CACHE_PRUNE_LOCK:
        if now - _CACHE_PRUNE_LAST.get(key, 0) < interval:
            return
        _CACHE_PRUNE_LAST[key] = now
    threading.Thread(
        target=prune_cache, args=(folder, pattern, max_bytes), daemon=True,
        name="lighttable-cache-prune").start()


def prune_render_cache(folder: Path, max_bytes: int) -> None:
    """Prune JPEG, native surface, and metadata as inseparable render rows."""
    max_bytes = scaled_cache_limit(max_bytes)
    groups: dict[str, list[Path]] = {}
    for suffix in ("*.jpg", "*.rgba", "*.json"):
        for path in folder.glob(suffix):
            if path.name.startswith("."):
                continue
            groups.setdefault(path.stem, []).append(path)
    snapshots = []
    for paths in groups.values():
        live_paths, mtimes = [], []
        for path in paths:
            try:
                mtimes.append(path.stat().st_mtime)
                live_paths.append(path)
            except OSError:
                continue
        if live_paths:
            snapshots.append((max(mtimes), live_paths))
    ordered = [paths for _, paths in sorted(
        snapshots, key=lambda item: item[0], reverse=True)]
    used = 0
    for paths in ordered:
        size = 0
        for path in paths:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        if used + size <= max_bytes:
            used += size
            continue
        for path in paths:
            path.unlink(missing_ok=True)


def prune_render_cache_throttled(folder: Path, max_bytes: int,
                                 interval: float = 30.0) -> None:
    key = (str(folder), "render-bundles")
    now = time.monotonic()
    with _CACHE_PRUNE_LOCK:
        if now - _CACHE_PRUNE_LAST.get(key, 0) < interval:
            return
        _CACHE_PRUNE_LAST[key] = now
    threading.Thread(
        target=prune_render_cache, args=(folder, max_bytes), daemon=True,
        name="lighttable-render-cache-prune").start()


def orientation_deg(src: Path) -> int:
    """Clockwise rotation baked in by the camera's EXIF Orientation tag.

    sips strips the tag when converting, so the pixels must be rotated
    physically or portrait shots display sideways.
    """
    return platform_image.orientation_degrees(src)


INPUT_CACHE_VERSION = 4  # learned denoise joins capture-stage RAW development
RAW_PREVIEW_CACHE_VERSION = 4  # width-aware half-size accurate demosaic
RAW_SHARED_MAGIC = b"LTRI"
RAW_SHARED_HEADER = struct.Struct("<4sIII")


@contextmanager
def array_shared_input(rgb: np.ndarray, cache_key: str):
    from multiprocessing import shared_memory

    if rgb.dtype != np.uint16 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("shared array must be packed RGB16 pixels")
    height, width = rgb.shape[:2]
    row_bytes = width * 3 * np.dtype("<u2").itemsize
    total_bytes = RAW_SHARED_HEADER.size + row_bytes * height
    shared = shared_memory.SharedMemory(create=True, size=total_bytes)
    pixels = None
    try:
        RAW_SHARED_HEADER.pack_into(
            shared.buf, 0, RAW_SHARED_MAGIC, width, height, row_bytes)
        pixels = np.ndarray(
            (height, width, 3), dtype="<u2", buffer=shared.buf,
            offset=RAW_SHARED_HEADER.size)
        pixels[:] = rgb
        yield {
            "input_shm": shared.name,
            "input_shm_len": total_bytes,
            "input_cache_key": cache_key,
        }
    finally:
        del pixels
        shared.close()
        shared.unlink()


@contextmanager
def raw_shared_input(name: str, params: dict | None = None, *,
                     denoise_status=None, denoise_cancel=None):
    """Expose one decoded RAW to the resident renderer without a TIFF hop.

    ``multiprocessing.shared_memory`` maps the same anonymous POSIX object into
    Python and Rust.  The renderer copies the pixels into its resident input
    cache before replying, so the segment can be unlinked immediately after
    the request.  This keeps the large full-resolution exchange in memory and
    leaves ``tiff_for`` as the portable/failure fallback.
    """
    rgb = np.ascontiguousarray(color_pipeline.decode_raw(
        src_path(name), params, learned_denoise_status=denoise_status,
        learned_denoise_cancel=denoise_cancel))
    cache_key = (
        f"raw-v{INPUT_CACHE_VERSION}:{file_key(name)}:"
        f"{color_pipeline.raw_decode_fingerprint(params)}")
    with array_shared_input(rgb, cache_key) as shared:
        yield shared


def tiff_for(name: str, params: dict | None = None, *,
             denoise_status=None, denoise_cancel=None) -> Path:
    """Full-resolution 16-bit TIFF decode of the source, cached on disk."""
    src = src_path(name)
    wb_key = color_pipeline.raw_decode_fingerprint(params) if is_raw(name) else "romm"
    t = CACHE / "tiff" / f"v{INPUT_CACHE_VERSION}_{file_key(name)}_{wb_key}.tif"
    with TIFF_BUILD_LOCK:
        if not t.exists() or t.stat().st_mtime < src.stat().st_mtime:
            temporary = durable_io.temporary_path(t, "decode")
            try:
                if is_raw(name):
                    # Demosaic to scene-linear with no camera tone curve, no film
                    # simulation, and the full highlight range intact. This is the
                    # input the pipeline actually wants.
                    import tifffile as tf
                    rgb = color_pipeline.decode_raw(
                        src, params, learned_denoise_status=denoise_status,
                        learned_denoise_cancel=denoise_cancel)
                    tf.imwrite(temporary, rgb)
                else:
                    platform_image.convert_processed_to_tiff(
                        src, temporary, app_root=APP, output_space="prophoto")
                durable_io.publish_file(temporary, t)
            finally:
                temporary.unlink(missing_ok=True)
            prune_cache(CACHE / "tiff", "*.tif", _TIFF_CACHE_MAX_BYTES)
        else:
            t.touch()  # keep recently used files out of the prune window
    return t


def neutral_tiff_for(name: str, params: dict | None = None, *,
                     denoise_status=None, denoise_cancel=None,
                     output_space: str = "srgb") -> Path:
    """Full-resolution, display-referred source for profile-off exports."""
    src = src_path(name)
    raw_key = color_pipeline.raw_decode_fingerprint(params) if is_raw(name) else "romm"
    output_space = color_pipeline.normalise_output_space(output_space)
    # Keep preview cache identity stable, and isolate color-preserving exports.
    color_key = "" if output_space == "srgb" else f"_gamut-v1-{output_space}"
    t = CACHE / "neutral" / (
        f"v{INPUT_CACHE_VERSION}_{file_key(name)}_{raw_key}{color_key}.tif")
    with TIFF_BUILD_LOCK:
        if not t.exists() or t.stat().st_mtime < src.stat().st_mtime:
            temporary = durable_io.temporary_path(t, "decode")
            try:
                if is_raw(name):
                    import tifffile as tf
                    # Reuse the scene-linear master so an expensive learned
                    # denoise runs exactly once and Film/Develop see the same
                    # capture-stage pixels.
                    linear = tf.imread(tiff_for(
                        name, params, denoise_status=denoise_status,
                        denoise_cancel=denoise_cancel))
                    display = color_pipeline.linear_prophoto_to_display(
                        linear, params, output_space=output_space)
                    tf.imwrite(temporary,
                               (display * 65535.0 + 0.5).astype(np.uint16))
                else:
                    platform_image.convert_processed_to_tiff(
                        src, temporary, app_root=APP, output_space=output_space)
                durable_io.publish_file(temporary, t)
            finally:
                temporary.unlink(missing_ok=True)
            prune_cache(CACHE / "neutral", "*.tif", _NEUTRAL_CACHE_MAX_BYTES)
        else:
            t.touch()
    return t


NEUTRAL_PREVIEW_CACHE_VERSION = 2


def neutral_preview_path(name: str, width: int, rotate: float = 0,
                         params: dict | None = None,
                         raw_key: str | None = None) -> Path:
    capture_key = (raw_key or color_pipeline.raw_decode_fingerprint(params)) \
        if is_raw(name) else "romm"
    return CACHE / "neutral" / (
        f"preview-v{NEUTRAL_PREVIEW_CACHE_VERSION}_{file_key(name)}_"
        f"{capture_key}_{max(64, min(int(width), 8000))}_{rot90k(rotate)}.jpg")


def build_neutral_preview(name: str, width: int, rotate: float = 0,
                          params: dict | None = None) -> Path:
    """Build the accurate Develop-mode base from the same neutral RAW as export."""
    output = neutral_preview_path(name, width, rotate, params)
    if output.exists():
        return output
    if is_raw(name):
        learned = bool((params or {}).get("learned_denoise", False))
        if learned:
            image = color_pipeline.load_float_rgb(neutral_tiff_for(name, params))
        else:
            linear = color_pipeline.decode_raw(
                src_path(name), params, max_width=width)
            image = color_pipeline.linear_prophoto_to_display_srgb(linear, params)
    else:
        image = color_pipeline.load_float_rgb(neutral_tiff_for(name, params))
    image = color_pipeline.resize_float_width(image, width)
    k = rot90k(rotate)
    if k:
        image = np.ascontiguousarray(np.rot90(image, k))
    durable_io.atomic_write_bytes(
        output, jpeg_bytes((image * 255.0 + 0.5).astype(np.uint8)))
    prune_cache_throttled(CACHE / "neutral", "preview-*.jpg",
                          _NEUTRAL_PREVIEW_CACHE_MAX_BYTES)
    return output


def render_is_stale(client: str, generation: int | None) -> bool:
    if not client or generation is None:
        return False
    with GENERATION_LOCK:
        return generation < LATEST_GENERATION.get(client, generation)


def _run_refinement(function, args, client: str, generation: int | None):
    # Pause decode admission while an interactive render owns the hot path.
    # Release before LibRaw: a long demosaic must not lock out warm previews.
    cancelled = lambda: render_is_stale(client, generation)
    if not RENDER_LOCK.acquire(priority="refine", cancelled=cancelled):
        return None
    RENDER_LOCK.release()
    try:
        return function(*args)
    finally:
        cat = catalog_handle()
        if cat is not None:
            cat.close()


def schedule_neutral_refinement(name: str, width: int,
                                rotate: float = 0,
                                params: dict | None = None, *,
                                client: str = "", generation: int | None = None) -> bool:
    raw_key = color_pipeline.raw_decode_fingerprint(params)
    key = (file_key(name), raw_key, int(width), rot90k(rotate), client, generation)
    if (render_is_stale(client, generation)
            or neutral_preview_path(name, width, rotate, params).exists()):
        return False
    created = False
    with RAW_REFINE_LOCK:
        future = NEUTRAL_REFINE_JOBS.get(key)
        if future is None or future.done():
            future = RAW_REFINE_POOL.submit(
                client or ("neutral", file_key(name)), _run_refinement,
                build_neutral_preview,
                (name, int(width), float(rotate), dict(params or {})),
                client, generation,
                cancelled=lambda: render_is_stale(client, generation))
            NEUTRAL_REFINE_JOBS[key] = future
            created = True
    if created:
        def forget(completed, job_key=key):
            with RAW_REFINE_LOCK:
                if NEUTRAL_REFINE_JOBS.get(job_key) is completed:
                    NEUTRAL_REFINE_JOBS.pop(job_key, None)
        future.add_done_callback(forget)
    return created


def thumb_jpeg(name: str) -> bytes:
    """Small strip thumbnail straight from the source; never decodes full TIFF."""
    p = CACHE / "thumb" / f"{file_key(name)}.jpg"
    if p.exists():
        return p.read_bytes()
    with THUMB_SEM:
        if p.exists():          # another request may have just built it
            return p.read_bytes()
        return _build_thumb(name, p)


def _build_thumb(name: str, p: Path) -> bytes:
    temporary = durable_io.temporary_path(p, "thumb")
    try:
        if is_raw(name):
            built = False
            try:
                built = color_pipeline.raw_embedded_thumbnail(src_path(name), temporary, 240, 80)
            except Exception:
                built = False
            if not built:
                try:
                    rgb = color_pipeline.raw_embedded_preview(src_path(name), 240)
                    im = Image.fromarray(rgb, "RGB")
                except Exception:  # a RAW without an embedded preview still works
                    im = Image.open(raw_display(name))
                    im.load()
                    im.thumbnail((240, 240), Image.LANCZOS)
                im.save(temporary, "JPEG", quality=80)
        else:
            platform_image.build_thumbnail(src_path(name), temporary)
        durable_io.publish_file(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)
    prune_cache_throttled(p.parent, "*.jpg", _THUMB_CACHE_MAX_BYTES)
    return p.read_bytes()


def edited_thumbnail_state(name: str) -> dict:
    """Return the visual state the editor shows for one photo.

    Lean catalog rows intentionally omit their edit blobs, so the thumbnail
    endpoint resolves the authoritative state itself. New-photo defaults are
    merged exactly like the browser does, including the opt-in Film default.
    """
    entry = catalog_entry_for(name)
    default_params, default_grade = effective_new_photo_defaults()
    params = dict(default_params)
    saved_params = entry.get("params") or {}
    if not saved_params and is_raw(name):
        params.update(raw_camera_default(name).get("settings") or {})
    params.update(saved_params)
    current_grade = dict(default_grade)
    current_grade.update(entry.get("grade") or {})
    return {
        "params": fp.clean_params(params),
        "grade": grade.clean(current_grade),
        "crop": clean_crop(entry.get("crop")),
        "masks": edits.clean_masks(entry.get("masks")),
        "heals": edits.clean_heals(entry.get("heals")),
        "optics": edits.clean_optics(entry.get("optics")),
    }


def edited_thumbnail_key(name: str, state: dict | None = None) -> str:
    visual = state or edited_thumbnail_state(name)
    payload = json.dumps([
        EDITED_THUMB_CACHE_VERSION, RENDERER_IDENTITY, file_key(name), visual,
    ], sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode()).hexdigest()


def _accurate_thumbnail_base_ready(name: str, state: dict) -> bool:
    """Schedule an accurate RAW base and report whether it is ready now."""
    if not is_raw(name):
        return True
    params = state["params"]
    if fp.clean_params(params)["profile_enabled"]:
        full = raw_preview_path(
            name, EDITED_THUMB_RENDER_EDGE, "full", params)
        if full.exists():
            return True
        schedule_raw_refinement(name, EDITED_THUMB_RENDER_EDGE, params)
        return False
    rotate = fp.clean_params(params)["rotate"]
    accurate = neutral_preview_path(
        name, EDITED_THUMB_RENDER_EDGE, rotate, params)
    if accurate.exists():
        return True
    schedule_neutral_refinement(
        name, EDITED_THUMB_RENDER_EDGE, rotate, params)
    return False


def edited_thumbnail(name: str) -> tuple[Path, str] | None:
    """Build the current edit-aware thumbnail without delaying interaction.

    The source thumbnail is the immediate UI placeholder. This low-priority
    replacement is generated once the accurate RAW base and shared renderer
    are idle, then cached by source content, renderer identity, and edit state.
    """
    state = edited_thumbnail_state(name)
    key = edited_thumbnail_key(name, state)
    output = CACHE / "thumb" / f"edited_{key}.jpg"
    if output.is_file():
        return output, key
    if not EDITED_THUMB_LOCK.acquire(blocking=False):
        return None
    try:
        if output.is_file():
            return output, key
        if not _accurate_thumbnail_base_ready(name, state):
            return None
        try:
            image = program_render_image({
                "name": name,
                "state": state,
                "w": EDITED_THUMB_RENDER_EDGE,
                "engine": "rs",
            }, priority="prefetch")
        except RuntimeError as error:
            reason = str(error).casefold()
            if "cancelled" in reason or "interactive render active" in reason:
                return None
            raise
        image.thumbnail(
            (EDITED_THUMB_OUTPUT_EDGE, EDITED_THUMB_OUTPUT_EDGE),
            Image.Resampling.LANCZOS,
            reducing_gap=3.0,
        )
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=86, subsampling=1)
        output.parent.mkdir(parents=True, exist_ok=True)
        durable_io.atomic_write_bytes(output, buffer.getvalue())
        prune_cache_throttled(output.parent, "*.jpg", _THUMB_CACHE_MAX_BYTES)
        return output, key
    finally:
        EDITED_THUMB_LOCK.release()


def video_thumbnail(name: str) -> bytes:
    """A poster frame for a catalogued video, cached like any other thumbnail.

    The Swift helper reads the frame with AVFoundation. Without it — on
    Windows, or before the helper is built — a neutral placeholder is returned
    so the grid still lays out correctly rather than showing a broken image.
    """
    destination = CACHE / "thumb" / f"video_{file_key(name)}.jpg"
    if destination.exists():
        return destination.read_bytes()
    source = src_path(name)
    if VISION_HELPER.is_file():
        temporary = durable_io.temporary_path(destination, "video")
        completed = durable_io.temporary_path(destination, "thumb")
        try:
            subprocess.run(
                [str(VISION_HELPER), "--video-thumbnail", str(source),
                 str(temporary)],
                capture_output=True, timeout=60, check=True)
            if temporary.exists():
                with Image.open(temporary) as image:
                    image.thumbnail((240, 240))
                    image.convert("RGB").save(completed, "JPEG", quality=80)
                durable_io.publish_file(completed, destination)
                prune_cache_throttled(
                    destination.parent, "*.jpg", _THUMB_CACHE_MAX_BYTES)
                return destination.read_bytes()
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            temporary.unlink(missing_ok=True)
            completed.unlink(missing_ok=True)
    placeholder = Image.new("RGB", (240, 160), (38, 38, 38))
    buffer = io.BytesIO()
    placeholder.save(buffer, "JPEG", quality=70)
    return buffer.getvalue()


_EXIF_CACHE: dict[str, dict] = {}
def exif_for(name: str) -> dict:
    """Camera metadata for the info panel. Cached; exiftool costs ~50 ms."""
    if name in _EXIF_CACHE:
        return _EXIF_CACHE[name]
    out = platform_image.metadata(src_path(name))
    _EXIF_CACHE[name] = out
    return out


def parse_camera_ev(metadata: dict) -> float | None:
    """Calculate camera EV100 from FNumber, ExposureTime, and ISO."""
    try:
        f_raw = str(metadata.get("FNumber", "")).strip().replace("f/", "").replace("F", "")
        t_raw = str(metadata.get("ExposureTime", "")).strip()
        iso_raw = str(metadata.get("ISO", "")).strip()
        if not f_raw or not t_raw or not iso_raw:
            return None
        fnumber = float(f_raw)
        if "/" in t_raw:
            num, den = t_raw.split("/", 1)
            etime = float(num) / float(den)
        else:
            etime = float(t_raw)
        iso = float(iso_raw)
        if fnumber <= 0 or etime <= 0 or iso <= 0:
            return None
        ev = np.log2((fnumber ** 2) / etime) - np.log2(iso / 100.0)
        return float(ev) if np.isfinite(ev) else None
    except (ValueError, ZeroDivisionError):
        return None


def image_mean_luminance(name: str) -> float:
    """Calculate average scene luminance from the image thumbnail."""
    try:
        data = thumb_jpeg(name)
        if data:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            arr = np.asarray(img, dtype=np.float32) / 255.0
            return float(np.mean(arr @ grade.LUMA))
    except Exception:
        pass
    return 0.18



def raw_camera_identity(name: str) -> dict:
    """Stable camera-default key and human label from source metadata."""
    metadata = exif_for(name)
    make = " ".join(str(metadata.get("Make", "")).split()).strip()
    model = " ".join(str(metadata.get("Model", "")).split()).strip()
    serial = " ".join(str(metadata.get("BodySerialNumber", "")).split()).strip()
    iso = " ".join(str(metadata.get("ISO", "")).split()).strip()
    base_label = " ".join(part for part in (make, model) if part) \
        or "Unknown camera"
    model_key = f"{make.casefold()}|{model.casefold()}"
    identities = {
        "model": model_key,
        "serial": f"{model_key}|serial:{serial.casefold()}" if serial else model_key,
        "iso": f"{model_key}|serial:{serial.casefold()}|iso:{iso.casefold()}"
        if serial and iso else
        f"{model_key}|iso:{iso.casefold()}" if iso else model_key,
    }
    return {
        "key": identities["model"][:240],
        "keys": {key: value[:240] for key, value in identities.items()},
        "label": base_label[:160],
        "serial": serial[:120], "iso": iso[:40],
    }


def clean_raw_develop_settings(value: dict | None) -> dict:
    cleaned = fp.clean_params(value or {})
    return {key: cleaned[key] for key in fp.RAW_DEVELOP_KEYS}


def raw_camera_default(name: str) -> dict:
    identity = raw_camera_identity(name)
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        defaults = prefs.get("rawCameraDefaults", {})
        mode = str(prefs.get("rawDefaultMatch", "model"))
        if mode not in ("model", "serial", "iso"):
            mode = "model"
        keys = identity["keys"]
        candidates = ([keys["iso"], keys["serial"], keys["model"]]
                      if mode == "iso" else
                      [keys["serial"], keys["model"]]
                      if mode == "serial" else [keys["model"]])
        raw = None
        matched_key = candidates[-1]
        if isinstance(defaults, dict):
            for candidate in candidates:
                if candidate in defaults:
                    raw, matched_key = defaults[candidate], candidate
                    break
        return dict(
            identity,
            key=matched_key, matchMode=mode,
            settings=(clean_raw_develop_settings(raw) if raw else None),
        )


def update_raw_camera_default(name: str, settings: dict | None) -> dict:
    identity = raw_camera_identity(name)
    with PREFS_LOCK:
        prefs = load_json_file(PREFS_FILE, {})
        if not isinstance(prefs, dict):
            prefs = {}
        defaults = prefs.get("rawCameraDefaults")
        if not isinstance(defaults, dict):
            defaults = {}
        mode = str(prefs.get("rawDefaultMatch", "model"))
        if mode not in ("model", "serial", "iso"):
            mode = "model"
        key = identity["keys"][mode]
        if settings is None:
            defaults.pop(key, None)
        else:
            defaults[key] = clean_raw_develop_settings(settings)
        prefs["rawCameraDefaults"] = defaults
        durable_io.atomic_write_json(PREFS_FILE, prefs)
        return dict(identity, key=key, matchMode=mode,
                    settings=defaults.get(key))


def semantic_mask_payload(name: str, kind: str,
                          point=None, rotate: float = 0) -> dict:
    """Generate a compact on-device selection in displayed coordinates."""
    import semantic_masks
    if kind not in ("subject", "sky", "object", "depth",
                    *semantic_masks.PERSON_PARTS):
        raise ValueError("unknown semantic mask type")
    normalized_point = None
    if kind == "object":
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("object selection requires a point")
        normalized_point = (
            max(0.0, min(1.0, float(point[0]))),
            max(0.0, min(1.0, float(point[1]))),
        )
    source = CACHE / "semantic" / (
        f"{file_key(name)}_{rot90k(rotate)}_source.jpg")
    if not source.exists():
        durable_io.atomic_write_bytes(source, orig_jpeg(name, 1400, rotate))
    image = np.asarray(Image.open(source).convert("RGB"))
    parts_cache = CACHE / "semantic" / (
        f"{file_key(name)}_{rot90k(rotate)}_parts")
    mask, provider = semantic_masks.generate(
        image, kind, normalized_point, source_path=source,
        vision_helper=VISION_HELPER, vision_provider=VISION_PROVIDER,
        parts_cache=parts_cache,
    )
    if not np.any(mask):
        raise ValueError(f"no {kind} selection found")
    faces = 0
    if kind in semantic_masks.PERSON_PARTS:
        try:
            faces = int(load_json_file(parts_cache / "parts.json", {}).get(
                "faces", 0))
        except (TypeError, ValueError):
            faces = 0
    return {"kind": kind, "provider": provider, "faces": faces,
            "bitmap": semantic_masks.encode_bitmap(
                mask, png=(kind == "depth" or kind in semantic_masks.PERSON_PARTS),
                max_edge=(semantic_masks.MAX_DEPTH_EDGE if kind == "depth" else
                          semantic_masks.MAX_PART_EDGE
                          if kind in semantic_masks.PERSON_PARTS else None))}


def capture_time(name: str) -> str:
    return exif_for(name).get("DateTimeOriginal", "")


def raw_preview_path(name: str, width: int, quality: str,
                     params: dict | None = None) -> Path:
    wb_key = color_pipeline.raw_decode_fingerprint(params)
    return CACHE / "rust" / (
        f"v{RAW_PREVIEW_CACHE_VERSION}_{file_key(name)}_"
        f"{wb_key}_{width}_{quality}.tif")


def build_raw_preview(name: str, width: int, quality: str,
                      params: dict | None = None) -> Path:
    import tifffile as tf
    output = raw_preview_path(name, width, quality, params)
    if output.exists():
        return output
    rgb = (color_pipeline.decode_raw_draft(src_path(name), params, width)
           if quality == "fast" else
           color_pipeline.decode_raw(
               src_path(name), params, max_width=width))
    if rgb.shape[1] > width:
        resized = color_pipeline.resize_float_width(rgb, width)
        rgb = (resized * 65535.0 + 0.5).astype(np.uint16)
    temporary = durable_io.temporary_path(output, "decode")
    try:
        tf.imwrite(temporary, rgb)
        durable_io.publish_file(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    prune_cache(output.parent, "*.tif", _RUST_INPUT_CACHE_MAX_BYTES)
    return output


def schedule_raw_refinement(name: str, width: int,
                            params: dict | None = None, *,
                            client: str = "", generation: int | None = None) -> bool:
    wb_key = color_pipeline.raw_decode_fingerprint(params)
    key = (file_key(name), wb_key, width, client, generation)
    full = raw_preview_path(name, width, "full", params)
    if full.exists() or render_is_stale(client, generation):
        return False
    created = False
    with RAW_REFINE_LOCK:
        future = RAW_REFINE_JOBS.get(key)
        if future is None or future.done():
            future = RAW_REFINE_POOL.submit(
                client or ("film", file_key(name)), _run_refinement,
                build_raw_preview, (name, width, "full", dict(params or {})),
                client, generation,
                cancelled=lambda: render_is_stale(client, generation))
            RAW_REFINE_JOBS[key] = future
            created = True
    if created:
        def forget(completed, job_key=key):
            with RAW_REFINE_LOCK:
                if RAW_REFINE_JOBS.get(job_key) is completed:
                    RAW_REFINE_JOBS.pop(job_key, None)
        future.add_done_callback(forget)
    return created


def selected_preview_tiff(name: str, width: int,
                          params: dict | None = None) -> Path:
    full = raw_preview_path(name, width, "full", params)
    if full.exists():
        return full
    return build_raw_preview(name, width, "fast", params)


def preview_variant(name: str, width: int, params: dict | None = None) -> str:
    if not is_raw(name):
        return "standard"
    return "full" if raw_preview_path(name, width, "full", params).exists() else "fast"


def linear_for(name: str, width: int, params: dict | None = None) -> np.ndarray:
    quality = preview_variant(name, width, params)
    wb_key = color_pipeline.raw_decode_fingerprint(params) if is_raw(name) else "romm"
    key = (file_key(name), wb_key, width, quality)
    with STATE_LOCK:
        if key in _LINEAR_CACHE:
            _LINEAR_CACHE.move_to_end(key)
            return _LINEAR_CACHE[key]
    if is_raw(name):
        preview = selected_preview_tiff(name, width, params)
        arr = fp.load_linear(str(preview))
    else:
        arr = fp.load_linear(str(tiff_for(name, params)), max_width=width)
    with STATE_LOCK:
        _LINEAR_CACHE[key] = arr
        while (len(_LINEAR_CACHE) > 1 and
               sum(value.nbytes for value in _LINEAR_CACHE.values()) >
               _LINEAR_CACHE_MAX_BYTES):
            _LINEAR_CACHE.popitem(last=False)
    return arr


def jpeg_bytes(arr_uint8: np.ndarray, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr_uint8, "RGB").save(
        buf, "JPEG", quality=quality, subsampling=1)
    return buf.getvalue()


def raw_display(name: str) -> Path:
    """Neutral default conversion of a RAW file for the 'before' view.

    rawpy's standard postprocess: camera white balance, auto-brightening and
    the sRGB transfer curve, i.e. roughly what any ordinary raw converter
    shows you. The pipeline itself still receives the untouched linear
    decode; this is only what the eye is asked to compare against.
    """
    p = CACHE / "orig" / f"v2_{file_key(name)}_display.jpg"
    if not p.exists():
        try:
            rgb = color_pipeline.raw_embedded_preview(src_path(name), 1600)
        except Exception:  # noqa: BLE001 - some RAW formats omit a preview
            import rawpy
            with rawpy.imread(str(src_path(name))) as raw:
                rgb = raw.postprocess(output_bps=8, use_camera_wb=True,
                                      output_color=rawpy.ColorSpace.sRGB,
                                      half_size=True)
        im = Image.fromarray(rgb, "RGB")
        im.thumbnail((1600, 1600), Image.LANCZOS)
        buffer = io.BytesIO()
        im.save(buffer, "JPEG", quality=88, subsampling=1)
        durable_io.atomic_write_bytes(p, buffer.getvalue())
        prune_cache_throttled(p.parent, "*.jpg", _ORIGINAL_CACHE_MAX_BYTES)
    return p


def rot90k(deg: float) -> int:
    """Clockwise degrees -> np.rot90 k (which rotates counter-clockwise)."""
    return (-int(round(deg / 90))) % 4


def orig_jpeg(name: str, width: int, rotate: float = 0) -> bytes:
    guard_photo(name)
    with SESSION.inflight("decode", library_workflow.source_name(name)):
        return _orig_jpeg(name, width, rotate)


def _orig_jpeg(name: str, width: int, rotate: float = 0) -> bytes:
    k = rot90k(rotate)
    if k:
        rotated = CACHE / "orig" / f"{file_key(name)}_{width}_{k}.jpg"
        if not rotated.exists():
            base = Image.open(io.BytesIO(_orig_jpeg(name, width, 0))).convert("RGB")
            arr = np.rot90(np.asarray(base), k)
            durable_io.atomic_write_bytes(
                rotated, jpeg_bytes(np.ascontiguousarray(arr)))
        return rotated.read_bytes()
    p = CACHE / "orig" / f"{file_key(name)}_{width}.jpg"
    if not p.exists():
        if is_raw(name):
            im = Image.open(raw_display(name))
            im.load()
            if im.width > width:
                im = im.resize((width, round(im.height * width / im.width)),
                               Image.LANCZOS)
            buffer = io.BytesIO()
            im.save(buffer, "JPEG", quality=88, subsampling=1)
            durable_io.atomic_write_bytes(p, buffer.getvalue())
        else:
            arr = linear_for(name, width)
            durable_io.atomic_write_bytes(
                p, jpeg_bytes((arr * 255 + 0.5).astype(np.uint8)))
        prune_cache_throttled(p.parent, "*.jpg", _ORIGINAL_CACHE_MAX_BYTES)
    return p.read_bytes()


def orig_mean_display(name: str, width: int,
                      linear: np.ndarray | None = None) -> float:
    """Mean of what is actually shown as 'before', for the Match factor."""
    key = (file_key(name), width, "raw-display" if is_raw(name) else "linear")
    with STATE_LOCK:
        if key in _ORIG_MEAN_CACHE:
            _ORIG_MEAN_CACHE.move_to_end(key)
            return _ORIG_MEAN_CACHE[key]
    if is_raw(name):
        im = Image.open(io.BytesIO(orig_jpeg(name, width, 0))).convert("RGB")
        mean = float(sum(ImageStat.Stat(im).mean) / (3.0 * 255.0))
    else:
        source = linear if linear is not None else linear_for(name, width)
        mean = float(source.mean())
    with STATE_LOCK:
        _ORIG_MEAN_CACHE[key] = mean
        while len(_ORIG_MEAN_CACHE) > _ORIG_MEAN_CACHE_MAX_ENTRIES:
            _ORIG_MEAN_CACHE.popitem(last=False)
    return mean


def _platform_binary(path: Path) -> Path:
    return path.with_name(path.name + ".exe") if IS_WINDOWS else path


RUST_BIN = _platform_binary(APP / "engine" / "spektrafilm-rs")
RUST_WORKER_BIN = next((path for path in (
    _platform_binary(APP / "engine" / "lighttable-engine"),
    _platform_binary(APP / "rust-engine" / "target" / "release" / "lighttable-engine"),
) if path.exists()), None)
RUST_DATA = APP / "engine" / "data"
RUST_AVAILABLE = bool((RUST_WORKER_BIN or RUST_BIN.exists())
                      and RUST_DATA.is_dir())
RENDER_CACHE_VERSION = 7  # sixteen-mask atlas, range masks, and uniformity
EDIT_PREVIEW_CACHE_VERSION = 1
EDITED_THUMB_CACHE_VERSION = 1
EDITED_THUMB_RENDER_EDGE = 512
EDITED_THUMB_OUTPUT_EDGE = 320
EDITED_THUMB_LOCK = threading.Lock()


def _digest_file(path: Path | None) -> str | None:
    if not path or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


_RENDERER_PROVENANCE = {
    "schema": RENDER_CACHE_VERSION,
    "lightTableRevision": _git_revision(APP),
    "pythonSpektrafilmRevision": _git_revision(APP / "vendor" / "spektrafilm"),
    "rustCoreRevision": ((APP / "engine" / "VERSION.txt").read_text().strip()
                         if (APP / "engine" / "VERSION.txt").is_file() else None),
    "residentEngineSha256": _digest_file(RUST_WORKER_BIN),
    "oneShotEngineSha256": _digest_file(RUST_BIN),
    "profileCatalogSha256": fp.PROFILE_CATALOG_DIGEST,
}
RENDERER_IDENTITY = hashlib.sha256(json.dumps(
    _RENDERER_PROVENANCE, sort_keys=True, separators=(",", ":"),
).encode()).hexdigest()


def renderer_provenance() -> dict:
    return dict(_RENDERER_PROVENANCE, rendererIdentity=RENDERER_IDENTITY)


class PipeLineReader:
    """Read a child-process pipe with a timeout on platforms without pipe select."""

    def __init__(self, stream):
        self.lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            try:
                for line in iter(stream.readline, ""):
                    self.lines.put(line)
            finally:
                self.lines.put(None)

        self.thread = threading.Thread(
            target=pump, daemon=True, name="rust-engine-output")
        self.thread.start()

    def readline(self, timeout: float) -> str:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("resident Rust engine timed out") from error
        return line or ""


class RustEngineClient:
    """Long-lived JSON-lines client that keeps the GPU and shader caches warm."""

    def __init__(self, binary: Path | None):
        self.binary = binary
        self.process: subprocess.Popen | None = None
        self.reader: PipeLineReader | None = None
        self.lock = RENDER_LOCK
        self.request_id = 0

    def _start(self) -> subprocess.Popen:
        if not self.binary:
            raise RuntimeError("resident Rust engine is not built")
        if self.process and self.process.poll() is None:
            return self.process
        self.process = subprocess.Popen(
            [str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if IS_WINDOWS:
            assert self.process.stdout
            self.reader = PipeLineReader(self.process.stdout)
        return self.process

    def _readline(self, process: subprocess.Popen, timeout: float) -> str:
        assert process.stdout
        if self.reader:
            return self.reader.readline(timeout)
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("resident Rust engine timed out")
        return process.stdout.readline()

    def close(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None
            self.reader = None

    def render(self, request: dict) -> dict:
        priority = getattr(RENDER_CONTEXT, "priority", "export")
        cancelled = getattr(RENDER_CONTEXT, "cancelled", None)
        queued_at = time.perf_counter()
        if not self.lock.acquire(priority=priority, cancelled=cancelled):
            raise RenderCancelled("render superseded before engine dispatch")
        queue_ms = (time.perf_counter() - queued_at) * 1000
        try:
            for attempt in range(2):
                if cancelled and cancelled():
                    raise RenderCancelled("render superseded before engine dispatch")
                process = self._start()
                self.request_id += 1
                payload = dict(request, id=self.request_id, command="render")
                try:
                    assert process.stdin and process.stdout
                    process.stdin.write(json.dumps(payload, separators=(",", ":"))
                                        + "\n")
                    process.stdin.flush()
                    line = self._readline(process, 300)
                    if not line:
                        raise RuntimeError("resident Rust engine exited")
                    result = json.loads(line)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error") or
                                           "resident Rust render failed")
                    return dict(result, queue_ms=round(queue_ms, 3))
                except (BrokenPipeError, OSError, ValueError,
                        TimeoutError, RuntimeError):
                    self.close_unlocked()
                    if attempt:
                        raise
            raise RuntimeError("resident Rust engine unavailable")
        finally:
            self.lock.release()

    def close_unlocked(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None
        self.reader = None

    def warm(self) -> None:
        """Initialize the GPU off the request path; failures retain normal fallback."""
        if not self.binary:
            return
        with self.lock:
            try:
                process = self._start()
                self.request_id += 1
                assert process.stdin and process.stdout
                process.stdin.write(json.dumps({
                    "id": self.request_id, "command": "ping",
                }) + "\n")
                process.stdin.flush()
                if not self._readline(process, 30):
                    self.close_unlocked()
            except (BrokenPipeError, OSError, ValueError,
                    TimeoutError, RuntimeError):
                self.close_unlocked()


RUST_ENGINE = RustEngineClient(RUST_WORKER_BIN)
atexit.register(RUST_ENGINE.close)


def render_key(name: str, params: dict, width: int, engine: str = "py") -> str:
    cleaned = fp.clean_params(params)
    if not is_raw(name):
        # Capture WB is deliberately RAW-only. Preserve it in the saved edit
        # for later RAW use, but do not fragment processed-file render caches
        # with settings that cannot affect their already-developed pixels.
        cleaned["wb_mode"] = fp.DEFAULT_PARAMS["wb_mode"]
        cleaned["wb_temperature"] = fp.DEFAULT_PARAMS["wb_temperature"]
        cleaned["wb_tint"] = fp.DEFAULT_PARAMS["wb_tint"]
        cleaned["raw_profile"] = fp.DEFAULT_PARAMS["raw_profile"]
        cleaned["raw_highlight_recovery"] = fp.DEFAULT_PARAMS["raw_highlight_recovery"]
        cleaned["raw_sensor_denoise"] = fp.DEFAULT_PARAMS["raw_sensor_denoise"]
    blob = json.dumps([RENDER_CACHE_VERSION, RENDERER_IDENTITY, file_key(name),
                       cleaned, width, engine,
                       preview_variant(name, width, params)],
                      sort_keys=True)
    return hashlib.md5(blob.encode()).hexdigest()


def render_rust(name: str, params: dict, width: int,
                output: Path | None,
                native_output: Path | None = None) -> dict:
    """Render JPEG and/or a native RGBA surface in one resident pass."""
    cp = fp.clean_params(params)
    src_tif = selected_preview_tiff(name, width, params) if is_raw(name) else \
        CACHE / "rust" / f"v{INPUT_CACHE_VERSION}_{file_key(name)}_romm_{width}.tif"
    src_tif.parent.mkdir(parents=True, exist_ok=True)
    if RUST_WORKER_BIN:
        request = {
            "data_dir": str(RUST_DATA), "film": cp["stock"],
            "paper": cp["paper"],
            "scan_film": cp["stock"] in fp.POSITIVE_STOCKS,
            "params": fp.rust_params_json(params),
            "quality": 88,
            "rotate_quarters_ccw": rot90k(cp["rotate"]),
        }
        if output is not None:
            request["output"] = str(output)
        if native_output is not None:
            request["native_output"] = str(native_output)

        if src_tif.exists():
            request["input"] = str(src_tif)
            return RUST_ENGINE.render(request)

        if os.name == "posix":
            try:
                arr = linear_for(name, width, params)
                rgb16 = np.ascontiguousarray((np.clip(arr, 0, 1) * 65535 + 0.5).astype(np.uint16))
                cache_key = f"prev-v{INPUT_CACHE_VERSION}:{file_key(name)}:{width}:{preview_variant(name, width, params)}"
                with array_shared_input(rgb16, cache_key) as shared:
                    request.update(shared)
                    return RUST_ENGINE.render(request)
            except RenderCancelled:
                raise
            except Exception:
                for key in ("input_shm", "input_shm_len", "input_cache_key"):
                    request.pop(key, None)

        src_tif.parent.mkdir(parents=True, exist_ok=True)
        arr = linear_for(name, width, params)
        import tifffile as tf
        staged_tiff = durable_io.temporary_path(src_tif, "rust-input")
        try:
            tf.imwrite(
                staged_tiff,
                (np.clip(arr, 0, 1) * 65535 + 0.5).astype(np.uint16),
            )
            durable_io.publish_file(staged_tiff, src_tif)
        finally:
            staged_tiff.unlink(missing_ok=True)
        prune_cache(src_tif.parent, "*.tif", _RUST_INPUT_CACHE_MAX_BYTES)

        request["input"] = str(src_tif)
        return RUST_ENGINE.render(request)

    pjson = CACHE / "rust" / f"p_{render_key(name, params, width, 'rs')}.json"
    durable_io.atomic_write_text(pjson, json.dumps(fp.rust_params_json(params)))
    out_png = CACHE / "rust" / f"o_{render_key(name, params, width, 'rs')}.png"
    cmd = [str(RUST_BIN), "process", str(src_tif), "-o", str(out_png),
           "--film", cp["stock"], "--data-dir", str(RUST_DATA),
           "--params", str(pjson)]
    if cp["stock"] not in fp.POSITIVE_STOCKS:
        cmd += ["--paper", cp["paper"]]
    else:
        cmd += ["--scan-film"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    img = np.asarray(Image.open(out_png).convert("RGB"))
    k = rot90k(cp["rotate"])
    if k:
        img = np.ascontiguousarray(np.rot90(img, k))
    if output is not None:
        durable_io.atomic_write_bytes(output, jpeg_bytes(img))
    if native_output is not None:
        write_native_surface(native_output, img)
    out_png.unlink(missing_ok=True)
    pjson.unlink(missing_ok=True)
    return {"backend": "one-shot", "total_ms": None,
            "mean": float(img.mean()) / 255.0,
            "width": int(img.shape[1]), "height": int(img.shape[0]),
            "input_cache_hit": False, "pipeline_cache_hit": False}


NATIVE_SURFACE_MAGIC = b"FLRA"
NATIVE_SURFACE_HEADER = struct.Struct("<4sIII")


def write_native_surface(path: Path, rgb: np.ndarray) -> dict:
    """Write tightly packed RGBA8 pixels for the AppKit Metal viewport."""
    image = np.asarray(rgb)
    if image.dtype != np.uint8:
        image = (np.clip(image, 0, 1) * 255 + 0.5).astype(np.uint8)
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise ValueError("native surface must be HxWxRGB(A)")
    height, width = image.shape[:2]
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = image[..., :3]
    rgba[..., 3] = 255
    row_bytes = width * 4
    temporary = durable_io.temporary_path(path, "surface")
    try:
        with temporary.open("wb") as handle:
            handle.write(NATIVE_SURFACE_HEADER.pack(
                NATIVE_SURFACE_MAGIC, width, height, row_bytes))
            handle.write(rgba.tobytes(order="C"))
            handle.flush()
            os.fsync(handle.fileno())
        durable_io.publish_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"width": width, "height": height, "rowBytes": row_bytes}


def read_native_surface(path: Path) -> tuple[np.ndarray, dict]:
    """Read a native surface for cache conversion and contract tests."""
    with path.open("rb") as handle:
        header = handle.read(NATIVE_SURFACE_HEADER.size)
        magic, width, height, row_bytes = NATIVE_SURFACE_HEADER.unpack(header)
        if magic != NATIVE_SURFACE_MAGIC or row_bytes != width * 4:
            raise ValueError("invalid native surface")
        pixels = handle.read()
    expected = row_bytes * height
    if len(pixels) != expected:
        raise ValueError("truncated native surface")
    rgba = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
    return rgba, {"width": width, "height": height, "rowBytes": row_bytes}


def native_surface_payload(key: str, path: Path) -> dict:
    with path.open("rb") as handle:
        magic, width, height, row_bytes = NATIVE_SURFACE_HEADER.unpack(
            handle.read(NATIVE_SURFACE_HEADER.size))
    if magic != NATIVE_SURFACE_MAGIC or row_bytes != width * 4:
        raise ValueError("invalid native surface")
    if path.stat().st_size != NATIVE_SURFACE_HEADER.size + row_bytes * height:
        raise ValueError("truncated native surface")
    return {
        "url": f"/api/render/native?key={key}",
        "format": "rgba8",
        "width": width,
        "height": height,
        "rowBytes": row_bytes,
        "headerBytes": NATIVE_SURFACE_HEADER.size,
    }


def ensure_native_surface(path: Path, jpg: Path) -> None:
    if path.exists() or not jpg.exists():
        return
    with Image.open(jpg) as image:
        write_native_surface(path, np.asarray(image.convert("RGB")))


def ensure_jpeg_surface(jpg: Path, native: Path,
                        max_width: int | None = None) -> None:
    if jpg.exists() or not native.exists():
        return
    rgba, _ = read_native_surface(native)
    image = Image.fromarray(np.asarray(rgba[..., :3]), "RGB")
    if max_width and image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS,
                             reducing_gap=3.0)
    jpg.parent.mkdir(parents=True, exist_ok=True)
    durable_io.atomic_write_bytes(jpg, jpeg_bytes(np.asarray(image)))


def _render_bundle_metadata(meta: Path, jpg: Path, native: Path) -> dict | None:
    """Validate the cheap parts of a disposable render cache bundle.

    The metadata file is the bundle's commit marker. Invalid metadata or a
    truncated native surface is discarded so the normal render path rebuilds
    it. JPEGs are atomically published by current versions and are decoded by
    the normal consumer when conversion is needed.
    """
    if not meta.exists():
        return None
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("render metadata is not an object")
    except (OSError, UnicodeDecodeError, ValueError):
        meta.unlink(missing_ok=True)
        return None
    if native.exists():
        try:
            native_surface_payload("", native)
        except (OSError, ValueError, struct.error):
            native.unlink(missing_ok=True)
    return value


def browser_helper_path(key: str) -> Path:
    return (CACHE / "render-helper" /
            f"{key}_{NATIVE_BROWSER_HELPER_OUTPUT_WIDTH}.jpg")


def preview_response(meta: dict, key: str, jpg: Path, native: Path,
                     *, cached: bool, refining: bool) -> dict:
    response = dict(meta, key=key, cached=cached, refining=refining)
    response.setdefault("queue_ms", 0.0)
    if jpg.exists():
        response["img"] = f"/api/render/image?key={key}"
    if native.exists():
        surface = native_surface_payload(key, native)
        response["native"] = surface
        if surface["width"] <= NATIVE_BROWSER_HELPER_MAX_WIDTH:
            response["helper"] = f"/api/render/helper?key={key}"
    return response


def native_image_payload(url: str, image: bytes | Path) -> dict:
    """Describe a decoded image URL using the geometry Metal must present."""
    source = io.BytesIO(image) if isinstance(image, bytes) else image
    with Image.open(source) as preview:
        width, height = preview.size
    return {
        "url": url, "format": "image", "width": width, "height": height,
        "rowBytes": 0, "headerBytes": 0,
    }


def render_preview(name: str, params: dict, width: int,
                   engine: str = "py", client: str = "",
                   generation: int | None = None,
                   native: bool = False,
                   priority: str = "interactive") -> dict:
    # A decoder or engine crash takes the whole process down, so the photo
    # being processed is recorded first; the next launch reads that marker.
    guard_photo(name)
    with SESSION.inflight("render", library_workflow.source_name(name)):
        previous_priority = getattr(RENDER_CONTEXT, "priority", "export")
        previous_cancelled = getattr(RENDER_CONTEXT, "cancelled", None)
        RENDER_CONTEXT.priority = priority
        RENDER_CONTEXT.cancelled = lambda: render_is_stale(client, generation)
        try:
            return _render_preview(name, params, width, engine, client,
                                   generation, native, priority)
        except RenderCancelled:
            return {"cancelled": True, "reason": "superseded"}
        finally:
            RENDER_CONTEXT.priority = previous_priority
            RENDER_CONTEXT.cancelled = previous_cancelled


def _render_preview(name: str, params: dict, width: int,
                    engine: str = "py", client: str = "",
                    generation: int | None = None,
                    native: bool = False,
                    priority: str = "interactive") -> dict:
    params = dict(params)
    params["linear_input"] = is_raw(name)
    cp = fp.clean_params(params)
    if not cp["profile_enabled"]:
        t0 = time.time()
        accurate = neutral_preview_path(name, width, cp["rotate"], cp)
        refining = bool(is_raw(name) and not accurate.exists())
        if refining:
            preview_image = orig_jpeg(name, width, cp["rotate"])
            image_url = (f"/api/orig?name={quote(name, safe='')}&w={width}"
                         f"&rot={cp['rotate']}&key={file_key(name)}")
        elif is_raw(name):
            preview_image = accurate
            image_url = (f"/api/neutral?name={quote(name, safe='')}&w={width}"
                         f"&rot={cp['rotate']}&rk="
                         f"{color_pipeline.raw_decode_fingerprint(cp)}"
                         f"&key={file_key(name)}")
        else:
            preview_image = orig_jpeg(name, width, cp["rotate"])
            image_url = (f"/api/orig?name={quote(name, safe='')}&w={width}"
                         f"&rot={cp['rotate']}&key={file_key(name)}")
        source_key = hashlib.md5(json.dumps([
            "source-preview-v1", file_key(name), cp, int(width),
        ], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        response = {
            "ms": int((time.time() - t0) * 1000), "match": 1.0,
            "engine": "source", "profile_enabled": False,
            "cached": not refining, "refining": refining,
            "img": image_url, "key": source_key,
        }
        if native:
            response["native"] = native_image_payload(image_url, preview_image)
        return response
    if fp.profile_requires_rust(cp["stock"]):
        if not RUST_AVAILABLE:
            raise RuntimeError(f"{cp['stock']} requires the Rust film engine")
        engine = "rs"
    elif engine == "rs" and not RUST_AVAILABLE:
        engine = "py"
    variant = preview_variant(name, width, params)

    def response_refining() -> bool:
        return bool(is_raw(name) and variant == "fast")

    key = render_key(name, params, width, engine)
    jpg = CACHE / "render" / f"{key}.jpg"
    native_surface = CACHE / "render" / f"{key}.rgba"
    meta = CACHE / "render" / f"{key}.json"
    # Native presentation must not wait for JPEG encoding. A small WebGL
    # helper is generated lazily after interaction settles for histogram,
    # sampling, and reference tools.
    need_jpg = not native
    cached_meta = _render_bundle_metadata(meta, jpg, native_surface)
    if cached_meta is not None:
        if native:
            try:
                ensure_native_surface(native_surface, jpg)
            except (OSError, ValueError):
                jpg.unlink(missing_ok=True)
        if need_jpg:
            try:
                ensure_jpeg_surface(jpg, native_surface)
            except (OSError, ValueError, struct.error):
                native_surface.unlink(missing_ok=True)
    if cached_meta is not None and (not native or native_surface.exists()) and (
            not need_jpg or jpg.exists()):
        return preview_response(
            cached_meta, key, jpg, native_surface, cached=True,
            refining=response_refining())
    if client and generation is not None:
        with GENERATION_LOCK:
            if generation < LATEST_GENERATION.get(client, generation):
                return {"cancelled": True}
    queued_at = time.perf_counter()
    acquired = RENDER_LOCK.acquire(
        blocking=priority != "prefetch", priority=priority,
        cancelled=lambda: render_is_stale(client, generation))
    if not acquired:
        return {"cancelled": True, "reason": "interactive render active"}
    queue_ms = (time.perf_counter() - queued_at) * 1000
    try:
        if client and generation is not None:
            with GENERATION_LOCK:
                if generation < LATEST_GENERATION.get(client, generation):
                    return {"cancelled": True}
        cached_meta = _render_bundle_metadata(meta, jpg, native_surface)
        if cached_meta is not None:
            if native:
                try:
                    ensure_native_surface(native_surface, jpg)
                except (OSError, ValueError):
                    jpg.unlink(missing_ok=True)
            if need_jpg:
                try:
                    ensure_jpeg_surface(jpg, native_surface)
                except (OSError, ValueError, struct.error):
                    native_surface.unlink(missing_ok=True)
        if cached_meta is not None and (
                not native or native_surface.exists()) and (
                not need_jpg or jpg.exists()):      # raced with prefetch
            return dict(preview_response(
                cached_meta, key, jpg, native_surface, cached=True,
                refining=response_refining()), queue_ms=round(queue_ms, 3))
        t0 = time.time()
        arr = None
        rust_metrics = None
        if engine == "rs":
            rust_metrics = render_rust(
                name, params, width,
                jpg if need_jpg else None,
                native_surface if native else None)
            film_mean = float(rust_metrics["mean"])
        else:
            arr = linear_for(name, width, params)
            out = fp.render(arr, params)
        ms = int((time.time() - t0) * 1000)
        if engine != "rs":
            k = rot90k(fp.clean_params(params)["rotate"])
            if k:
                out = np.ascontiguousarray(np.rot90(out, k))
            film_mean = float(out.mean()) / 255.0
            if need_jpg:
                durable_io.atomic_write_bytes(jpg, jpeg_bytes(out))
            if native:
                write_native_surface(native_surface, out)
        orig_mean = orig_mean_display(name, width, arr)
        match = max(0.3, min(2.5, orig_mean / max(film_mean, 1e-6)))
        m = {"ms": ms, "match": round(match, 3), "engine": engine}
        if rust_metrics:
            m["backend"] = rust_metrics.get("backend")
            m["gpu_ms"] = rust_metrics.get("render_ms")
            m["resident_ms"] = rust_metrics.get("total_ms")
            m["input_cache_hit"] = rust_metrics.get("input_cache_hit")
            m["pipeline_cache_hit"] = rust_metrics.get("pipeline_cache_hit")
        durable_io.atomic_write_json(meta, m, indent=None, keep_backup=False)
        prune_render_cache_throttled(meta.parent, _RENDER_CACHE_MAX_BYTES)
        return dict(preview_response(
            m, key, jpg, native_surface, cached=False,
            refining=response_refining()), queue_ms=round(queue_ms, 3))
    finally:
        RENDER_LOCK.release()


def _preview_source_bytes(result: dict, name: str, width: int,
                          rotate: float) -> bytes:
    url = str(result.get("img", ""))
    if url.startswith("/api/render/image"):
        key = parse_qs(urlparse(url).query).get("key", [""])[0]
        path = CACHE / "render" / f"{key}.jpg"
        if path.is_file():
            return path.read_bytes()
    if url.startswith("/api/neutral"):
        raw_key = parse_qs(urlparse(url).query).get("rk", [None])[0]
        path = neutral_preview_path(name, width, rotate, raw_key=raw_key)
        if path.is_file():
            return path.read_bytes()
    native_url = str((result.get("native") or {}).get("url", ""))
    if native_url.startswith("/api/render/native"):
        key = parse_qs(urlparse(native_url).query).get("key", [""])[0]
        surface = CACHE / "render" / f"{key}.rgba"
        image = CACHE / "render" / f"{key}.jpg"
        ensure_jpeg_surface(image, surface)
        if image.is_file():
            return image.read_bytes()
    return orig_jpeg(name, width, rotate)


def native_base_edits_required(optics=None, heals=None) -> bool:
    """Bake only corrections outside the live Metal preview's contract."""
    return (edits.clean_optics(optics)["profileEnabled"]
            or sum(spot["enabled"] for spot in edits.clean_heals(heals)) > 16)


def _native_corrected_preview(result: dict, name: str, width: int,
                              params: dict, optics, heals) -> dict:
    cleaned_optics = edits.clean_optics(optics)
    cleaned_heals = edits.clean_heals(heals)
    profile = edits.lens_profile_for(exif_for(name))
    token = json.dumps([
        "native-base-edits-v1", file_key(name), result.get("key"),
        result.get("img"), (result.get("native") or {}).get("url"),
        width, cleaned_optics, cleaned_heals, profile,
    ], sort_keys=True, separators=(",", ":"))
    key = hashlib.md5(token.encode()).hexdigest()
    surface = CACHE / "render" / f"{key}.rgba"
    jpg = surface.with_suffix(".jpg")
    metadata = surface.with_suffix(".json")
    started = time.perf_counter()
    cached = _render_bundle_metadata(metadata, jpg, surface) is not None
    if cached:
        ensure_native_surface(surface, jpg)
    cached = cached and surface.exists()
    if not cached:
        native_url = str((result.get("native") or {}).get("url", ""))
        if native_url.startswith("/api/render/native?"):
            base_key = parse_qs(urlparse(native_url).query)["key"][0]
            rgba, _ = read_native_surface(CACHE / "render" / f"{base_key}.rgba")
            image = rgba[..., :3].astype(np.float32) / 255.0
        else:
            base = Image.open(io.BytesIO(_preview_source_bytes(
                result, name, width, fp.clean_params(params)["rotate"]))).convert("RGB")
            image = np.asarray(base, dtype=np.float32) / 255.0
        adjusted = edits.apply_base(image, cleaned_optics, cleaned_heals, profile)
        write_native_surface(surface, adjusted)
        # Publish metadata last, matching the ordinary render bundle contract.
        durable_io.atomic_write_json(metadata, {"baseEditsBaked": True},
                                     indent=None, keep_backup=False)
        prune_render_cache_throttled(surface.parent, _RENDER_CACHE_MAX_BYTES)
    response = {field: value for field, value in result.items()
                if field not in {"img", "native", "helper", "key"}}
    response.update(baseEditsBaked=True, edit_cached=cached,
                    edit_ms=round((time.perf_counter() - started) * 1000, 2),
                    lens_profile=profile)
    return preview_response(
        response, key, jpg, surface, cached=bool(result.get("cached")) and cached,
        refining=bool(result.get("refining")))


def apply_preview_edits(result: dict, name: str, width: int,
                        params: dict, optics=None, heals=None, *,
                        native: bool = False) -> dict:
    """Apply cached geometry/healing after the expensive base render.

    Global and local-mask colour work remains in WebGL for instant sliders;
    this stage is reserved for operations that resample source pixels.
    """
    if result.get("cancelled") or result.get("error"):
        return result
    if native:
        if native_base_edits_required(optics, heals):
            return _native_corrected_preview(result, name, width, params, optics, heals)
        return dict(result, baseEditsBaked=False)
    if edits.base_edits_are_identity(optics, heals):
        return result
    cleaned_optics = edits.clean_optics(optics)
    cleaned_heals = edits.clean_heals(heals)
    profile = edits.lens_profile_for(exif_for(name))
    rotate = fp.clean_params(params).get("rotate", 0)
    token = json.dumps([
        EDIT_PREVIEW_CACHE_VERSION, file_key(name), result.get("img"),
        (result.get("native") or {}).get("url"), width,
        cleaned_optics, cleaned_heals, profile,
    ], sort_keys=True, separators=(",", ":"))
    key = hashlib.md5(token.encode()).hexdigest()
    output = CACHE / "edit" / f"{key}.jpg"
    started = time.perf_counter()
    cached = output.is_file()
    if not cached:
        source = Image.open(io.BytesIO(
            _preview_source_bytes(result, name, width, rotate))).convert("RGB")
        image = np.asarray(source, dtype=np.float32) / 255.0
        adjusted = edits.apply_base(image, cleaned_optics, cleaned_heals, profile)
        durable_io.atomic_write_bytes(
            output,
            jpeg_bytes(
                (np.clip(adjusted, 0, 1) * 255 + 0.5).astype(np.uint8),
                quality=90,
            ),
        )
        prune_cache(output.parent, "*.jpg", _EDIT_CACHE_MAX_BYTES)
    result = dict(result)
    result.pop("native", None)
    result.pop("helper", None)
    result.update(
        img=f"/api/edit/image?key={key}",
        key=key,
        edit_cached=cached,
        edit_ms=round((time.perf_counter() - started) * 1000, 2),
        lens_profile=profile,
    )
    return result


# --------------------------------------------------------------- export ----

EXPORT = {"total": 0, "done": 0, "errors": [], "warnings": [], "running": False, "log": []}
EXPORT_LOCK = threading.Lock()
EXPORT_PATH_LOCK = threading.Lock()
EXPORT_RESERVED_PATHS: set[Path] = set()
EXPORT_FILM_LOCK = threading.Lock()
EXPORT_POOL = ThreadPoolExecutor(max_workers=2)
MERGE = {"running": False, "mode": "", "progress": 0, "total": 0,
         "phase": "", "phaseProgress": 0, "phaseTotal": 0,
         "alignmentInliers": 0, "elapsedSeconds": 0.0,
         "timings": {}, "output": "", "error": ""}
MERGE_LOCK = threading.Lock()
MERGE_POOL = ThreadPoolExecutor(max_workers=1)
MERGE_MAX_EDGE = int(os.environ.get("LIGHTTABLE_MERGE_MAX_EDGE", "6000"))
DENOISE = {"running": False, "name": "", "progress": 0, "total": 0,
           "error": "", "cancelled": False, "done": False}
DENOISE_LOCK = threading.Lock()
DENOISE_POOL = ThreadPoolExecutor(max_workers=1)
EXTERNAL_EDIT = {"running": False, "total": 0, "done": 0,
                 "paths": [], "names": [], "errors": [], "warnings": []}
EXTERNAL_EDIT_LOCK = threading.Lock()
EXTERNAL_EDIT_POOL = ThreadPoolExecutor(max_workers=1)


def cancel_denoise_job() -> None:
    with DENOISE_LOCK:
        DENOISE["cancelled"] = True


def sync_job_status(status: dict, *, progress_key: str = "done",
                    result: dict | None = None) -> dict | None:
    """Mirror an established workflow dict into the uniform job registry."""
    ident = status.get("jobId")
    if not ident or JOBS.get(str(ident)) is None:
        return None
    errors = list(status.get("errors") or [])
    if status.get("error"):
        errors.append(str(status["error"]))
    if status.get("running") or status.get("active"):
        state = "running"
    elif status.get("cancelled") or status.get("cancel_requested"):
        state = "cancelled"
    elif errors:
        state = "failed"
    else:
        state = "done"
    return JOBS.update(
        str(ident), state=state,
        progress=int(status.get(progress_key, status.get("progress", 0)) or 0),
        total=int(status.get("total", 0) or 0), errors=errors,
        log=list(status.get("log") or []),
        result=result if result is not None else {
            key: value for key, value in status.items()
            if key not in {"jobId", "errors", "error", "log"}
        },
    )


def _export_metadata_payload(job: dict) -> tuple[str, Path | None, dict]:
    """Resolve metadata once for either staged or post-encode embedding."""
    policy = str(job.get("metadata", "all-except-location"))
    try:
        source = src_path(job["sourceName"]) if job.get("sourceName") else None
    except (ValueError, KeyError):
        source = None
        if policy in ("all", "all-except-location"):
            job.setdefault("warnings", []).append(
                "Source camera metadata could not be located.")
    return policy, source, job.get("metadataFields") or {}


def export_input_color_space(job: dict) -> str:
    """Record the source encoding and visibly report remaining gamut limits."""
    cp = fp.clean_params(job.get("params") or {})
    output_space = color_pipeline.normalise_output_space(job.get("outputSpace"))
    wide = (not cp["profile_enabled"] and output_space != "srgb"
            and color_pipeline.wide_develop_edits_supported(job))
    job["inputColorSpace"] = output_space if wide else "srgb"
    if output_space != "srgb" and not wide:
        warning = color_pipeline.SRGB_LIMITED_EXPORT_WARNING
        warnings = job.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
    return job["inputColorSpace"]


def export_render_source(name: str, job: dict) -> Path:
    """Choose a render source and record the color encoding passed to the CLI."""
    color_pipeline.required_icc_bytes(job.get("outputSpace", "srgb"))
    input_space = export_input_color_space(job)
    if fp.clean_params(job.get("params") or {})["profile_enabled"]:
        return tiff_for(name, job["params"])
    # Neutral TIFFs already contain the Develop transfer function, regardless
    # of whether the original capture was RAW.
    job["params"] = fp.clean_params(dict(job.get("params") or {}, linear_input=False))
    return neutral_tiff_for(name, job["params"],
                            output_space=input_space)


def finish_export(film_png: Path, dst: Path, job: dict) -> tuple[int, int]:
    """Apply the display grade/crop and encode a full-precision film render."""
    out = color_pipeline.load_float_rgb(film_png)
    out = edits.apply_base(
        out, job.get("optics"), job.get("heals"), job.get("lensProfile"))
    g = job.get("grade") or {}
    if not grade.is_identity(g):
        out = np.clip(grade.apply(out, g), 0, 1).astype(np.float32)
    out = edits.apply_masks(out, job.get("masks"))

    crop = job.get("crop")
    if crop:
        height, width = out.shape[:2]
        x0 = int(round(crop["x"] * width))
        y0 = int(round(crop["y"] * height))
        x1 = min(width, x0 + max(1, int(round(crop["w"] * width))))
        y1 = min(height, y0 + max(1, int(round(crop["h"] * height))))
        out = np.ascontiguousarray(out[y0:y1, x0:x1])

    out = color_pipeline.resize_float(out, job.get("longEdge"))
    # The mark is applied after resizing so it scales with the delivered image
    # rather than being enlarged or shrunk with the pixels beneath it.
    out = export_workflow.apply_watermark(out, job.get("watermark"), APP)
    fmt = job.get("format", "jpeg")
    is_heif = str(fmt).lower() in ("heif", "heic")
    policy, metadata_source, metadata_fields = _export_metadata_payload(job)
    size = color_pipeline.save_export_image(
        out, dst, fmt=fmt, quality=int(job.get("quality", 92)),
        output_space=str(job.get("outputSpace", "srgb")),
        bit_depth=int(job.get("bitDepth", 16)),
        metadata_source=metadata_source if is_heif else None,
        metadata_policy=policy if is_heif else "none",
        metadata_fields=metadata_fields if is_heif else None,
        warnings=job.setdefault("warnings", []))
    if not is_heif:
        embed_export_metadata(dst, job)
    return size


def export_metadata_fields(name: str) -> dict:
    """Catalog metadata for one image, in the shape write_metadata expects."""
    cat = catalog_handle()
    if cat is None:
        return {}
    image_id = catalog_image_id(name)
    if image_id is None:
        return {}
    iptc = cat.iptc_for(image_id)
    state = cat.state_for(image_id)
    fields = {key: iptc.get(key) for key in
              ("title", "caption", "creator", "copyright")
              if iptc.get(key)}
    keywords = state.get("keywords") or []
    if keywords:
        # Flat terms for readers that only understand dc:subject, and the
        # full paths for those that understand the hierarchy.
        fields["keywords"] = [path.rsplit(" > ", 1)[-1] for path in keywords]
        fields["keywordPaths"] = keywords
    if state.get("rating"):
        fields["rating"] = int(state["rating"])
    if state.get("label") and state["label"] != "none":
        fields["label"] = state["label"]
    return fields


def embed_export_metadata(dst: Path, job: dict) -> bool:
    """Write the requested metadata into a finished export.

    Exports used to carry only an ICC profile, which made them unusable for
    client delivery: no copyright, no creator, no caption. The policy lives on
    the recipe so a web JPEG and an archive master can differ.
    """
    policy, source, fields = _export_metadata_payload(job)
    if policy == "none":
        return False
    warnings = job.setdefault("warnings", [])
    before = len(warnings)
    succeeded = platform_image.write_metadata(
        dst, source, policy, fields, warnings=warnings)
    if not succeeded and len(warnings) == before:
        warnings.append("Requested metadata could not be saved.")
    return succeeded


def rust_direct_export_supported(job: dict) -> bool:
    """Whether Rust can finish this recipe without changing its semantics."""
    if str(job.get("format", "jpeg")).lower() not in ("jpeg", "jpg"):
        return False
    if color_pipeline.normalise_output_space(
            str(job.get("outputSpace", "srgb"))) != "srgb":
        return False
    if not edits.base_edits_are_identity(job.get("optics"), job.get("heals")):
        return False
    if export_workflow.clean_watermark(job.get("watermark"))["enabled"]:
        return False
    # Rust reproduces geometric and bitmap masks. Brush rasterization still
    # uses Pillow's Gaussian stroke contract and therefore stays on Python.
    for mask in edits.clean_masks(job.get("masks")):
        if any(component.get("type") == "brush"
               for component in mask.get("components", [])):
            return False
    return True


def _resident_render_full(name: str, params: dict, request: dict) -> dict:
    """Render a full-resolution source using shared RAW pixels when possible."""
    request = dict(request)
    if is_raw(name) and os.name == "posix":
        try:
            with raw_shared_input(name, params) as shared:
                request.update(shared)
                metrics = RUST_ENGINE.render(request)
            metrics["input_transport"] = "shared-memory-rgb16"
            metrics["input_exchange_bytes"] = int(shared["input_shm_len"])
            return metrics
        except RenderCancelled:
            raise
        except Exception as shared_error:  # noqa: BLE001
            # A platform may expose POSIX shared memory yet cap a segment below
            # a large sensor frame. Retain the proven TIFF route rather than
            # making export brittle.
            for key in ("input_shm", "input_shm_len", "input_cache_key"):
                request.pop(key, None)
            source = tiff_for(name, params)
            request["input"] = str(source)
            metrics = RUST_ENGINE.render(request)
            metrics["input_transport"] = "tiff-fallback"
            metrics["input_exchange_bytes"] = source.stat().st_size
            metrics["input_fallback"] = type(shared_error).__name__
            return metrics
    source = tiff_for(name, params)
    request["input"] = str(source)
    metrics = RUST_ENGINE.render(request)
    metrics["input_transport"] = "tiff"
    metrics["input_exchange_bytes"] = source.stat().st_size
    return metrics


def export_with_resident_engine(name: str, dst: Path, job: dict) -> dict:
    # Check before a renderer spends work or creates any untagged output.
    profile = color_pipeline.required_icc_bytes(job.get("outputSpace", "srgb"))
    export_input_color_space(job)
    params = fp.clean_params(dict(job["params"], linear_input=is_raw(name)))
    cp = fp.clean_params(params)
    direct_error = None
    if rust_direct_export_supported(job):
        request = {
            "output": str(dst),
            "data_dir": str(RUST_DATA), "film": cp["stock"],
            "paper": cp["paper"],
            "scan_film": cp["stock"] in fp.POSITIVE_STOCKS,
            "params": fp.rust_params_json(params),
            "rotate_quarters_ccw": rot90k(cp["rotate"]),
            "quality": int(job.get("quality", 92)),
            "grade": grade.clean(job.get("grade") or {}),
            "masks": edits.clean_masks(job.get("masks")),
            "crop": clean_crop(job.get("crop")),
            "long_edge": job.get("longEdge"),
        }
        try:
            metrics = _resident_render_full(name, params, request)
            platform_image.embed_jpeg_icc(dst, profile)
            embed_export_metadata(dst, job)
            return dict(metrics, width=int(metrics["width"]),
                        height=int(metrics["height"]), direct_export=True)
        except RenderCancelled:
            raise
        except Exception as error:  # noqa: BLE001
            # A direct-path defect must never cost the user an export. Remove
            # its staged bytes and run the established high-precision path.
            dst.unlink(missing_ok=True)
            direct_error = type(error).__name__

    cache_key = render_key(name, params, 0, "rs-export")
    film_png = CACHE / "export-film" / f"{cache_key}.tif"
    metrics = {"cached": True, "total_ms": 0.0}
    with EXPORT_FILM_LOCK:
        cancelled = getattr(RENDER_CONTEXT, "cancelled", None)
        if cancelled and cancelled():
            raise RenderCancelled("export cancelled before rendering")
        if not film_png.exists():
            staged = durable_io.temporary_path(film_png, "film")
            try:
                request = {
                    "output": str(staged),
                    "data_dir": str(RUST_DATA), "film": cp["stock"],
                    "paper": cp["paper"],
                    "scan_film": cp["stock"] in fp.POSITIVE_STOCKS,
                    "params": fp.rust_params_json(params),
                    "rotate_quarters_ccw": rot90k(cp["rotate"]),
                    "bit_depth": 32,
                }
                metrics = _resident_render_full(name, params, request)
                durable_io.publish_file(staged, film_png)
            finally:
                staged.unlink(missing_ok=True)
            prune_cache(film_png.parent, "*.tif", _EXPORT_FILM_CACHE_MAX_BYTES)
    if cancelled and cancelled():
        raise RenderCancelled("export cancelled before encoding")
    width, height = finish_export(film_png, dst, job)
    return dict(metrics, width=width, height=height,
                direct_export=False, direct_fallback=direct_error)


def _external_job(name: str, output_space: str, bit_depth: int = 16) -> dict:
    entry = catalog_entry_for(name)
    return {
        "sourceName": name,
        "params": fp.clean_params(entry.get("params") or {}),
        "grade": grade.clean(entry.get("grade") or {}),
        "crop": clean_crop(entry.get("crop")),
        "masks": edits.clean_masks(entry.get("masks")),
        "heals": edits.clean_heals(entry.get("heals")),
        "optics": edits.clean_optics(entry.get("optics")),
        "format": "tif", "quality": 100, "outputSpace": output_space,
        "longEdge": None, "watermark": {"enabled": False},
        "metadata": "all", "metadataFields": export_metadata_fields(name),
        "copyMetadataFrom": True, "engine": "rs",
        "bitDepth": 8 if int(bit_depth) == 8 else 16,
        "lensProfile": edits.lens_profile_for(exif_for(name)),
    }


def _render_external_job(name: str, destination: Path, job: dict) -> None:
    staged = durable_io.temporary_path(destination, "external-edit")
    job_file = None
    try:
        cp = fp.clean_params(job["params"])
        if RUST_WORKER_BIN and cp["profile_enabled"]:
            export_with_resident_engine(name, staged, job)
        else:
            job_file = durable_io.temporary_path(
                CACHE / "external-edit-job.json", "job")
            source = export_render_source(name, job)
            durable_io.atomic_write_text(job_file, json.dumps(job))
            completed = subprocess.run(
                [sys.executable, str(APP / "render_cli.py"), str(source),
                 str(staged), str(job_file)],
                capture_output=True, text=True,
                env=dict(os.environ, OMP_NUM_THREADS="4", NUMBA_NUM_THREADS="4"),
                timeout=1800, check=False)
            if completed.returncode:
                raise RuntimeError((completed.stderr or completed.stdout)[-300:])
            for line in reversed((completed.stdout or "").splitlines()):
                try:
                    result = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(result, dict) and result.get("ok"):
                    warnings = job.setdefault("warnings", [])
                    for warning in result.get("warnings") or []:
                        if str(warning) not in warnings:
                            warnings.append(str(warning))
                    break
        durable_io.publish_file_no_replace(staged, destination)
        staged.unlink(missing_ok=True)
    finally:
        staged.unlink(missing_ok=True)
        if job_file is not None:
            job_file.unlink(missing_ok=True)


def _run_external_edits(names: list[str], output_space: str,
                        bit_depth: int = 16,
                        stack_with_original: bool = True) -> None:
    cat = catalog_handle()
    try:
        for name in names:
            try:
                source = src_path(name)
                parent = source.parent
                mode_bits = parent.stat().st_mode & 0o222
                if not mode_bits or not os.access(parent, os.W_OK):
                    raise PermissionError(f"Cannot write to {parent}")
                destination = export_workflow.collision_path(
                    parent / f"{source.stem}-Edit.tif", "rename")
                if destination is None:
                    raise FileExistsError("Could not choose an edit filename")
                job = _external_job(name, output_space, bit_depth)
                _render_external_job(name, destination, job)
                derivative_name = destination.name
                if cat is not None:
                    original_id = catalog_image_id(name)
                    derivative_id = catalog_scan.register_file(cat, destination)
                    if original_id is not None:
                        cat.copy_organizational_state(original_id, derivative_id)
                        if stack_with_original:
                            stack_id = cat.stack_id_for(original_id)
                            if stack_id is None:
                                cat.add_stack(source.stem,
                                              [original_id, derivative_id])
                            else:
                                cat.add_to_stack(stack_id, derivative_id)
                    row = cat.image_row(derivative_id)
                    if row:
                        derivative_name = catalog_module.qualified_name(
                            row["source_id"], row["relpath"])
                with EXTERNAL_EDIT_LOCK:
                    EXTERNAL_EDIT["paths"].append(str(destination))
                    EXTERNAL_EDIT["names"].append(derivative_name)
                    if job.get("warnings"):
                        EXTERNAL_EDIT["warnings"].append({
                            "name": name, "path": str(destination),
                            "warnings": list(job["warnings"])})
                if WATCH_SERVICE is not None:
                    WATCH_SERVICE.add_session_path(destination)
            except Exception as error:
                with EXTERNAL_EDIT_LOCK:
                    EXTERNAL_EDIT["errors"].append(f"{name}: {error}")
            finally:
                with EXTERNAL_EDIT_LOCK:
                    EXTERNAL_EDIT["done"] += 1
                    sync_job_status(dict(EXTERNAL_EDIT))
        with EXTERNAL_EDIT_LOCK:
            EXTERNAL_EDIT["running"] = False
            sync_job_status(dict(EXTERNAL_EDIT))
        invalidate_library_cache()
    finally:
        if cat is not None:
            cat.close()


def start_external_edit(body: dict) -> dict:
    names = list(dict.fromkeys(str(value) for value in
                              (body.get("names") or []) if value))[:100]
    if not names:
        return {"ok": False, "error": "Choose a photo to edit"}
    mode = str(body.get("mode", "adjusted"))
    if mode not in ("adjusted", "original"):
        return {"ok": False, "error": "Unknown external edit mode"}
    paths = []
    for name in names:
        if is_video(name):
            return {"ok": False, "error": "Video cannot be sent to a pixel editor"}
        if mode == "original":
            if is_raw(name):
                return {"ok": False, "error":
                        "A RAW original cannot be edited in place; send an adjusted TIFF"}
            paths.append(str(src_path(name)))
    if mode == "original":
        return {"ok": True, "running": False, "paths": paths, "names": names}
    output_space = str(body.get("space", "prophoto")).lower()
    if output_space not in ("prophoto", "display_p3", "srgb"):
        output_space = "prophoto"
    bit_depth = 8 if int(body.get("bitDepth", 16)) == 8 else 16
    stack_with_original = body.get("stackWithOriginal", True) is not False
    with EXTERNAL_EDIT_LOCK:
        if EXTERNAL_EDIT["running"]:
            return {"ok": False, "error": "An external edit is already rendering"}
        EXTERNAL_EDIT.update(running=True, total=len(names), done=0,
                             paths=[], names=[], errors=[], warnings=[])
        EXTERNAL_EDIT["jobId"] = JOBS.create(
            "external-edit", total=len(names), state="running")["id"]
    EXTERNAL_EDIT_POOL.submit(
        _run_external_edits, names, output_space, bit_depth,
        stack_with_original)
    return {"ok": True, "running": True, "queued": len(names)}


def benchmark_export(name: str, job: dict) -> dict:
    """Exercise the same resident export path without writing into the photo folder."""
    started = time.perf_counter()
    dst = CACHE / "benchmark-export.jpg"
    metrics = export_with_resident_engine(name, dst, job)
    result = {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": 0,
        "output_bytes": dst.stat().st_size,
        "source_tiff_bytes": (metrics.get("input_exchange_bytes", 0)
                              if "tiff" in str(metrics.get("input_transport", ""))
                              else 0),
        "input_exchange_bytes": metrics.get("input_exchange_bytes", 0),
        "input_transport": metrics.get("input_transport"),
        "backend": metrics.get("backend"),
        "gpu_ms": metrics.get("render_ms"),
        "resident_ms": metrics.get("total_ms"),
        "width": metrics.get("width"),
        "height": metrics.get("height"),
    }
    dst.unlink(missing_ok=True)
    return result


class ExportCancelled(Exception):
    pass


def export_would_replace_original(destination: Path, source_name: str) -> bool:
    path = destination.resolve()
    source = src_path(source_name).resolve()
    if path == source:
        return True
    # A RAW export next to its capture must not overwrite the camera JPEG.
    if path.exists() and path.parent == source.parent and path.stem.casefold() == source.stem.casefold():
        return True
    cat = catalog_handle()
    if path.exists() and cat is not None:
        for row in cat.sources():
            try:
                relative = path.relative_to(Path(row["path"]).resolve()).as_posix()
            except ValueError:
                continue
            if cat.image_id_for(int(row["id"]), relative) is not None:
                return True
    return False


class ExportBatch:
    """Keep only two workers active; this batch owns its status until cleanup."""
    def __init__(self, items, destination):
        self.items = iter(items)
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.active = 0
        self.remaining = len(items)
        self.status = dict(total=len(items), done=0, completed=0, skipped=0,
                           cancelledCount=0, errors=[], warnings=[], running=bool(items),
                           cancel_requested=False, log=[], destination=str(destination))

    def publish_status(self):
        with EXPORT_LOCK:
            if EXPORT.get("jobId") == self.status.get("jobId"):
                EXPORT.clear()
                EXPORT.update(self.status)
                sync_job_status(dict(self.status))

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            self.status["cancel_requested"] = True
            self.status["cancelledCount"] += self.remaining
            self.status["done"] += self.remaining
            self.remaining = 0
            self.publish_status()
        return False

    def check(self):
        if self.cancelled.is_set():
            raise ExportCancelled()

    def dispatch(self):
        with self.lock:
            if self.cancelled.is_set() or not self.remaining:
                return
            name, job = next(self.items)
            self.remaining -= 1
            self.active += 1
            try:
                EXPORT_POOL.submit(export_one, name, job, self)
            except Exception as error:
                self.finish(name, {"error": str(error)})

    def finish(self, name, outcome):
        with self.lock:
            self.active -= 1
            self.status["done"] += 1
            for key in ("completed", "skipped", "cancelledCount"):
                self.status[key] += int(outcome.get(key, 0))
            if outcome.get("error"):
                self.status["errors"].append(f"{name}: {outcome['error']}")
            if outcome.get("log"):
                self.status["log"].append(outcome["log"])
                self.status["log"] = self.status["log"][-200:]
            if outcome.get("warnings"):
                self.status["warnings"].append({
                    "name": name, "path": outcome.get("path"),
                    "warnings": outcome["warnings"]})
            self.dispatch()
            if not self.remaining and not self.active:
                self.status["running"] = False
                self.status["cancelled"] = self.cancelled.is_set()
            self.publish_status()


def _run_export_process(command, env, batch):
    if batch is None:
        return subprocess.run(command, capture_output=True, text=True, env=env, timeout=1800)
    batch.check()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env) as process:
        try:
            for _ in range(12000):
                batch.check()
                try:
                    stdout, stderr = process.communicate(timeout=0.15)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pass
            raise TimeoutError("Export renderer exceeded 30 minutes")
        except BaseException:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise


def export_one(name: str, job: dict, batch: ExportBatch | None = None) -> None:
    previous = getattr(RENDER_CONTEXT, "cancelled", None)
    if batch:
        RENDER_CONTEXT.cancelled = batch.cancelled.is_set
    try:
        with SESSION.inflight("export", library_workflow.source_name(name)):
            outcome = _export_one(name, job, batch)
    except Exception as error:
        outcome = {"error": str(error)}
    finally:
        RENDER_CONTEXT.cancelled = previous
    if batch:
        batch.finish(name, outcome)
    else:
        # Preserve the direct worker entry point used by integrations.
        with EXPORT_LOCK:
            if outcome.get("error"):
                EXPORT["errors"].append(f"{name}: {outcome['error']}")
            if outcome.get("log"):
                EXPORT["log"].append(outcome["log"])
            EXPORT["done"] += 1
            if EXPORT["done"] >= EXPORT["total"]:
                EXPORT["running"] = False
            sync_job_status(dict(EXPORT))


def _export_one(name: str, job: dict, batch: ExportBatch | None = None) -> dict:
    dst: Path | None = None
    staged: Path | None = None
    staged_sidecar: Path | None = None
    outcome = {}
    check = batch.check if batch else lambda: None
    try:
        check()
        guard_photo(name)
        started = time.perf_counter()
        job = dict(job)
        job["warnings"] = list(job.get("warnings") or [])
        out_dir = Path(job["destination"])
        if "destinationMode" in job and out_dir.resolve() != out_dir:
            raise ValueError("The export destination changed after planning; preview it again")
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        cp = fp.clean_params(job["params"])
        # Resolve metadata only for the worker's source, as in preview.
        metadata = exif_for(name)
        job["lensProfile"] = edits.lens_profile_for(metadata)
        job["metadataFields"] = export_metadata_fields(name)
        requested = export_requested_path(name, job, metadata)
        check()
        with EXPORT_PATH_LOCK:
            dst = export_workflow.collision_path(
                requested, job.get("collision", "rename"),
                EXPORT_RESERVED_PATHS,
                (".lighttable.json",) if job.get("sidecar", True) else (),
            )
            if dst is not None:
                EXPORT_RESERVED_PATHS.add(dst)
        if dst is None:
            outcome = {"skipped": 1, "log": f"{name} skipped (file already exists)"}
            return outcome
        if export_would_replace_original(dst, name):
            raise ValueError("An export cannot replace a cataloged original or its capture companion")
        staged = durable_io.temporary_path(dst, "export")
        check()
        if RUST_WORKER_BIN and cp["profile_enabled"]:
            metrics = export_with_resident_engine(name, staged, job)
            detail = (f"{metrics.get('backend', 'cache')} "
                      f"{metrics.get('total_ms', 0):.0f} ms GPU")
        else:
            # Parallel exports of the same photo may use different recipes.
            # A unique job file prevents one worker from reading or deleting
            # another worker's instructions.
            jfile = durable_io.temporary_path(
                CACHE / "export-job.json", "job")
            job = dict(job)
            job["params"] = fp.clean_params(dict(
                job["params"],
                linear_input=is_raw(name) if cp["profile_enabled"] else False))
            # The worker script cannot resolve catalog names, and the render
            # source it receives is an intermediate TIFF without camera EXIF.
            # Name the original capture so this path embeds the same metadata
            # the resident engine path does.
            _, metadata_source, _ = _export_metadata_payload(job)
            if metadata_source is not None:
                job["metadataSource"] = str(metadata_source)
            render_source = export_render_source(name, job)
            durable_io.atomic_write_text(jfile, json.dumps(job))
            env = dict(os.environ, OMP_NUM_THREADS="4", NUMBA_NUM_THREADS="4")
            try:
                check()
                r = _run_export_process(
                    [sys.executable, str(APP / "render_cli.py"),
                     str(render_source), str(staged), str(jfile)], env, batch)
            finally:
                jfile.unlink(missing_ok=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip()[-300:])
            stdout = getattr(r, "stdout", "")
            if isinstance(stdout, str):
                for line in reversed(stdout.splitlines()):
                    try:
                        payload = json.loads(line)
                        job["warnings"].extend(payload.get("warnings") or [])
                        break
                    except (ValueError, AttributeError):
                        continue
            detail = "Python fallback"
        check()
        if job.get("preserveCaptureTime"):
            warning = export_workflow.apply_capture_timestamp(
                staged, metadata, job.get("captureTimePolicy", "require-offset"))
            if warning:
                job["warnings"].append(warning)
        # A client delivery should not carry a machine-readable recipe next to
        # it, so the sidecar is a recipe choice rather than a fixed behaviour.
        if job.get("sidecar", True):
            sidecar = dst.with_suffix(dst.suffix + ".lighttable.json")
            sidecar_payload = {
                "source": name,
                "output": dst.name,
                "params": fp.clean_params(job["params"]),
                "grade": grade.clean(job.get("grade", {})),
                "crop": clean_crop(job.get("crop")),
                "masks": edits.clean_masks(job.get("masks")),
                "heals": edits.clean_heals(job.get("heals")),
                "optics": edits.clean_optics(job.get("optics")),
                "lensProfile": job.get("lensProfile"),
                "outputSpace": job.get("outputSpace", "srgb"),
                "provenance": job.get("provenance") or renderer_provenance(),
            }
            staged_sidecar = durable_io.temporary_path(sidecar, "export-sidecar")
            durable_io.atomic_write_json(staged_sidecar, sidecar_payload, indent=2)
        # Cancellation and publication share a short critical section. Once
        # publication starts, this complete output survives later cancellation.
        publish_lock = batch.lock if batch else EXPORT_PATH_LOCK
        with publish_lock:
            check()
            if dst.parent.resolve() != out_dir:
                raise ValueError("The export destination moved while rendering; nothing was published")
            if export_would_replace_original(dst, name):
                raise ValueError("The destination is an original photo; export was not published")
            if job.get("collision", "rename") == "overwrite":
                durable_io.publish_file(staged, dst)
            else:
                durable_io.publish_file_no_replace(staged, dst)
                staged.unlink(missing_ok=True)
            staged = None
            if staged_sidecar is not None:
                try:
                    if job.get("collision", "rename") == "overwrite":
                        durable_io.publish_file(staged_sidecar, sidecar)
                    else:
                        durable_io.publish_file_no_replace(staged_sidecar, sidecar)
                except OSError as error:
                    job["warnings"].append(f"Photo exported, but its recipe sidecar could not be saved: {error}")
        elapsed = time.perf_counter() - started
        outcome = {"completed": 1, "path": str(dst), "warnings": list(dict.fromkeys(job["warnings"])),
                   "log": f"{name} -> {dst.name} ({elapsed:.1f}s, {detail})"}
    except (ExportCancelled, RenderCancelled):
        outcome = {"cancelledCount": 1}
    except Exception as error:
        outcome = {"error": str(error)}
    finally:
        for temporary in (staged, staged_sidecar):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    outcome["error"] = f"Could not remove incomplete export {temporary}: {error}"
        if dst is not None:
            with EXPORT_PATH_LOCK:
                EXPORT_RESERVED_PATHS.discard(dst)
        cat = catalog_handle()
        if cat is not None:
            try:
                cat.close()
            except Exception as error:
                outcome["error"] = f"Could not close export catalog connection: {error}"
    return outcome


def export_candidates() -> list[tuple[str, dict, str]]:
    """Return every exportable image from the authoritative live store."""
    cat = catalog_handle()
    if cat is None:
        st = load_state()
        copies = {item["name"]: item
                  for item in library_state(st)["virtualCopies"]}
        return [
            (name, entry_for(st, name),
             copies[name]["displayName"] if name in copies else
             str(Path(library_workflow.source_name(name)).with_suffix("")))
            for name in library_item_names(st) if not is_video(name)
        ]

    candidates: list[tuple[str, dict, str]] = []
    offset = 0
    while True:
        page = cat.query({
            "limit": 5000, "offset": offset,
            "sort": {"field": "name", "dir": "asc"},
        }, include_state=True)
        for item in page["items"]:
            if item["kind"] == "video":
                continue
            entry = {
                "status": item.get("status", "pending"),
                "rating": int(item.get("rating", 0) or 0),
                "label": clean_label(item.get("label")),
                "params": item.get("params"),
                "grade": item.get("grade"),
                "crop": item.get("crop"),
                "masks": edits.clean_masks(item.get("masks")),
                "heals": edits.clean_heals(item.get("heals")),
                "optics": edits.clean_optics(item.get("optics")),
                "keywords": clean_keywords(item.get("keywords", [])),
                "provenance": item.get("provenance"),
            }
            base = (item["displayName"] if item["virtual"] else
                    str(Path(item["relpath"]).with_suffix("")))
            candidates.append((item["name"], entry, base))
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break
    return candidates


def prepare_export(opts: dict) -> tuple[list, Path]:
    """Queue every image matching explicit `names`, `which` ("approved" | "rated" | "all" | "selected"), or selection."""
    which = opts.get("which", "approved")
    target_names = None
    if "names" in opts:
        target_names = {str(x) for x in (opts.get("names") or [])}
    elif which == "selected":
        target_names = {str(x) for x in (opts.get("selected") or [])}

    items = []
    recipe = export_workflow.clean_recipe(opts)
    color_pipeline.required_icc_bytes(recipe["outputSpace"])
    destination = export_workflow.resolve_destination(
        FOLDER, recipe["destination"])
    default_params, default_grade = effective_new_photo_defaults()
    for n, e, export_base_name in export_candidates():
        if target_names is not None:
            if n not in target_names:
                continue
        elif which == "approved" and e["status"] != "approved":
            continue
        elif which == "rated" and (e["rating"] or 0) < 1:
            continue
        elif which == "all" and e["status"] == "skipped":
            continue
        item_destination = destination
        if recipe["destinationMode"] != "fixed":
            source, source_id, _, _ = resolve_name(n)
            root = source_root(source_id) if source_id is not None else FOLDER
            item_destination = export_workflow.photo_destination(
                recipe, library_root=FOLDER, source=source, source_root=root)
        items.append((n, {
            "params": e["params"] or default_params,
            "grade": e["grade"] or default_grade,
            "crop": e["crop"],
            "masks": e["masks"],
            "heals": e["heals"],
            "optics": e["optics"],
            "format": recipe["format"],
            "quality": recipe["quality"],
            "longEdge": recipe["longEdge"],
            "engine": opts.get("engine", "rs"),
            "outputSpace": color_pipeline.normalise_output_space(
                recipe["outputSpace"]),
            "destination": str(item_destination),
            "destinationMode": recipe["destinationMode"],
            "preserveCaptureTime": recipe["preserveCaptureTime"],
            "captureTimePolicy": recipe["captureTimePolicy"],
            "filenameTemplate": recipe["filenameTemplate"],
            "collision": recipe["collision"],
            "rating": e["rating"],
            "exportBaseName": export_base_name,
            "sequence": len(items) + 1,
            "provenance": renderer_provenance(),
            "metadata": recipe.get("metadata", "all-except-location"),
            "sidecar": recipe.get("sidecar", True),
            "watermark": recipe.get("watermark"),
            "sourceName": n,
        }))
    if target_names is None and opts.get("pairView") in ("raw", "jpeg"):
        groups = {}
        for name, _job in items:
            path, source_id, _, copy_ident = resolve_name(name)
            if copy_ident or (not is_raw(name) and path.suffix.lower() not in (".jpg", ".jpeg")):
                continue
            key = (source_id, str(path.parent), path.stem.casefold())
            groups.setdefault(key, []).append((name, is_raw(name)))
        hidden = set()
        for group in groups.values():
            if len(group) == 2 and sum(raw for _, raw in group) == 1:
                hidden.update(name for name, raw in group
                              if raw != (opts["pairView"] == "raw"))
        items = [item for item in items if item[0] not in hidden]
        for index, (_, job) in enumerate(items):
            job["sequence"] = index + 1
    if opts.get("names"):
        order = {str(name): i for i, name in enumerate(opts["names"])}
        items.sort(key=lambda item: order.get(item[0], 999999))
        for idx, item in enumerate(items):
            item[1]["sequence"] = idx + 1
    return items, destination


def export_requested_path(name: str, job: dict, metadata: dict) -> Path:
    """One filename contract for preview and export, including virtual copies."""
    cp = fp.clean_params(job["params"])
    stock = cp["stock"].replace("kodak_", "").replace("fujifilm_", "") \
        if cp["profile_enabled"] else "neutral"
    extension = {"png": "png", "tif": "tif", "tiff": "tif",
                 "heif": "heic", "heic": "heic"}.get(
                     job.get("format", "jpeg"), "jpg")
    context = {
        "filename": Path(str(job.get("exportBaseName") or
                        Path(library_workflow.source_name(name)).with_suffix(""))).name,
        "stock": stock, "rating": job.get("rating", 0),
        "date": str(metadata.get("DateTimeOriginal", "")).split(" ", 1)[0],
        "sequence": job.get("sequence", 1),
    }
    return Path(job["destination"]) / export_workflow.render_filename(
        job.get("filenameTemplate", "{filename}_{stock}"), context, extension)


def export_source_dimensions(name: str) -> tuple[int, int]:
    """Read the oriented source geometry without demosaicing or rendering."""
    source = src_path(name)
    if is_raw(name):
        import rawpy
        with rawpy.RawPy() as raw:
            raw.open_file(str(source))
            size = raw.sizes
            if size.pixel_aspect != 1.0:
                raise ValueError("Decoder adjusts non-square source pixels")
            width, height = size.iwidth, size.iheight
            if size.flip in (5, 6):
                width, height = height, width
    else:
        from PIL import Image
        with Image.open(source) as image:
            width, height = image.size
            if image.getexif().get(274, 1) in (5, 6, 7, 8):
                width, height = height, width
    if not width or not height:
        raise ValueError("Source dimensions are unavailable")
    return int(width), int(height)


def preview_export(opts: dict) -> dict:
    """Inspect one delivery example; never reserve a path or create a job."""
    items, destination = prepare_export(opts)
    result = {
        "total": len(items), "destination": str(destination),
        "collision": export_workflow.clean_recipe(opts)["collision"],
        "sample": None,
        "samples": [],
        "destinationMode": opts.get("destinationMode", "fixed"),
        "note": "Example for the first photo. Names may change if destination files change before export.",
    }
    if not items:
        return result
    name, job = items[0]
    requested = export_requested_path(name, job, exif_for(name))
    with EXPORT_PATH_LOCK:
        resolved = export_workflow.collision_path(
            requested, job["collision"], EXPORT_RESERVED_PATHS,
            (".lighttable.json",) if job.get("sidecar", True) else ())
    sample = {
        "name": name, "filename": (resolved or requested).name,
        "path": str(resolved or requested), "requestedFilename": requested.name,
        "skipped": resolved is None, "width": None, "height": None,
        "dimensionsExact": False,
    }
    try:
        width, height = export_source_dimensions(name)
        width, height = export_workflow.output_dimensions(
            width, height, rotate=fp.clean_params(job["params"])["rotate"],
            crop=job.get("crop"), long_edge=job.get("longEdge"))
        sample.update(width=width, height=height, dimensionsExact=True)
    except Exception:  # Header support can differ from the full decoder.
        sample["dimensionsNote"] = "Dimensions will be available after rendering."
    result["sample"] = sample
    result["samples"].append(sample)
    # Include examples from distinct destinations, so multi-source behavior is
    # reviewable before an export creates any directories.
    shown = {job["destination"]}
    for other_name, other_job in items[1:]:
        if other_job["destination"] in shown:
            continue
        other_path = export_requested_path(other_name, other_job, exif_for(other_name))
        result["samples"].append({"name": other_name, "path": str(other_path),
                                  "filename": other_path.name})
        shown.add(other_job["destination"])
        if len(shown) == 3:
            break
    result["destinationCount"] = len({job["destination"] for _, job in items})
    return result


def start_export(opts: dict) -> dict:
    """Queue the same authoritative selection used by the export preview."""
    items, destination = prepare_export(opts)
    SESSION_EXPORT_DESTINATIONS.update(Path(job["destination"]) for _, job in items)
    batch = ExportBatch(items, destination)
    with EXPORT_LOCK:
        if EXPORT["running"]:
            return {"error": "export already running", "jobId": EXPORT.get("jobId")}
        record = JOBS.create("export", total=len(items),
                             state="running" if items else "done",
                             result={"destination": str(destination)}, cancel=batch.cancel)
        batch.status["jobId"] = record["id"]
        EXPORT.clear()
        EXPORT.update(batch.status)
    batch.dispatch()
    batch.dispatch()
    return {"queued": len(items), "destination": str(destination), "jobId": record["id"]}


def _merge_source_path(name: str, st: dict) -> Path:
    entry = entry_for(st, name)
    default_params, _ = effective_new_photo_defaults()
    return neutral_tiff_for(name, entry.get("params") or default_params)


def _load_merge_source(source: Path) -> np.ndarray:
    return color_pipeline.resize_float(
        color_pipeline.load_float_rgb(source), MERGE_MAX_EDGE)


def _merge_source(name: str, st: dict) -> np.ndarray:
    """Compatibility helper for tests and direct callers."""
    return _load_merge_source(_merge_source_path(name, st))


def _run_merge(mode: str, names: list[str], output: Path) -> None:
    staged: Path | None = None
    started = time.perf_counter()
    current_phase = ""
    phase_started = started
    timings: dict[str, float] = {}

    def report_progress(phase: str, done: int, total: int) -> None:
        nonlocal current_phase, phase_started
        now = time.perf_counter()
        if phase != current_phase:
            if current_phase:
                timings[current_phase] = round(
                    timings.get(current_phase, 0.0) + now - phase_started, 3)
            current_phase = phase
            phase_started = now
        update = {
            "phase": phase,
            "phaseProgress": int(done),
            "phaseTotal": int(total),
            "elapsedSeconds": round(now - started, 1),
            "timings": dict(timings),
        }
        if phase == "preparing":
            update["progress"] = int(done)
        with MERGE_LOCK:
            MERGE.update(update)
            sync_job_status(dict(MERGE), progress_key="phaseProgress")

    def finish_timing() -> tuple[float, dict[str, float]]:
        now = time.perf_counter()
        if current_phase:
            timings[current_phase] = round(
                timings.get(current_phase, 0.0) + now - phase_started, 3)
        return round(now - started, 1), dict(timings)

    try:
        import merge_workflow
        st = load_state()
        sources = []
        for index, name in enumerate(names, 1):
            sources.append(_merge_source_path(name, st))
            report_progress("preparing", index, len(names))
        loaders = [lambda path=path: _load_merge_source(path)
                   for path in sources]
        if mode == "hdr":
            result = merge_workflow.hdr_merge(loaders, progress=report_progress)
        elif mode == "panorama":
            result = merge_workflow.panorama_merge(
                loaders, progress=report_progress)
        else:
            def report_alignment(inliers: int) -> None:
                with MERGE_LOCK:
                    MERGE["alignmentInliers"] = int(inliers)

            result = merge_workflow.focus_merge(
                loaders, alignment=report_alignment,
                progress=report_progress)
        report_progress("saving", 0, 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        staged = durable_io.temporary_path(output, "merge")
        color_pipeline.save_export_image(
            result, staged, fmt="tif", quality=100, output_space="prophoto")
        durable_io.publish_file_no_replace(staged, output)
        staged.unlink(missing_ok=True)
        staged = None
        durable_io.atomic_create_json(
            output.with_suffix(output.suffix + ".lighttable.json"), {
            "kind": f"{mode}-merge", "sources": names,
            "outputSpace": "prophoto", "bitDepth": 16,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2)
        invalidate_library_cache()
        report_progress("saving", 1, 1)
        elapsed, final_timings = finish_timing()
        with MERGE_LOCK:
            MERGE.update(running=False, phase="complete",
                         elapsedSeconds=elapsed, timings=final_timings,
                         output=output.relative_to(FOLDER).as_posix())
            sync_job_status(dict(MERGE), progress_key="phaseProgress")
    except Exception as error:  # noqa: BLE001 - surfaced in merge status
        elapsed, final_timings = finish_timing()
        with MERGE_LOCK:
            MERGE.update(running=False, phase="error",
                         elapsedSeconds=elapsed, timings=final_timings,
                         error=str(error))
            sync_job_status(dict(MERGE), progress_key="phaseProgress")
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        cat = catalog_handle()
        if cat is not None:
            cat.close()


def start_merge(body: dict) -> dict:
    import merge_workflow

    mode = str(body.get("mode", ""))
    if mode not in ("hdr", "panorama", "focus"):
        raise ValueError("unknown merge mode")
    limit = {"hdr": 9, "panorama": 20, "focus": 60}[mode]
    names = list(dict.fromkeys(
        library_workflow.source_name(str(name)) for name in body.get("names", [])))
    if not 2 <= len(names) <= limit:
        raise ValueError(f"{mode} merge needs 2 to {limit} distinct originals")
    for name in names:
        src_path(name)
    with MERGE_LOCK:
        if MERGE["running"]:
            return {"error": "a merge is already running"}
        folder = FOLDER / "LightTable Merges"
        default_name = {"hdr": "HDR", "panorama": "Panorama",
                        "focus": "Focus"}[mode] + \
            time.strftime("-%Y%m%d-%H%M%S")
        output = merge_workflow.collision_path(
            folder / merge_workflow.safe_output_name(
                body.get("name") or default_name, mode))
        MERGE.update(running=True, mode=mode, progress=0, total=len(names),
                     phase="preparing", phaseProgress=0,
                     phaseTotal=len(names), alignmentInliers=0,
                     elapsedSeconds=0.0, timings={}, output="", error="")
        MERGE["jobId"] = JOBS.create(
            f"merge.{mode}", total=len(names), state="running")["id"]
    MERGE_POOL.submit(_run_merge, mode, names, output)
    return {"queued": len(names), "mode": mode}


class PreviewPregenQueue:
    """Background queue for pre-generating high-resolution 1:1 develop/loupe previews."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue: list[str] = []
        self.active: bool = False
        self.cancel_requested: bool = False
        self.total: int = 0
        self.completed: int = 0
        self.failed: int = 0
        self.current: str = ""
        self.thread: threading.Thread | None = None
        self.job_id: str | None = None

    def start(self, names: list[str], width: int = 3840) -> int:
        with self.lock:
            self.cancel_requested = False
            self.queue = [str(n) for n in names]
            self.total = len(self.queue)
            self.completed = 0
            self.failed = 0
            self.current = ""
            self.job_id = JOBS.create(
                "cache.pregenerate", total=self.total,
                state="running" if self.total else "done",
                cancel=self.cancel)["id"]
            if not self.active and self.queue:
                self.active = True
                self.thread = threading.Thread(
                    target=self._worker, args=(width,), daemon=True,
                    name="lighttable-pregen-previews",
                )
                self.thread.start()
            return self.total

    def cancel(self) -> None:
        with self.lock:
            self.cancel_requested = True
            self.queue.clear()
            self.active = False
            self.current = ""

    def status(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "total": self.total,
                "completed": self.completed,
                "failed": self.failed,
                "current": self.current,
                "jobId": self.job_id,
            }

    def _worker(self, width: int) -> None:
        while True:
            name = None
            with self.lock:
                if self.cancel_requested or not self.queue:
                    self.active = False
                    self.current = ""
                    status = self.status_unlocked()
                    sync_job_status(status, progress_key="completed")
                    return
                name = self.queue.pop(0)
                self.current = name

            try:
                entry = catalog_entry_for(name)
                params = entry.get("params")
                _run_refinement(build_neutral_preview,
                                (name, width, 0, params), "", None)
                with self.lock:
                    self.completed += 1
                    sync_job_status(self.status_unlocked(),
                                    progress_key="completed")
            except Exception:
                with self.lock:
                    self.failed += 1

    def status_unlocked(self) -> dict:
        return {
            "active": self.active, "total": self.total,
            "completed": self.completed, "failed": self.failed,
            "current": self.current, "cancel_requested": self.cancel_requested,
            "jobId": self.job_id,
            "errors": ([f"{self.failed} previews failed"] if self.failed else []),
        }


class BatchSemanticMaskQueue:
    """Background queue for batch AI subject/sky/object semantic masking."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue: list[tuple[str, list[str]]] = []
        self.active: bool = False
        self.cancel_requested: bool = False
        self.total: int = 0
        self.completed: int = 0
        self.failed: int = 0
        self.current: str = ""
        self.thread: threading.Thread | None = None
        self.job_id: str | None = None

    def start(self, names: list[str], categories: list[str]) -> int:
        cats = [str(c) for c in categories if c]
        if not cats:
            cats = ["subject"]
        with self.lock:
            self.cancel_requested = False
            self.queue = [(str(n), cats) for n in names]
            self.total = len(self.queue)
            self.completed = 0
            self.failed = 0
            self.current = ""
            self.job_id = JOBS.create(
                "masks.semantic", total=self.total,
                state="running" if self.total else "done",
                cancel=self.cancel)["id"]
            if not self.active and self.queue:
                self.active = True
                self.thread = threading.Thread(
                    target=self._worker, daemon=True,
                    name="lighttable-batch-semantic-masks",
                )
                self.thread.start()
            return self.total

    def cancel(self) -> None:
        with self.lock:
            self.cancel_requested = True
            self.queue.clear()
            self.active = False
            self.current = ""

    def status(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "total": self.total,
                "completed": self.completed,
                "failed": self.failed,
                "current": self.current,
                "jobId": self.job_id,
            }

    def _worker(self) -> None:
        while True:
            item = None
            with self.lock:
                if self.cancel_requested or not self.queue:
                    self.active = False
                    self.current = ""
                    sync_job_status(self.status_unlocked(),
                                    progress_key="completed")
                    return
                item = self.queue.pop(0)
                self.current = item[0]

            name, categories = item
            try:
                entry = catalog_entry_for(name)
                existing_masks = list(entry.get("masks") or [])
                updated = False
                for cat in categories:
                    try:
                        payload = semantic_mask_payload(name, cat)
                        mask_id = f"ai-{cat}-{int(time.time() * 1000)}"
                        mask_entry = {
                            "id": mask_id,
                            "name": cat.replace("_", " ").title(),
                            "type": cat,
                            "bitmap": payload["bitmap"],
                            "components": [{
                                "id": f"{mask_id}-c1",
                                "type": cat,
                                "combine": "add",
                                "bitmap": payload["bitmap"],
                            }],
                            "enabled": True,
                            "opacity": 1.0,
                        }
                        existing_masks.append(mask_entry)
                        updated = True
                    except Exception:
                        pass
                if updated:
                    save_image_state(name, {"masks": edits.clean_masks(existing_masks)})
                with self.lock:
                    self.completed += 1
                    sync_job_status(self.status_unlocked(),
                                    progress_key="completed")
            except Exception:
                with self.lock:
                    self.failed += 1

    def status_unlocked(self) -> dict:
        return {
            "active": self.active, "total": self.total,
            "completed": self.completed, "failed": self.failed,
            "current": self.current, "cancel_requested": self.cancel_requested,
            "jobId": self.job_id,
            "errors": ([f"{self.failed} masks failed"] if self.failed else []),
        }


PREGEN_QUEUE = PreviewPregenQueue()
BATCH_MASK_QUEUE = BatchSemanticMaskQueue()


# ---------------------------------------------------------------- http -----


def health_payload(*, include_token: bool = False) -> dict:
    cat = catalog_handle()
    try:
        import enhance_workflow
        models = enhance_workflow.capabilities()
    except Exception as error:  # optional feature discovery must not break health
        models = {"available": False, "reason": str(error)}
    payload = {
        "ok": True,
        "version": "1.0",
        "sourceRevision": _git_revision(APP),
        "catalog": str(cat.path.resolve()) if cat is not None else None,
        "folder": str(FOLDER.resolve()) if FOLDER else "",
        "host": BOUND_HOST,
        "port": PORT,
        "pid": os.getpid(),
        "uptime": round(max(0.0, time.time() - STARTED_AT), 3),
        "rust": RUST_AVAILABLE,
        "engineWarm": bool(RUST_ENGINE.process
                           and RUST_ENGINE.process.poll() is None),
        "models": models,
        "windowConnected": EVENTS.window_connected,
        "headless": os.environ.get("LIGHTTABLE_HEADLESS") == "1",
        "safeMode": SAFE_MODE,
        "restarting": RESTART_REQUESTED,
        "library": library_health_summary(),
    }
    if include_token:
        payload["token"] = INSTANCE_TOKEN
    return payload


def library_health_summary() -> dict:
    """The cheap health facts every client can show without a dialog."""
    cat = catalog_handle()
    with HEALTH_LOCK:
        verify = HEALTH_STATE.get("verify")
        disk = HEALTH_STATE.get("disk")
    if cat is not None:
        status = "ok" if verify is None or verify.get("ok") else "attention"
    elif not CATALOG_ENABLED:
        status = "folder-mode"
    else:
        status = (CATALOG_NOTICE or {}).get("status", "unavailable")
    previous = SESSION.previous
    return {
        "status": status,
        "catalog": (CATALOG_NOTICE or {}).get("status", "ok" if cat else None),
        "previousCrash": bool(previous),
        "crashes24h": SESSION.crash_count(24 * 3600),
        "quarantined": len(PHOTO_QUARANTINE.quarantined()),
        "diskLow": bool(disk and disk.get("low")),
        "lastVerifyOk": None if verify is None else bool(verify.get("ok")),
        "launch": LAUNCH_NOTICE,
    }


def write_instance_file() -> Path:
    path = instance_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_io.atomic_write_json(path, health_payload(include_token=True),
                                  keep_backup=False)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def remove_instance_file(path: Path | None = None) -> None:
    target = path or instance_path()
    try:
        current = durable_io.load_json(target, {})
        if not isinstance(current, dict) or current.get("pid") == os.getpid():
            target.unlink(missing_ok=True)
    except OSError:
        pass


def options_payload() -> dict:
    return {
        "stocks": {"negatives": fp.NEGATIVES, "positives": fp.POSITIVES,
                   "papers": fp.PAPERS,
                   "grainBaselines": fp.STOCK_GRAIN_AREA_UM2},
        "profiles": fp.PROFILE_CATALOG,
        "outputRecipes": fp.OUTPUT_RECIPES,
        "params": {"defaults": fp.DEFAULT_PARAMS,
                   "ranges": fp.NUMERIC_RANGES,
                   "rawDevelopKeys": fp.RAW_DEVELOP_KEYS},
        "grade": {"defaults": grade.DEFAULTS, "ranges": grade.RANGES,
                  "curveKeys": grade.CURVE_KEYS,
                  "hslBands": grade.HSL_BANDS,
                  "advancedKeys": grade.ADVANCED_KEYS},
        "labels": list(LABEL_VALUES),
        "statuses": list(catalog_module.STATUS_VALUES),
        "maskKinds": ["brush", "linear", "radial", "subject", "sky",
                      "object", "person", "face-skin", "eyes", "eyebrows",
                      "lips", "teeth", "hair"],
        "media": {"raw": sorted(RAW_EXTS), "processed": sorted(PROCESSED_EXTS),
                  "video": sorted(VIDEO_EXTS)},
        "provenance": renderer_provenance(),
        "rust": RUST_AVAILABLE,
    }


def resolve_filesystem_path(value: str) -> dict:
    path = Path(value).expanduser().resolve()
    cat = require_catalog()
    candidates = sorted(
        (source for source in cat.sources() if source.get("active", True)),
        key=lambda source: len(str(source.get("path", ""))), reverse=True)
    for source in candidates:
        root = Path(source["path"]).expanduser().resolve()
        try:
            relpath = path.relative_to(root).as_posix()
        except ValueError:
            continue
        image_id = cat.image_id_for(int(source["id"]), relpath)
        if image_id is None:
            raise FileNotFoundError("the path is not in the catalog")
        return {"name": catalog_module.qualified_name(
                    int(source["id"]), relpath),
                "id": image_id, "sourceId": int(source["id"]),
                "path": str(path)}
    raise FileNotFoundError("the path is outside the catalog")


def clean_program_grade(value) -> dict:
    """Accept compact curve points, then use the established preset LUT path."""
    if not isinstance(value, dict):
        return grade.clean(value)
    incoming = dict(value)
    for key in grade.CURVE_KEYS:
        points = incoming.get(key)
        if not isinstance(points, list) or len(points) == 256:
            continue
        converted = preset_io._curve_lut(points)
        if converted is None:
            continue
        # The grade cleaner deliberately removes identity curves. Represent
        # that accepted no-op as None so strict comparison does not report it
        # as a rejected non-empty value.
        incoming[key] = (converted if key in grade.clean({key: converted})
                         else None)
    return grade.clean(incoming)


def cleaned_state_request(body: dict, *, strict: bool) -> tuple[dict, list[dict]]:
    normalized = dict(body)
    if "grade" in normalized:
        raw_grade = normalized["grade"]
        if isinstance(raw_grade, dict):
            normalized["grade"] = dict(raw_grade)
            for key in grade.CURVE_KEYS:
                points = normalized["grade"].get(key)
                if not isinstance(points, list) or len(points) == 256:
                    continue
                converted = preset_io._curve_lut(points)
                if converted is not None:
                    normalized["grade"][key] = (
                        converted if key in grade.clean({key: converted})
                        else None)
    return clean_state_patch(
        normalized,
        params_cleaner=fp.clean_params,
        grade_cleaner=clean_program_grade,
        crop_cleaner=clean_crop,
        masks_cleaner=edits.clean_masks,
        heals_cleaner=edits.clean_heals,
        optics_cleaner=edits.clean_optics,
        keywords_cleaner=clean_keywords,
        versions_cleaner=clean_versions,
        label_cleaner=clean_label,
        params_keys=set(fp.DEFAULT_PARAMS) | {"grain_um2"},
        grade_keys=set(grade.DEFAULTS) | set(grade.CURVE_KEYS)
                   | {"hsl", *grade.ADVANCED_KEYS},
        status_values=set(catalog_module.STATUS_VALUES),
        strict=strict,
    )


def _program_render_state(body: dict) -> tuple[str, dict, int]:
    name = str(body["name"])
    stored = catalog_entry_for(name)
    supplied = body.get("state") if isinstance(body.get("state"), dict) else {}
    state = dict(stored)
    state.update(supplied)
    width = max(64, min(8000, int(body.get("w", 1400))))
    return name, state, width


def program_render_image(body: dict, *, priority: str = "background") -> Image.Image:
    name, state, width = _program_render_state(body)
    params = fp.clean_params(state.get("params") or {})
    if body.get("before"):
        return Image.open(io.BytesIO(orig_jpeg(
            name, width, params.get("rotate", 0)))).convert("RGB")
    result = render_preview(
        name, params, width, str(body.get("engine", "rs")),
        str(body.get("client", "cli"))[:80], None, False, priority)
    if result.get("cancelled"):
        raise RuntimeError(result.get("reason") or "render cancelled")
    base = Image.open(io.BytesIO(_preview_source_bytes(
        result, name, width, params.get("rotate", 0)))).convert("RGB")
    image = np.asarray(base, dtype=np.float32) / 255.0
    image = edits.apply_base(
        image, state.get("optics"), state.get("heals"),
        edits.lens_profile_for(exif_for(name)))
    cleaned_grade = grade.clean(state.get("grade") or {})
    if not grade.is_identity(cleaned_grade):
        image = grade.apply(image, cleaned_grade)
    image = edits.apply_masks(image, state.get("masks"))
    crop = clean_crop(state.get("crop"))
    if crop:
        height, image_width = image.shape[:2]
        x0 = round(crop["x"] * image_width)
        y0 = round(crop["y"] * height)
        x1 = min(image_width, x0 + max(1, round(crop["w"] * image_width)))
        y1 = min(height, y0 + max(1, round(crop["h"] * height)))
        image = image[y0:y1, x0:x1]
    return Image.fromarray(
        (np.clip(image, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")


def encode_program_image(image: Image.Image, fmt: str = "png") -> tuple[bytes, str]:
    requested = str(fmt).lower()
    output = io.BytesIO()
    if requested in {"jpeg", "jpg"}:
        image.save(output, "JPEG", quality=92)
        return output.getvalue(), "image/jpeg"
    if requested != "png":
        raise ValueError("format must be png or jpeg")
    image.save(output, "PNG")
    return output.getvalue(), "image/png"


def _image_statistics(image: Image.Image) -> dict:
    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    flat = array.reshape(-1, 3)
    luminance = array @ grade.LUMA
    histogram, _ = np.histogram(luminance, bins=64, range=(0.0, 1.0))
    return {
        "width": image.width, "height": image.height,
        "meanRGB": np.mean(flat, axis=0).round(6).tolist(),
        "percentileRGB": {
            str(percentile): np.percentile(flat, percentile, axis=0).round(6).tolist()
            for percentile in (1, 5, 50, 95, 99)
        },
        "luminanceHistogram": histogram.astype(int).tolist(),
        "clippedShadows": round(float(np.mean(luminance <= 1 / 255)), 8),
        "clippedHighlights": round(float(np.mean(luminance >= 254 / 255)), 8),
    }


def _rgb_to_lab(array: np.ndarray) -> np.ndarray:
    rgb = np.clip(array, 0, 1)
    linear = np.where(rgb <= 0.04045, rgb / 12.92,
                      ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.asarray([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float32).T
    xyz /= np.asarray([0.95047, 1.0, 1.08883], dtype=np.float32)
    delta = 6 / 29
    f = np.where(xyz > delta ** 3, np.cbrt(xyz),
                 xyz / (3 * delta ** 2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])), axis=-1)


def analyze_program_image(body: dict) -> dict:
    image = program_render_image(body)
    result = _image_statistics(image)
    regions = []
    for index, raw in enumerate(body.get("regions") or []):
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise ValueError(f"regions[{index}] must be x,y,w,h")
        x, y, w, h = (float(value) for value in raw)
        x0 = max(0, min(image.width - 1, round(x * image.width)))
        y0 = max(0, min(image.height - 1, round(y * image.height)))
        x1 = max(x0 + 1, min(image.width, round((x + w) * image.width)))
        y1 = max(y0 + 1, min(image.height, round((y + h) * image.height)))
        regions.append({"rect": [x, y, w, h],
                        **_image_statistics(image.crop((x0, y0, x1, y1)))})
    if regions:
        result["regions"] = regions
    reference = body.get("reference")
    if reference:
        reference_body = dict(body, name=str(reference), reference=None,
                              regions=None)
        reference_image = program_render_image(reference_body)
        size = (min(image.width, reference_image.width),
                min(image.height, reference_image.height))
        first = np.asarray(image.resize(size), dtype=np.float32) / 255.0
        second = np.asarray(reference_image.resize(size), dtype=np.float32) / 255.0
        result["reference"] = {
            **_image_statistics(reference_image),
            "meanPixelError": round(float(np.mean(np.abs(first - second))), 8),
            "deltaE76": round(float(np.mean(np.linalg.norm(
                _rgb_to_lab(first) - _rgb_to_lab(second), axis=2))), 6),
        }
    return result


def compare_program_image(body: dict) -> Image.Image:
    after = program_render_image(dict(body, before=False))
    if body.get("against"):
        other = program_render_image(dict(body, name=str(body["against"]),
                                          before=False))
    else:
        other = program_render_image(dict(body, before=True))
    height = min(after.height, other.height)
    after = after.resize((round(after.width * height / after.height), height))
    other = other.resize((round(other.width * height / other.height), height))
    if "wipe" in body:
        width = min(after.width, other.width)
        split = max(0.0, min(1.0, float(body.get("wipe", 0.5))))
        output = after.crop((0, 0, width, height))
        cut = round(width * split)
        output.paste(other.crop((0, 0, cut, height)), (0, 0))
        return output
    output = Image.new("RGB", (other.width + after.width, height))
    output.paste(other, (0, 0))
    output.paste(after, (other.width, 0))
    return output


READ_ONLY_POST_PATHS = {
    "/api/render", "/api/render/file", "/api/render/compare", "/api/analyze",
    "/api/refine", "/api/mask/semantic", "/api/perf/export-one",
    "/api/soft-proof", "/api/catalog/query", "/api/ingest/scan",
    "/api/photos/reveal", "/api/geometry/auto", "/api/presets/export",
    "/api/export/preview",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def finish(self) -> None:
        """Close this request thread's SQLite connection with its streams."""
        try:
            super().finish()
        finally:
            cat = catalog_handle()
            if cat is not None:
                cat.close()

    def _send(self, code: int, body: bytes, ctype: str,
              cache_control: str = "no-store", *,
              headers: dict[str, str] | None = None) -> None:
        self._response_status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, code: int, path: Path, ctype: str,
                   cache_control: str = "no-store") -> None:
        self._response_status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _send_video(self, name: str) -> None:
        """Stream a catalogued video, honouring Range requests.

        A plain 200 with the whole file makes a browser download before it can
        play and disables seeking entirely, so range support is what turns
        "the file is in the catalog" into "the clip plays".
        """
        path = src_path(name)
        if path.suffix.lower() not in VIDEO_EXTS:
            raise ValueError("not a video")
        size = path.stat().st_size
        content_type = {
            ".mov": "video/quicktime", ".mp4": "video/mp4",
            ".m4v": "video/mp4", ".avi": "video/x-msvideo",
        }.get(path.suffix.lower(), "application/octet-stream")
        header = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        match = re.match(r"bytes=(\d*)-(\d*)", header or "")
        if match and (match.group(1) or match.group(2)):
            partial = True
            if match.group(1):
                start = min(int(match.group(1)), size - 1)
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:
                start = max(0, size - int(match.group(2)))
            if end < start:
                start, end = 0, size - 1
                partial = False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _body(self) -> dict:
        content_type = str(self.headers.get("Content-Type", ""))
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise APIError(415, "requests must use application/json",
                           "unsupported-media-type")
        n = int(self.headers.get("Content-Length", 0))
        value = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _enforce_security(self, *, mutating: bool) -> None:
        # Direct unit tests instantiate Handler without a socket/server.  Real
        # requests always have one and take the complete path below.
        if not hasattr(self, "server"):
            return
        port = int(self.server.server_address[1])
        host = str(self.headers.get("Host", ""))
        accepted_hosts = {f"{BOUND_HOST}:{port}", f"localhost:{port}"}
        if host not in accepted_hosts:
            raise APIError(403, "request Host is not this LightTable instance",
                           "invalid-host")
        if not mutating:
            return
        origin = str(self.headers.get("Origin", ""))
        if origin and origin not in {f"http://{value}" for value in accepted_hosts}:
            raise APIError(403, "cross-origin changes are not allowed",
                           "invalid-origin")
        supplied = str(self.headers.get("X-LightTable-Token", ""))
        if not supplied:
            cookies = str(self.headers.get("Cookie", ""))
            for part in cookies.split(";"):
                key, _, value = part.strip().partition("=")
                if key == "lighttable_token":
                    supplied = value
                    break
        if not supplied or not secrets.compare_digest(supplied, INSTANCE_TOKEN):
            raise APIError(401, "this change needs the instance token",
                           "unauthorized")

    def _handle_exception(self, error: Exception) -> None:
        if isinstance(error, APIError):
            status, code = error.status, error.code
            field, details = error.field, error.details
        elif isinstance(error, ValidationError):
            status, code = 400, "validation"
            field, details = error.field, error.issues
        elif isinstance(error, FileNotFoundError):
            status, code, field, details = 404, "not-found", None, None
        elif isinstance(error, PermissionError):
            status, code, field, details = 403, "forbidden", None, None
        elif isinstance(error, (KeyError, ValueError, TypeError,
                                json.JSONDecodeError)):
            status, code, field, details = 400, "bad-request", None, None
        elif isinstance(error, RuntimeError) and any(
                word in str(error).casefold()
                for word in ("already running", "busy", "not cancellable")):
            status, code, field, details = 409, "busy", None, None
        elif isinstance(error, RuntimeError) and any(
                word in str(error).casefold()
                for word in ("not ready", "unavailable")):
            status, code, field, details = 503, "not-ready", None, None
        else:
            status, code, field, details = 500, "internal", None, None
        payload = {"error": str(error), "code": code}
        if field:
            payload["field"] = field
        if details:
            payload["details"] = details
        self._json(payload, status)

    def _log_request(self, started: float) -> None:
        path = os.environ.get("LIGHTTABLE_REQUEST_LOG")
        if not path:
            return
        record = {
            "method": self.command,
            "path": urlparse(self.path).path,
            "status": int(getattr(self, "_response_status", 500)),
            "ms": round((time.perf_counter() - started) * 1000, 3),
            "client": str(self.headers.get("X-LightTable-Client", ""))[:80],
            "origin": str(self.headers.get("Origin", ""))[:200],
        }
        try:
            with REQUEST_LOG_LOCK:
                with Path(path).expanduser().open("a", encoding="utf-8") as log:
                    log.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _send_events(self, client: str) -> None:
        try:
            subscriber = EVENTS.subscribe(client)
        except RuntimeError as error:
            raise APIError(503, str(error), "not-ready") from error
        self._response_status = 200
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(encode_sse({
                "id": 0, "type": "ready", "time": time.time(),
                "client": client,
            }))
            self.wfile.flush()
            while True:
                record = EVENTS.get(subscriber, timeout=15.0)
                if record is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(encode_sse(record))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            EVENTS.unsubscribe(subscriber)

    def do_GET(self):  # noqa: N802
        started = time.perf_counter()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            self._enforce_security(mutating=False)
            if u.path == "/":
                self._send(200, (APP / "web" / "index.html").read_bytes(),
                           "text/html; charset=utf-8", headers={
                               "Set-Cookie": "lighttable_token="
                               f"{INSTANCE_TOKEN}; Path=/; HttpOnly; SameSite=Strict",
                           })
            elif u.path == "/api/health":
                self._json(health_payload())
            elif u.path == "/api/events":
                self._send_events(q.get("client", ""))
            elif u.path == "/api/options":
                self._json(options_payload())
            elif u.path == "/api/resolve":
                self._json(resolve_filesystem_path(q["path"]))
            elif u.path == "/api/jobs":
                self._json({"jobs": JOBS.list()})
            elif u.path.startswith("/api/jobs/"):
                ident = u.path.removeprefix("/api/jobs/")
                record = JOBS.get(ident)
                if record is None:
                    raise FileNotFoundError("job not found")
                self._json(record)
            elif u.path == "/api/ui/state":
                with UI_STATE_LOCK:
                    report = dict(UI_STATE)
                if report.get("reportedAt"):
                    report["age"] = max(0.0, time.time() - report["reportedAt"])
                self._json(report)
            elif u.path.startswith("/web/"):
                rel = u.path[len("/web/"):]
                f = (APP / "web" / rel).resolve()
                if (APP / "web").resolve() not in f.parents or not f.is_file():
                    self._json({"error": "not found"}, 404)
                else:
                    ctype = {
                        ".js": "text/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8",
                        ".html": "text/html; charset=utf-8",
                    }.get(f.suffix, "application/octet-stream")
                    self._send(200, f.read_bytes(), ctype)
            elif u.path == "/api/images":
                image_rows, snapshot = library_payload(
                    int(q.get("limit", LIBRARY_PAGE_LIMIT)))
                cat = catalog_handle()
                default_params, default_grade = effective_new_photo_defaults()
                self._json({
                    "folder": str(FOLDER),
                    "folders": snapshot["folders"],
                    "catalog": {
                        "path": str(cat.path.resolve()) if cat else None,
                        "enabled": cat is not None,
                        "primarySource": PRIMARY_SOURCE_ID,
                        "sources": cat.sources() if cat else [],
                        "collections": cat.collections() if cat else [],
                    },
                    "labels": list(LABEL_VALUES),
                    "stocks": {"negatives": fp.NEGATIVES,
                               "positives": fp.POSITIVES,
                               "papers": fp.PAPERS,
                               "grainBaselines": fp.STOCK_GRAIN_AREA_UM2},
                    "profiles": fp.PROFILE_CATALOG,
                    "outputRecipes": fp.OUTPUT_RECIPES,
                    "provenance": renderer_provenance(),
                    "defaults": default_params,
                    "rust": RUST_AVAILABLE,
                    "images": image_rows,
                    "total": snapshot.get("total", len(image_rows)),
                    "library": current_library_state(),
                    "hasExif": True,
                    "gradeDefaults": default_grade,
                    "aiIndex": AI_INDEX.status() if AI_INDEX else None,
                })
            elif u.path == "/api/thumb":
                payload = (video_thumbnail(q["name"]) if is_video(q["name"])
                           else thumb_jpeg(q["name"]))
                self._send(200, payload, "image/jpeg",
                           "public, max-age=31536000, immutable")
            elif u.path == "/api/thumb/rendered":
                rendered = edited_thumbnail(q["name"])
                if rendered is None:
                    self._send(
                        202, b"", "application/octet-stream", "no-store",
                        headers={"Retry-After": "1"})
                else:
                    path, key = rendered
                    self._send(
                        200, path.read_bytes(), "image/jpeg", "no-store",
                        headers={"X-LightTable-Thumbnail-Key": key})
            elif u.path == "/api/video":
                self._send_video(q["name"])
            elif u.path == "/api/orig":
                self._send(200, orig_jpeg(q["name"], int(q.get("w", 1100)),
                                          float(q.get("rot", 0))),
                           "image/jpeg",
                           "public, max-age=31536000, immutable")
            elif u.path == "/api/neutral":
                raw_key = q.get("rk")
                if raw_key and (len(raw_key) != 12 or
                                any(c not in "0123456789abcdef" for c in raw_key)):
                    raise ValueError("bad RAW development key")
                path = neutral_preview_path(
                    q["name"], int(q.get("w", 1100)), float(q.get("rot", 0)),
                    raw_key=raw_key)
                if not path.exists():
                    raise FileNotFoundError("neutral preview is still rendering")
                self._send(200, path.read_bytes(), "image/jpeg",
                           "public, max-age=31536000, immutable")
            elif u.path == "/api/render/image":
                key = q.get("key", "")
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise ValueError("bad render key")
                image = CACHE / "render" / f"{key}.jpg"
                if not image.exists():
                    self._json({"error": "render not found"}, 404)
                else:
                    self._send(
                        200, image.read_bytes(), "image/jpeg",
                        "public, max-age=31536000, immutable")
            elif u.path == "/api/edit/image":
                key = q.get("key", "")
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise ValueError("bad edit render key")
                image = CACHE / "edit" / f"{key}.jpg"
                if not image.exists():
                    self._json({"error": "edit render not found"}, 404)
                else:
                    self._send(
                        200, image.read_bytes(), "image/jpeg",
                        "public, max-age=31536000, immutable")
            elif u.path == "/api/render/native":
                key = q.get("key", "")
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise ValueError("bad render key")
                surface = CACHE / "render" / f"{key}.rgba"
                if not surface.exists():
                    self._json({"error": "native render not found"}, 404)
                else:
                    self._send_file(
                        200, surface, "application/x-lighttable-rgba",
                        "public, max-age=31536000, immutable")
            elif u.path == "/api/render/helper":
                key = q.get("key", "")
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise ValueError("bad render key")
                image = browser_helper_path(key)
                surface = CACHE / "render" / f"{key}.rgba"
                with HELPER_LOCK:
                    ensure_jpeg_surface(
                        image, surface, NATIVE_BROWSER_HELPER_OUTPUT_WIDTH)
                    prune_cache(
                        image.parent, "*.jpg", _NATIVE_SURFACE_CACHE_MAX_BYTES // 8)
                if not image.exists():
                    self._json({"error": "render helper not found"}, 404)
                else:
                    self._send_file(
                        200, image, "image/jpeg",
                        "public, max-age=31536000, immutable")
            elif u.path == "/api/exif":
                self._json(exif_for(q["name"]))
            elif u.path == "/api/lens-profile":
                profile = edits.lens_profile_for(exif_for(q["name"]))
                self._json({"found": bool(profile), "profile": profile})
            elif u.path == "/api/raw-default":
                if not is_raw(q["name"]):
                    self._json({"settings": None, "label": "Processed image",
                                "key": ""})
                else:
                    self._json(raw_camera_default(q["name"]))
            elif u.path == "/api/calibration/target.png":
                self._send(200, calibration_target.target_png(
                    int(q.get("w", 1600)), int(q.get("h", 1000))),
                    "image/png", "public, max-age=86400")
            elif u.path == "/api/presets":
                self._json(load_presets())
            elif u.path == "/api/export-recipes":
                self._json(load_export_recipes())
            elif u.path == "/api/prefs":
                self._json(load_json_file(PREFS_FILE, {}))
            elif u.path == "/api/cache/status":
                self._json(cache_status())
            elif u.path == "/api/sidecars/status":
                self._json(sidecar_sync_status())
            elif u.path == "/api/soft-proof/profiles":
                import soft_proof
                self._json({"profiles": soft_proof.list_system_icc_profiles()})
            elif u.path == "/api/ai-index/status":
                if not AI_INDEX:
                    self._json({"error": "local index is not ready"}, 503)
                else:
                    self._json(AI_INDEX.status())
            elif u.path == "/api/ai-index/results":
                if not AI_INDEX:
                    self._json({"error": "local index is not ready"}, 503)
                else:
                    # The same name source the indexer enumerates with. In
                    # catalog mode `list_images` returns filesystem paths
                    # while the index is keyed by catalog names, so asking
                    # with the wrong one silently returns nothing.
                    self._json(AI_INDEX.results(catalog_image_names()))
            elif u.path == "/api/export/status":
                with EXPORT_LOCK:
                    sync_job_status(dict(EXPORT))
                    self._json(dict(EXPORT))
            elif u.path == "/api/merge/status":
                with MERGE_LOCK:
                    sync_job_status(dict(MERGE), progress_key="phaseProgress")
                    self._json(dict(MERGE))
            elif u.path == "/api/denoise/status":
                with DENOISE_LOCK:
                    sync_job_status(dict(DENOISE), progress_key="progress")
                    self._json(dict(DENOISE))
            elif u.path == "/api/edit-external/status":
                with EXTERNAL_EDIT_LOCK:
                    sync_job_status(dict(EXTERNAL_EDIT))
                    self._json(dict(EXTERNAL_EDIT))
            elif u.path == "/api/cache/pregenerate/status":
                self._json(PREGEN_QUEUE.status())
            elif u.path == "/api/batch/semantic-masks/status":
                self._json(BATCH_MASK_QUEUE.status())
            elif u.path == "/api/watch":
                self._json(watch_action({"action": "list"}))
            elif u.path == "/api/watch/status":
                self._json({"watches": WATCH_SERVICE.status
                            if WATCH_SERVICE else []})
            elif u.path == "/api/library":
                self._json(library_state())
            elif u.path == "/api/catalog":
                cat = catalog_handle()
                self._json({
                    "enabled": cat is not None,
                    "mirrorEnabled": catalog_mirror_enabled(),
                    "mirrorAllowed": CATALOG_MIRROR,
                    "path": str(cat.path.resolve()) if cat else None,
                    "backupPath": str(configured_backup_directory(cat).resolve())
                    if cat else None,
                    "stats": cat.stats() if cat else None,
                    "sources": cat.sources() if cat else [],
                    "collections": cat.collections() if cat else [],
                    "primarySource": PRIMARY_SOURCE_ID,
                    "scan": SCANNER.status if SCANNER else None,
                    "recovery": CATALOG_NOTICE,
                })
            elif u.path == "/api/recovery":
                self._json(recovery_status(
                    include_backups=q.get("backups", "1") != "0"))
            elif u.path == "/api/recovery/log":
                self._json({"path": os.environ.get("LIGHTTABLE_SERVER_LOG"),
                            "lines": recovery.log_tail(
                                os.environ.get("LIGHTTABLE_SERVER_LOG"),
                                int(q.get("lines", 120)))})
            elif u.path == "/api/catalog/folders":
                self._json({"folders": catalog_folder_rows(
                    int(q["sourceId"]) if q.get("sourceId") else None)})
            elif u.path == "/api/catalog/keywords":
                cat = catalog_handle()
                self._json({"keywords": cat.keyword_tree() if cat else []})
            elif u.path == "/api/catalog/scan":
                self._json(SCANNER.status if SCANNER else {"running": False})
            elif u.path == "/api/catalog/duplicates":
                cat = require_catalog()
                self._json({"groups": cat.duplicates()})
            elif u.path == "/api/state":
                state = catalog_entry_for(q["name"])
                if q.get("recovery") == "1":
                    try:
                        state["_recoverySourceKey"] = file_key(q["name"])
                    except (ValueError, OSError):
                        state["_recoverySourceKey"] = None
                self._json(state)
            elif u.path == "/api/metadata":
                cat = require_catalog()
                image_id = catalog_image_id(q["name"])
                if image_id is None:
                    raise ValueError("unknown image")
                self._json({"iptc": cat.iptc_for(image_id),
                            "exif": exif_for(q["name"])})
            elif u.path == "/api/history":
                cat = require_catalog()
                image_id = catalog_image_id(q["name"])
                if image_id is None:
                    raise ValueError("unknown image")
                self._json({"steps": cat.history_for(image_id)})
            elif u.path == "/api/history/state":
                cat = require_catalog()
                state = cat.history_state(int(q["id"]))
                if state is None:
                    raise ValueError("unknown history step")
                self._json(state)
            elif u.path == "/api/enhance/capabilities":
                import enhance_workflow
                self._json(enhance_workflow.capabilities())
            elif u.path == "/api/ingest/status":
                with INGEST_LOCK:
                    sync_job_status(dict(INGEST))
                    self._json(dict(INGEST))
            elif u.path == "/api/ingest/sources":
                self._json({"sources": INGEST_VOLUMES})
            elif u.path == "/api/import/status":
                with IMPORT_LOCK:
                    sync_job_status(dict(IMPORT_JOB))
                    self._json(dict(IMPORT_JOB))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._handle_exception(e)
        finally:
            self._log_request(started)

    def do_POST(self):  # noqa: N802
        started = time.perf_counter()
        u = urlparse(self.path)
        try:
            self._enforce_security(mutating=u.path not in READ_ONLY_POST_PATHS)
            if u.path == "/api/render/file":
                body = self._body()
                payload, content_type = encode_program_image(
                    program_render_image(body), body.get("format", "png"))
                self._send(200, payload, content_type)
            elif u.path == "/api/analyze":
                self._json(analyze_program_image(self._body()))
            elif u.path == "/api/render/compare":
                body = self._body()
                payload, content_type = encode_program_image(
                    compare_program_image(body), "png")
                self._send(200, payload, content_type)
            elif u.path == "/api/render":
                b = self._body()
                client = str(b.get("client", ""))[:80]
                generation = b.get("generation")
                if client and isinstance(generation, int):
                    with GENERATION_LOCK:
                        LATEST_GENERATION[client] = max(
                            generation, LATEST_GENERATION.get(client, generation))
                result = render_preview(
                    b["name"], b.get("params", {}), int(b.get("w", 1100)),
                    b.get("engine", "py"), client,
                    generation if isinstance(generation, int) else None,
                    bool(b.get("native", False)),
                    str(b.get("priority", "interactive")))
                if bool(b.get("native", False)):
                    result = apply_preview_edits(
                        result, b["name"], int(b.get("w", 1100)),
                        b.get("params", {}), b.get("optics"), b.get("heals"),
                        native=True)
                    if not result.get("cancelled"):
                        result = dict(result, lens_profile=edits.lens_profile_for(
                            exif_for(b["name"])))
                    self._json(result)
                else:
                    self._json(apply_preview_edits(
                        result, b["name"], int(b.get("w", 1100)),
                        b.get("params", {}), b.get("optics"), b.get("heals")))
            elif u.path == "/api/mask/semantic":
                b = self._body()
                params = fp.clean_params(b.get("params", {}))
                self._json(semantic_mask_payload(
                    b["name"], str(b.get("kind", "")), b.get("point"),
                    params["rotate"]))
            elif u.path == "/api/refine":
                b = self._body()
                name = b["name"]
                width = int(b.get("w", 1100))
                params = b.get("params", {})
                client = str(b.get("client", ""))[:80]
                generation = b.get("generation")
                generation = generation if isinstance(generation, int) else None
                if not is_raw(name):
                    self._json({"scheduled": False, "ready": True})
                elif not fp.clean_params(params)["profile_enabled"]:
                    rotate = fp.clean_params(params)["rotate"]
                    scheduled = schedule_neutral_refinement(
                        name, width, rotate, params,
                        client=client, generation=generation)
                    self._json({
                        "scheduled": scheduled,
                        "ready": neutral_preview_path(
                            name, width, rotate, params).exists(),
                    })
                else:
                    scheduled = schedule_raw_refinement(
                        name, width, params, client=client, generation=generation)
                    self._json({
                        "scheduled": scheduled,
                        "ready": preview_variant(name, width, params) == "full",
                    })
            elif u.path == "/api/perf/export-one":
                b = self._body()
                self._json(benchmark_export(b["name"], b["job"]))
            elif u.path == "/api/state":
                b = self._body()
                strict = str(self.headers.get("X-LightTable-Strict", "")) == "1"
                entry, warnings = cleaned_state_request(b, strict=strict)
                if "params" in entry:
                    entry["provenance"] = renderer_provenance()
                updates = expand_paired_metadata({b["name"]: entry})
                save_image_states(updates)
                origin = str(b.get("origin", ""))[:80]
                if origin and not origin.startswith("window"):
                    cat = catalog_handle()
                    image_id = catalog_image_id(b["name"])
                    if cat is not None and image_id is not None:
                        cat.add_history(
                            image_id, str(b.get("historyLabel") or "External edit"),
                            catalog_entry_for(b["name"]), origin=origin)
                EVENTS.publish("state", {
                    "names": list(updates), "fields": sorted(entry),
                    "origin": origin or "window",
                    "client": str(self.headers.get(
                        "X-LightTable-Client", ""))[:80],
                })
                response = {"ok": True, "names": list(updates)}
                if warnings:
                    response["warnings"] = warnings
                self._json(response)
            elif u.path == "/api/state/bulk":
                b = self._body()
                strict = str(self.headers.get("X-LightTable-Strict", "")) == "1"
                raw_entry = b.get("entry") if isinstance(b.get("entry"), dict) else {}
                cleaned, warnings = cleaned_state_request(
                    {"name": "bulk", **raw_entry}, strict=strict)
                if "params" in cleaned:
                    cleaned["provenance"] = renderer_provenance()
                updates = {}
                for name in b.get("names", []):
                    updates[str(name)] = dict(cleaned)
                updates = expand_paired_metadata(updates)
                if updates:
                    save_image_states(updates)
                origin = str(b.get("origin", ""))[:80]
                if origin and not origin.startswith("window"):
                    cat = catalog_handle()
                    if cat is not None:
                        for name in updates:
                            image_id = catalog_image_id(name)
                            if image_id is not None:
                                cat.add_history(
                                    image_id,
                                    str(b.get("historyLabel") or "External edit"),
                                    catalog_entry_for(name), origin=origin)
                EVENTS.publish("state", {
                    "names": list(updates), "fields": sorted(cleaned),
                    "origin": origin or "window",
                    "client": str(self.headers.get(
                        "X-LightTable-Client", ""))[:80],
                })
                response = {"ok": True, "count": len(updates)}
                if warnings:
                    response["warnings"] = warnings
                self._json(response)
            elif u.path == "/api/ui/state":
                body = self._body()
                report = dict(body)
                report["client"] = str(body.get("client", ""))[:80]
                report["reportedAt"] = time.time()
                with UI_STATE_LOCK:
                    UI_STATE.clear()
                    UI_STATE.update(report)
                EVENTS.publish("ui.state", report)
                self._json({"ok": True})
            elif u.path == "/api/ui/result":
                body = self._body()
                ident = str(body.get("id", ""))
                with UI_PENDING_LOCK:
                    pending = UI_PENDING.get(ident)
                    if pending is not None:
                        pending["result"] = {
                            "ok": bool(body.get("ok")),
                            "result": body.get("result"),
                            "error": body.get("error"),
                        }
                        pending["event"].set()
                self._json({"ok": pending is not None})
            elif u.path == "/api/ui/command":
                body = self._body()
                with UI_STATE_LOCK:
                    target = dict(UI_STATE)
                if not target or time.time() - target.get("reportedAt", 0) > 10:
                    raise APIError(409, "no live LightTable window is connected",
                                   "no-window")
                if target.get("allowAutomation") is False:
                    raise APIError(403, "window automation is disabled",
                                   "automation-disabled")
                ident = secrets.token_hex(12)
                pending = {"event": threading.Event(), "result": None}
                with UI_PENDING_LOCK:
                    UI_PENDING[ident] = pending
                EVENTS.publish("ui.command", {
                    "commandId": ident, "command": str(body.get("command", "")),
                    "args": body.get("args") or {},
                    "targetClient": target.get("client", ""),
                    "origin": str(body.get("origin", "cli"))[:80],
                })
                timeout = max(0.1, min(30.0, float(body.get("timeout", 10))))
                completed = pending["event"].wait(timeout)
                with UI_PENDING_LOCK:
                    UI_PENDING.pop(ident, None)
                if not completed:
                    raise APIError(504, "the window did not answer the command",
                                   "ui-timeout")
                result = pending["result"] or {"ok": False,
                                                "error": "empty UI result"}
                self._json(result, 200 if result.get("ok") else 409)
            elif u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
                ident = u.path.removeprefix("/api/jobs/").removesuffix("/cancel")
                self._json(JOBS.cancel(ident))
            elif u.path == "/api/match-exposure":
                b = self._body()
                ref_name = b.get("reference")
                targets = list(b.get("targets", []))
                if not ref_name and targets:
                    ref_name = targets[0]
                    targets = targets[1:]
                targets = [t for t in targets if t != ref_name]
                if not ref_name or not targets:
                    self._json({"ok": False, "error": "reference and target image names required"}, 400)
                    return

                ref_exif = exif_for(ref_name)
                ref_ev = parse_camera_ev(ref_exif)
                ref_entry = catalog_entry_for(ref_name)
                ref_grade = ref_entry.get("grade") or {}
                ref_params = ref_entry.get("params") or {}
                ref_film_on = bool(ref_params.get("film_profile_on", True))
                ref_adj = float(ref_params.get("exposure_ev", 0.0) if ref_film_on else ref_grade.get("exposure", 0.0))

                updates = {}
                applied_deltas = {}

                for target_name in targets:
                    target_entry = catalog_entry_for(target_name)
                    target_grade = dict(target_entry.get("grade") or {})
                    target_params = dict(target_entry.get("params") or {})
                    target_film_on = bool(target_params.get("film_profile_on", True))
                    target_adj = float(target_params.get("exposure_ev", 0.0) if target_film_on else target_grade.get("exposure", 0.0))

                    target_exif = exif_for(target_name)
                    target_ev = parse_camera_ev(target_exif)

                    if ref_ev is not None and target_ev is not None:
                        # delta = (target_ev - ref_ev) + (ref_adj - target_adj)
                        delta = (target_ev - ref_ev) + (ref_adj - target_adj)
                    else:
                        ref_lum = image_mean_luminance(ref_name)
                        target_lum = image_mean_luminance(target_name)
                        if ref_lum > 1e-4 and target_lum > 1e-4:
                            delta = float(np.log2(ref_lum / target_lum))
                        else:
                            delta = 0.0

                    delta = float(np.clip(delta, -3.0, 3.0))
                    new_adj = round(float(np.clip(target_adj + delta, -3.0, 3.0)), 3)

                    entry_update = dict(target_entry)
                    if target_film_on:
                        target_params["exposure_ev"] = new_adj
                        entry_update["params"] = fp.clean_params(target_params)
                    else:
                        target_grade["exposure"] = new_adj
                        entry_update["grade"] = grade.clean(target_grade)

                    updates[str(target_name)] = entry_update
                    applied_deltas[str(target_name)] = round(delta, 3)

                if updates:
                    save_image_states(updates)

                self._json({"ok": True, "count": len(updates), "deltas": applied_deltas})
            elif u.path == "/api/soft-proof":
                import soft_proof
                b = self._body()
                profile = b.get("profile", "srgb")
                intent = b.get("intent", "relative_colorimetric")
                simulate_paper = bool(b.get("simulate_paper", False))
                self._json({
                    "ok": True,
                    "profile": profile,
                    "intent": intent,
                    "simulate_paper": simulate_paper,
                })
            elif u.path == "/api/cache/pregenerate":
                b = self._body()
                names = list(b.get("names", []))
                size = int(b.get("size", 3840))
                queued = PREGEN_QUEUE.start(names, size)
                self._json({"ok": True, "queued": queued,
                            "jobId": PREGEN_QUEUE.job_id})
            elif u.path == "/api/cache/pregenerate/cancel":
                PREGEN_QUEUE.cancel()
                self._json({"ok": True})
            elif u.path == "/api/batch/semantic-masks":
                b = self._body()
                names = list(b.get("names", []))
                cats = list(b.get("categories", ["subject"]))
                queued = BATCH_MASK_QUEUE.start(names, cats)
                self._json({"ok": True, "queued": queued,
                            "jobId": BATCH_MASK_QUEUE.job_id})
            elif u.path == "/api/batch/semantic-masks/cancel":
                BATCH_MASK_QUEUE.cancel()
                self._json({"ok": True})
            elif u.path == "/api/presets":
                b = self._body()
                items = load_presets()
                act = b.get("action")
                if act == "save":
                    items = [i for i in items if i.get("name") != b["name"]]
                    items.append({
                        "name": b["name"],
                        "source": "lighttable",
                        "presetType": b.get("presetType", "style"),
                        "includeFilm": bool(b.get("includeFilm", True)),
                        "recommendedFilmOff": False,
                        "params": b.get("params", {}),
                        "grade": b.get("grade", {}),
                        "masks": b.get("masks", []),
                        "heals": b.get("heals", []),
                        "optics": b.get("optics", {}),
                    })
                elif act == "delete":
                    items = [i for i in items if i.get("name") != b["name"]]
                saved = save_presets(items)
                EVENTS.publish("library", {"reason": "presets"})
                self._json(saved)
            elif u.path == "/api/presets/import":
                b = self._body()
                imported, failures = preset_io.import_uploads(b.get("files", []))
                items = merge_imported_presets(load_presets(), imported)
                self._json({
                    "presets": items,
                    "imported": len(imported),
                    "failures": failures,
                })
            elif u.path == "/api/presets/export":
                b = self._body()
                selected = next((item for item in load_presets()
                                 if item["name"] == b.get("name")), None)
                if not selected:
                    self._json({"error": "preset not found"}, 404)
                else:
                    filename, content_type, content = preset_io.export_preset(
                        selected, str(b.get("format", "lighttable")))
                    self._json({"filename": filename,
                                "contentType": content_type,
                                "content": content})
            elif u.path == "/api/prefs":
                with PREFS_LOCK:
                    current = load_json_file(PREFS_FILE, {})
                    if not isinstance(current, dict):
                        current = {}
                    patch = self._body()
                    current.update(patch)
                    # An unreadable live file is about to be replaced; keep
                    # its bytes rather than letting the rewrite erase them.
                    recovery.preserve_damaged_json(PREFS_FILE)
                    durable_io.atomic_write_json(PREFS_FILE, current)
                if {"catalogMirror", "writeSidecars"} & set(patch):
                    _queue_mirror(0)
                self._json({"ok": True})
            elif u.path == "/api/recovery":
                self._json(recovery_action(self._body()))
            elif u.path == "/api/cache/purge":
                self._json(purge_generated_cache())
            elif u.path == "/api/export-recipes":
                recipes = update_export_recipes(self._body())
                EVENTS.publish("library", {"reason": "export-recipes"})
                self._json(recipes)
            elif u.path == "/api/raw-default":
                b = self._body()
                name = b["name"]
                if not is_raw(name):
                    self._json({"error": "camera defaults require a RAW photo"}, 400)
                elif b.get("action") == "delete":
                    self._json(update_raw_camera_default(name, None))
                elif b.get("action") == "save":
                    self._json(update_raw_camera_default(
                        name, b.get("settings", {})))
                else:
                    self._json({"error": "unknown camera-default action"}, 400)
            elif u.path == "/api/ai-index":
                if not AI_INDEX:
                    self._json({"error": "local index is not ready"}, 503)
                else:
                    action = self._body().get("action")
                    if action == "enable":
                        self._json(AI_INDEX.enable())
                    elif action == "disable":
                        self._json(AI_INDEX.disable())
                    elif action == "rebuild":
                        self._json(AI_INDEX.rebuild())
                    elif action == "clear":
                        self._json(AI_INDEX.clear())
                    else:
                        self._json({"error": "unknown local index action"}, 400)
            elif u.path == "/api/folders":
                b = self._body()
                action = b.get("action")
                if action == "create":
                    path = create_subfolder(b.get("parent", ""), b.get("name"))
                    EVENTS.publish("library", {"reason": "folder"})
                    self._json({"ok": True, "path": path})
                elif action == "rename":
                    path = rename_subfolder(b.get("path", ""), b.get("name"))
                    EVENTS.publish("library", {"reason": "folder"})
                    self._json({"ok": True, "path": path})
                else:
                    self._json({"error": "unknown folder action"}, 400)
            elif u.path == "/api/photos/move":
                b = self._body()
                names = [str(name) for name in b.get("names", [])]
                if not names:
                    raise ValueError("no photos selected")
                moved = move_images(names, b.get("destination", ""))
                EVENTS.publish("library", {"reason": "move"})
                self._json({"ok": True, "moved": moved})
            elif u.path == "/api/library":
                result = update_library(self._body())
                EVENTS.publish("library", {"reason": "library"})
                self._json(result)
            elif u.path == "/api/export/preview":
                self._json(preview_export(self._body()))
            elif u.path == "/api/export":
                result = start_export(self._body())
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/merge":
                result = start_merge(self._body())
                if MERGE.get("jobId"):
                    result.setdefault("jobId", MERGE["jobId"])
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/catalog/query":
                self._json(browser_catalog_query(self._body()))
            elif u.path == "/api/catalog/sources":
                result = catalog_sources_action(self._body())
                EVENTS.publish("library", {"reason": "source"})
                self._json(result)
            elif u.path == "/api/catalog/scan":
                body = self._body()
                if SCANNER is None:
                    raise ValueError("the catalog is not available")
                SCANNER.request(int(body["sourceId"])
                                if body.get("sourceId") else None)
                EVENTS.publish("library", {"reason": "scan"})
                self._json({"ok": True, "status": SCANNER.status})
            elif u.path == "/api/catalog/collections":
                result = catalog_collections_action(self._body())
                EVENTS.publish("library", {"reason": "collection"})
                self._json(result)
            elif u.path == "/api/catalog/keywords":
                body = self._body()
                cat = require_catalog()
                if body.get("action") == "rename":
                    cat.rename_keyword(int(body["id"]), str(body["name"]))
                self._json({"ok": True, "keywords": cat.keyword_tree()})
            elif u.path == "/api/catalog/backup":
                cat = require_catalog()
                directory = configured_backup_directory(cat)
                archive = cat.backup(directory)
                cat.prune_backups(directory)
                self._json({"ok": True, "archive": str(archive)})
            elif u.path == "/api/metadata":
                body = self._body()
                cat = require_catalog()
                image_id = catalog_image_id(body["name"])
                if image_id is None:
                    raise ValueError("unknown image")
                cat.save_iptc(image_id, body.get("fields") or {})
                EVENTS.publish("state", {"names": [body["name"]],
                                          "fields": ["metadata"],
                                          "origin": "metadata"})
                self._json({"ok": True, "iptc": cat.iptc_for(image_id)})
            elif u.path == "/api/metadata/bulk":
                body = self._body()
                cat = require_catalog()
                fields = body.get("fields") or {}
                count = 0
                for name in body.get("names", [])[:5000]:
                    image_id = catalog_image_id(str(name))
                    if image_id is not None:
                        cat.save_iptc(image_id, fields)
                        count += 1
                EVENTS.publish("state", {
                    "names": [str(name) for name in body.get("names", [])[:5000]],
                    "fields": ["metadata"], "origin": "metadata"})
                self._json({"ok": True, "count": count})
            elif u.path == "/api/history":
                body = self._body()
                cat = require_catalog()
                image_id = catalog_image_id(body["name"])
                if image_id is None:
                    raise ValueError("unknown image")
                seq = cat.add_history(image_id, str(body.get("label", "Edit")),
                                      body.get("state") or {},
                                      origin=str(body.get("origin", "edit")))
                self._json({"ok": True, "seq": seq})
            elif u.path == "/api/history/clear":
                body = self._body()
                cat = require_catalog()
                image_id = catalog_image_id(body["name"])
                if image_id is not None:
                    cat.clear_history(image_id)
                self._json({"ok": True})
            elif u.path == "/api/import/sidecars":
                self._json(import_sidecars(self._body()))
            elif u.path == "/api/import/catalog":
                body = self._body()
                result = start_catalog_import(body)
                if IMPORT_JOB.get("jobId") and not body.get("inspectOnly"):
                    result.setdefault("jobId", IMPORT_JOB["jobId"])
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/ingest/scan":
                self._json(scan_ingest_source(self._body()))
            elif u.path == "/api/ingest":
                result = start_ingest(self._body())
                if INGEST.get("jobId"):
                    result.setdefault("jobId", INGEST["jobId"])
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/ingest/cancel":
                with INGEST_LOCK:
                    INGEST["cancelled"] = True
                    sync_job_status(dict(INGEST))
                self._json({"ok": True})
            elif u.path == "/api/sidecars/write":
                body = self._body()
                for name in body.get("names", [])[:20000]:
                    queue_sidecar(str(name))
                written = write_pending_sidecars()
                status = sidecar_sync_status()
                self._json({"ok": not status["failed"], "written": written, **status})
            elif u.path == "/api/photos/rename":
                result = rename_photos(self._body())
                EVENTS.publish("library", {"reason": "rename"})
                self._json(result)
            elif u.path == "/api/photos/trash":
                self._json(trash_photos(self._body()))
            elif u.path == "/api/photos/reveal":
                self._json(reveal_photo(self._body()))
            elif u.path == "/api/geometry/auto":
                self._json(auto_geometry(self._body()))
            elif u.path == "/api/enhance":
                result = start_enhance(self._body())
                record = JOBS.create(
                    "enhance", state="done" if result.get("ok") else "failed",
                    result=result)
                self._json({**result, "jobId": record["id"]})
            elif u.path == "/api/denoise":
                result = start_denoise(self._body())
                if DENOISE.get("jobId"):
                    result.setdefault("jobId", DENOISE["jobId"])
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/denoise/cancel":
                with DENOISE_LOCK:
                    DENOISE["cancelled"] = True
                    sync_job_status(dict(DENOISE), progress_key="progress")
                self._json({"ok": True})
            elif u.path == "/api/edit-external":
                result = start_external_edit(self._body())
                if EXTERNAL_EDIT.get("jobId"):
                    result.setdefault("jobId", EXTERNAL_EDIT["jobId"])
                self._json(result, 409 if result.get("error") else 200)
            elif u.path == "/api/watch":
                self._json(watch_action(self._body()))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._handle_exception(e)
        finally:
            self._log_request(started)


def _wait_for_windows_process_exit(process_id: int) -> None:
    """Wait on a Windows process handle without sending it a signal."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
    if not handle:
        return
    try:
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
    finally:
        kernel.CloseHandle(handle)


def _watch_parent() -> None:
    """Exit if our launcher dies, so the app can never leave a stray server."""
    configured = os.environ.get("LIGHTTABLE_PARENT_PID")
    if configured:
        try:
            parent_pid = int(configured)
        except ValueError:
            parent_pid = 0
        if IS_WINDOWS and parent_pid > 0:
            _wait_for_windows_process_exit(parent_pid)
            _exit_with_parent()
        while parent_pid > 0:
            time.sleep(2)
            try:
                os.kill(parent_pid, 0)
            except OSError:
                _exit_with_parent()
        return
    start_ppid = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != start_ppid:
            _exit_with_parent()


def _exit_with_parent() -> None:
    # `os._exit` skips atexit, so record the ending first: a launcher that
    # vanished is not a server crash and must not be counted as one.
    SESSION.end("parent-gone")
    STARTUP.remove()
    os._exit(0)


class LightTableServer(ThreadingHTTPServer):
    """The threading server, minus tracebacks for clients that hang up.

    Browsers and the CLI drop keep-alive sockets whenever they like. The
    stock server prints a twenty-line traceback for each of those resets,
    which buries real errors in the log. Anything else still reports.
    """

    QUIET_DISCONNECTS = (BrokenPipeError, ConnectionResetError,
                         ConnectionAbortedError)

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], self.QUIET_DISCONNECTS):
            return
        super().handle_error(request, client_address)


def main() -> None:
    global AI_INDEX, WATCH_SERVICE, PORT, HTTPD, LAUNCH_NOTICE
    STARTUP.path = recovery.startup_report_path(instance_directory())
    STARTUP.phase("starting", "Starting LightTable…")
    if os.environ.get("LIGHTTABLE_WATCH_PARENT"):
        threading.Thread(target=_watch_parent, daemon=True).start()
    if not FOLDER.is_dir():
        # With a catalog the launch folder is only one source among many, so
        # an unmounted drive or a renamed folder must not stop the app.
        if not CATALOG_ENABLED:
            STARTUP.failed(
                "folder-missing", f"The photo folder is not available: {FOLDER}",
                hint="Choose another folder from the File menu.")
            sys.exit(f"LIGHTTABLE_DIR is not a folder: {FOLDER!r}")
        LAUNCH_NOTICE = {
            "status": "folder-missing", "folder": str(FOLDER),
            "message": "The folder LightTable last opened is not available. "
                       "Its photos will return when the volume is mounted.",
        }
        print(f"LightTable: launch folder unavailable: {FOLDER}")
    try:
        acquire_catalog_process_lock()
    except CatalogLockedError as error:
        holder = error.holder or {}
        STARTUP.failed(
            "catalog-locked", str(error), holder=holder or None,
            hint=("Another copy of LightTable already has this library open. "
                  "Use that window or quit it, then try again."))
        sys.exit(f"LightTable: {error}")
    cat = open_catalog()
    STARTUP.phase("listening", "Starting the local server…")
    try:
        httpd = LightTableServer((BOUND_HOST, PORT), Handler)
    except OSError as error:
        # The launcher probed the port, but something claimed it between
        # that probe and this bind. Any free port is better than no window;
        # the launcher reads the real one from the startup report.
        if PORT == 0:
            STARTUP.failed("port", f"could not listen: {error}")
            raise
        print(f"LightTable: port {PORT} is busy ({error}); choosing another")
        httpd = LightTableServer((BOUND_HOST, 0), Handler)
    PORT = int(httpd.server_address[1])
    HTTPD = httpd
    registered_instance = write_instance_file()
    atexit.register(remove_instance_file, registered_instance)
    atexit.register(STARTUP.remove)
    crashed = SESSION.begin(
        port=PORT, revision=_git_revision(APP),
        previous_exit=os.environ.get("LIGHTTABLE_PREVIOUS_EXIT") or None)
    atexit.register(SESSION.end)
    if crashed:
        blamed = PHOTO_QUARANTINE.note_previous_crash(crashed)
        print("LightTable: the previous session did not end cleanly"
              + (f"; {blamed['name']} was being processed "
                 f"(strike {blamed['strikes']})" if blamed else ""))
        if blamed and blamed.get("quarantined"):
            print(f"LightTable: {blamed['name']} has been set aside after "
                  "repeated crashes; release it from Library Health")
    if SAFE_MODE:
        print("LightTable: safe mode; background services are off")
    if cat is not None and SCANNER is not None and not SAFE_MODE:
        # Scanning happens behind the server, never in front of it. A hundred
        # thousand files take a while to hash and read, and blocking the port
        # on that would mean the window shows nothing until it finished.
        # Anything already in the catalog is served immediately; new files and
        # any pre-catalog state file arrive as the scan reaches them.
        SCANNER.request(adopt_state_file=True)
    n = (cat.query({"limit": 1})["total"] if cat is not None
         else len(list_images()))
    AI_INDEX = AIIndexService(
        library=FOLDER,
        data_root=AI_DATA_ROOT,
        vision_helper=VISION_HELPER,
        list_images=catalog_image_names,
        source_key=file_key,
        preview_bytes=lambda name: orig_jpeg(name, 1024),
        render_busy=RENDER_LOCK.locked,
        worker_cleanup=(
            lambda: CATALOG.close() if CATALOG is not None else None),
        analyzer=LocalPhotoAnalyzer(
            VISION_HELPER, vision_provider=VISION_PROVIDER),
    )
    atexit.register(AI_INDEX.shutdown)
    if not SAFE_MODE:
        AI_INDEX.start()
    if cat is not None and os.environ.get("LIGHTTABLE_WATCH", "1") != "0" \
            and not SAFE_MODE:
        WATCH_SERVICE = watch_workflow.WatchService(
            cat, configured_watches, presets=load_presets,
            render_busy=RENDER_LOCK.locked)
        WATCH_SERVICE.start()
        atexit.register(WATCH_SERVICE.shutdown)
    print(f"LightTable: {n} images in {FOLDER}")
    print(f"UI: http://127.0.0.1:{PORT}")
    STARTUP.ready(PORT)
    if cat is not None and not SAFE_MODE and load_json_file(PREFS_FILE, {}).get("writeSidecars"):
        _queue_mirror()
    threading.Thread(
        target=maintenance_loop, args=(cat,), daemon=True,
        name="lighttable-maintenance",
    ).start()
    if RUST_WORKER_BIN and not SAFE_MODE:
        threading.Thread(target=RUST_ENGINE.warm, daemon=True,
                         name="rust-engine-warmup").start()
    # Importing the optional colour-science dependency can take hundreds of
    # milliseconds. Warm it after the port is live so the first Develop open
    # never pays that cold import, without delaying the window itself.
    def warm_colour() -> None:
        try:
            __import__("colour")
        except Exception:
            pass
        grade.warm_grade_jit()
    timer = threading.Timer(0.75, warm_colour)
    timer.daemon = True
    timer.start()
    def stop_server(_signum, _frame) -> None:
        raise SystemExit(0)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_server)
    try:
        httpd.serve_forever()
    finally:
        remove_instance_file(registered_instance)
        if cat is not None and not cat.retired:
            # Fold the log into the main file on the way out so a later
            # crash has less to lose and a copy of the file is complete.
            try:
                cat.checkpoint()
            except Exception:
                pass
            cat.close()
    if RESTART_REQUESTED:
        SESSION.end("restart")
        sys.exit(recovery.EXIT_RESTART)


# ------------------------------------------------------------ maintenance --


def maintenance_loop(cat: "catalog_module.Catalog | None") -> None:
    """Background upkeep that a long-running server used to skip entirely.

    Backups were checked once at launch, the write-ahead log was never
    checkpointed, and the exhaustive integrity check ran on the startup path
    where it delayed the window. This loop runs those on a schedule, off the
    request path, and records the results for Library Health.
    """
    first = True
    while True:
        time.sleep(5.0 if first else max(30.0, MAINTENANCE_INTERVAL))
        first = False
        if RESTART_REQUESTED:
            return
        try:
            run_maintenance(cat)
        except Exception as error:  # noqa: BLE001 - upkeep must not die
            print(f"LightTable: maintenance failed ({error})")


def run_maintenance(cat: "catalog_module.Catalog | None", *,
                    force_verify: bool = False) -> dict:
    report: dict = {"at": time.time()}
    catalog_path = catalog_module.default_catalog_path()
    disk = recovery.disk_status(catalog_path.parent)
    report["disk"] = disk
    with HEALTH_LOCK:
        HEALTH_STATE["disk"] = disk
        last_verify = HEALTH_STATE.get("verify")
    if cat is None or cat.retired:
        with HEALTH_LOCK:
            HEALTH_STATE["maintenance"] = report
        return report
    try:
        if not disk.get("low"):
            max_age = automatic_backup_max_age()
            if max_age is not None:
                directory = configured_backup_directory(cat)
                archive = cat.backup_if_due(max_age=max_age,
                                            directory=directory)
                if archive is not None:
                    report["backup"] = str(archive)
                    report["pruned"] = cat.prune_backups(directory)
        else:
            report["backup"] = "skipped: low disk space"
        if not RENDER_LOCK.locked():
            report["checkpoint"] = cat.checkpoint()
        due = (force_verify or last_verify is None
               or time.time() - float(last_verify.get("checkedAt") or 0)
               >= FULL_VERIFY_INTERVAL)
        if due:
            verify = cat.verify(full=True)
            with HEALTH_LOCK:
                HEALTH_STATE["verify"] = verify
            report["verify"] = verify
            if not verify.get("ok"):
                print("LightTable: the catalog needs attention: "
                      + "; ".join(verify.get("problems") or ["unknown"]))
    finally:
        cat.close()
    with HEALTH_LOCK:
        HEALTH_STATE["maintenance"] = report
    return report


# ---------------------------------------------------------------- recovery --


def recovery_status(*, include_backups: bool = True) -> dict:
    """Everything Library Health shows: state, choices, and evidence."""
    cat = catalog_handle()
    catalog_path = catalog_module.default_catalog_path()
    backup_dir = (configured_backup_directory(cat) if cat is not None
                  else _backup_directory_for(catalog_path))
    with HEALTH_LOCK:
        verify = HEALTH_STATE.get("verify")
        maintenance = HEALTH_STATE.get("maintenance")
    notice = CATALOG_NOTICE
    damaged = bool(notice and notice.get("status") in ("damaged", "unavailable"))
    salvage_preview = None
    if damaged and catalog_path.exists():
        salvage_preview = catalog_module.catalog_summary(catalog_path)
    return {
        "catalog": {
            "enabled": cat is not None,
            "configured": CATALOG_ENABLED,
            "path": str(catalog_path),
            "exists": catalog_path.exists(),
            "notice": notice,
            "damaged": damaged,
            "stats": cat.stats() if cat is not None else None,
            "syncService": recovery.sync_service_for(catalog_path),
        },
        "backups": {
            "directory": str(backup_dir),
            "items": (catalog_module.list_backups(backup_dir)
                      if include_backups else []),
            "frequency": str(load_preferences().get("backupFrequency", "daily")),
        },
        "salvage": {
            "available": damaged and salvage_preview is not None,
            "readable": salvage_preview,
        },
        "quarantine": PHOTO_QUARANTINE.entries(),
        "session": SESSION.summary(),
        "verify": verify,
        "maintenance": maintenance,
        "disk": recovery.disk_status(catalog_path.parent),
        "documents": [recovery.inspect_json(PREFS_FILE),
                      recovery.inspect_json(PRESETS_FILE)],
        "safeMode": SAFE_MODE,
        "launch": LAUNCH_NOTICE,
        "log": os.environ.get("LIGHTTABLE_SERVER_LOG"),
        "recoveryFolder": str(catalog_path.parent / "Recovery"),
        "cachePath": str(CACHE.resolve()),
    }


def _retire_catalog_for_replacement() -> None:
    """Stop the live catalog before its file is swapped underneath it."""
    global CATALOG, SCANNER, CATALOG_NOTICE
    cat = CATALOG
    if cat is None:
        return
    CATALOG = None
    SCANNER = None
    CATALOG_NOTICE = {"status": "restarting",
                      "message": "The catalog is being replaced."}
    if WATCH_SERVICE is not None:
        try:
            WATCH_SERVICE.shutdown()
        except Exception:
            pass
    cat.retire()


def recovery_action(body: dict) -> dict:
    """Apply one Library Health decision.

    Destructive choices (restore, rebuild, reset) first quarantine whatever
    is at the catalog path and then ask for a clean restart, so the new
    process opens the replacement file with nothing cached from the old one.
    """
    action = str(body.get("action", ""))
    cat = catalog_handle()
    catalog_path = catalog_module.default_catalog_path()
    if action == "verify":
        cat = require_catalog()
        verify = cat.verify(full=bool(body.get("full", True)))
        with HEALTH_LOCK:
            HEALTH_STATE["verify"] = verify
        return {"ok": True, "verify": verify}
    if action == "repair":
        cat = require_catalog()
        result = cat.repair(backup_directory=configured_backup_directory(cat))
        with HEALTH_LOCK:
            HEALTH_STATE["verify"] = result["verify"]
        invalidate_library_cache()
        EVENTS.publish("library", {"reason": "repair"})
        return {"ok": True, **result}
    if action == "checkpoint":
        cat = require_catalog()
        return {"ok": True, "checkpoint": cat.checkpoint()}
    if action == "backup":
        cat = require_catalog()
        directory = configured_backup_directory(cat)
        archive = cat.backup(directory)
        cat.prune_backups(directory)
        return {"ok": True, "archive": str(archive)}
    if action == "maintenance":
        return {"ok": True, "maintenance": run_maintenance(
            cat, force_verify=True)}
    if action == "rescan":
        if SCANNER is None:
            raise ValueError("the catalog is not available")
        SCANNER.request(None)
        EVENTS.publish("library", {"reason": "scan"})
        return {"ok": True, "status": SCANNER.status}
    if action == "rebuild-search":
        cat = require_catalog()
        return {"ok": True, "reindexed": cat.rebuild_search_index()}
    if action == "release":
        name = str(body.get("name", ""))
        if not name:
            raise ValueError("a photo name is required")
        return {"ok": True, "released": PHOTO_QUARANTINE.release(name),
                "quarantine": PHOTO_QUARANTINE.entries()}
    if action == "set-aside":
        name = str(body.get("name", ""))
        if not name:
            raise ValueError("a photo name is required")
        for _ in range(recovery.QUARANTINE_STRIKES):
            entry = PHOTO_QUARANTINE.strike(name, "manual")
        return {"ok": True, "entry": entry,
                "quarantine": PHOTO_QUARANTINE.entries()}
    if action == "clear-caches":
        result = purge_generated_cache()
        invalidate_library_cache()
        return {"ok": True, **result}
    if action == "restore":
        archive = Path(str(body.get("archive", ""))).expanduser()
        backup_dir = (configured_backup_directory(cat) if cat is not None
                      else _backup_directory_for(catalog_path))
        if archive.parent.resolve() != backup_dir.resolve():
            raise ValueError("the archive must come from the backup folder")
        if cat is not None:
            # The live catalog is healthy enough to run; keep a copy of the
            # edits it holds before an older backup replaces them.
            cat.backup(backup_dir)
            _retire_catalog_for_replacement()
        result = catalog_module.restore_backup(catalog_path, archive)
        request_restart("restore")
        return {"ok": True, "restart": True, **result}
    if action == "rebuild":
        if cat is not None:
            cat.backup(configured_backup_directory(cat))
            _retire_catalog_for_replacement()
        result = catalog_module.rebuild_catalog(catalog_path)
        request_restart("rebuild")
        return {"ok": True, "restart": True, **result}
    if action == "reset":
        if not body.get("confirm"):
            raise ValueError("reset needs confirm: true")
        if cat is not None:
            cat.backup(configured_backup_directory(cat))
            _retire_catalog_for_replacement()
        result = catalog_module.create_fresh_catalog(catalog_path)
        request_restart("reset")
        return {"ok": True, "restart": True, **result}
    if action == "restart":
        request_restart("requested")
        return {"ok": True, "restart": True}
    raise ValueError(f"unknown recovery action: {action}")




# ------------------------------------------------- catalog-backed actions --


def catalog_sources_action(body: dict) -> dict:
    """Add, forget, favourite, or rename a source folder.

    Source management used to live in the native hosts, each with its own
    storage, which is why macOS and Windows kept two different favourites
    models. The catalog owns the list now and both hosts read it from here.
    """
    cat = require_catalog()
    action = str(body.get("action", "list"))
    if action == "add":
        path = Path(str(body["path"])).expanduser()
        if not path.is_dir():
            raise ValueError("that folder does not exist")
        source_id = cat.add_source(path, favorite=bool(body.get("favorite")))
        imported = None
        if body.get("importState", True):
            # A folder that was edited before the catalog existed carries its
            # own state file; fold it in on first sight so nothing is lost.
            catalog_scan.scan_source(cat, source_id)
            imported = catalog_scan.import_state_file(cat, source_id)
        elif SCANNER is not None:
            SCANNER.request(source_id)
        return {"ok": True, "sourceId": source_id, "imported": imported,
                "sources": cat.sources()}
    if action == "remove":
        cat.remove_source(int(body["id"]))
    elif action == "favorite":
        cat.set_source_favorite(int(body["id"]), bool(body.get("favorite")))
    elif action == "rename":
        cat.rename_source(int(body["id"]), str(body.get("name", "")))
    elif action == "rescan":
        if SCANNER is not None:
            SCANNER.request(int(body["id"]) if body.get("id") else None)
    elif action != "list":
        raise ValueError(f"unknown source action: {action}")
    return {"ok": True, "sources": cat.sources()}


def catalog_collections_action(body: dict) -> dict:
    cat = require_catalog()
    action = str(body.get("action", "list"))
    if action in ("create", "create_smart"):
        collection_id = cat.add_collection(
            str(body.get("name", "Collection")),
            kind="smart" if action == "create_smart" else "regular",
            rules=body.get("rules") if action == "create_smart" else None,
            parent_id=int(body["parentId"]) if body.get("parentId") else None)
        members = [int(i) for i in body.get("imageIds", [])]
        if members and action == "create":
            cat.set_collection_members(collection_id, members)
        _queue_mirror()
        return {"ok": True, "id": collection_id,
                "collections": cat.collections(),
                "library": current_library_state()}
    if action == "delete":
        cat.delete_collection(int(body["id"]))
    elif action == "add":
        cat.add_to_collection(int(body["id"]),
                              [int(i) for i in body.get("imageIds", [])])
    elif action == "set":
        cat.set_collection_members(int(body["id"]),
                                   [int(i) for i in body.get("imageIds", [])])
    elif action != "list":
        raise ValueError(f"unknown collection action: {action}")
    if action != "list":
        _queue_mirror()
    return {"ok": True, "collections": cat.collections(),
            "library": current_library_state()}


def import_sidecars(body: dict) -> dict:
    """Read rating, label, keywords, IPTC, and develop settings from XMP.

    This is the cheapest migration path: someone who has been tagging in
    another editor with sidecars turned on gets their work without exporting
    anything. Develop settings are off by default because they were authored
    against a different renderer.
    """
    import xmp_sidecar

    cat = require_catalog()
    apply = body.get("apply") or {}
    want_metadata = apply.get("metadata", True)
    want_develop = apply.get("develop", False)
    want_crop = apply.get("crop", False)
    conflict = str(body.get("conflict", "skip-existing"))

    names = body.get("names")
    if not names:
        scope = require_catalog().query({"limit": 5000,
                                         **(body.get("scope") or {})})
        names = [item["name"] for item in scope["items"]]

    report = {"read": 0, "applied": 0, "skipped": 0, "missing": 0,
              "ignored": {}, "errors": []}
    for name in names[:20000]:
        try:
            path = src_path(name)
        except ValueError:
            report["missing"] += 1
            continue
        parsed = xmp_sidecar.read_for(path)
        if not parsed:
            report["missing"] += 1
            continue
        report["read"] += 1
        image_id = catalog_image_id(name)
        if image_id is None:
            report["skipped"] += 1
            continue
        current = cat.state_for(image_id)
        has_edits = bool(current.get("grade") or current.get("params")
                         or current.get("crop"))
        entry: dict = {}
        if want_metadata:
            if parsed.get("rating") is not None:
                entry["rating"] = max(0, min(5, int(parsed["rating"])))
            if parsed.get("label"):
                entry["label"] = clean_label(parsed["label"])
            keywords = list(parsed.get("keywordPaths")
                            or parsed.get("keywords") or [])
            if keywords:
                entry["keywords"] = clean_keywords(keywords)
        if (want_develop or want_crop) and not (has_edits
                                                and conflict == "skip-existing"):
            patch = xmp_sidecar.as_edit_patch(parsed)
            if want_develop and patch.get("grade"):
                entry["grade"] = grade.clean(patch["grade"])
            if want_crop and patch.get("crop"):
                entry["crop"] = clean_crop(patch["crop"])
            if want_crop and patch.get("optics"):
                entry["optics"] = edits.clean_optics(patch["optics"])
            for note in patch.get("ignored", []) or []:
                report["ignored"][note] = report["ignored"].get(note, 0) + 1
        if entry:
            cat.save_state(image_id, entry)
            report["applied"] += 1
        else:
            report["skipped"] += 1
        iptc_fields = {key: parsed.get(key) for key in
                       ("title", "caption", "creator", "copyright", "credit",
                        "headline", "city", "state", "country")
                       if parsed.get(key)}
        gps = parsed.get("gps")
        if gps:
            iptc_fields.update({"gps_lat": gps.get("lat"),
                                "gps_lon": gps.get("lon"),
                                "gps_alt": gps.get("alt")})
        if want_metadata and iptc_fields:
            cat.save_iptc(image_id, iptc_fields)
    return report


def start_catalog_import(body: dict) -> dict:
    """Import another editor's catalog in the background."""
    import catalog_import

    cat = require_catalog()
    path = Path(str(body["path"])).expanduser()
    if not path.is_file():
        raise ValueError("that catalog file does not exist")
    if body.get("inspectOnly"):
        return catalog_import.inspect(path)
    with IMPORT_LOCK:
        if IMPORT_JOB["running"]:
            return {"error": "an import is already running"}
        IMPORT_JOB.update(running=True, stage="starting", done=0, total=0,
                          result=None, error=None)
        IMPORT_JOB["jobId"] = JOBS.create(
            "catalog-import", total=0, state="running")["id"]

    def progress(update: dict) -> None:
        with IMPORT_LOCK:
            IMPORT_JOB.update(update)
            sync_job_status(dict(IMPORT_JOB))

    def run() -> None:
        try:
            result = catalog_import.import_catalog(
                cat, path, options=body.get("options") or {},
                progress=progress, root_map=body.get("rootMap") or None)
            with IMPORT_LOCK:
                IMPORT_JOB.update(running=False, stage="done", result=result)
                sync_job_status(dict(IMPORT_JOB), result=result)
        except Exception as error:  # noqa: BLE001
            with IMPORT_LOCK:
                IMPORT_JOB.update(running=False, stage="failed",
                                  error=str(error))
                sync_job_status(dict(IMPORT_JOB))

    threading.Thread(target=run, name="lighttable-catalog-import",
                     daemon=True).start()
    return {"queued": True}


def scan_ingest_source(body: dict) -> dict:
    import ingest_workflow

    root = Path(str(body["path"])).expanduser()
    if not root.is_dir():
        raise ValueError("that folder does not exist")
    items = ingest_workflow.scan_source(root)
    cat = catalog_handle()
    known: set[str] = set()
    if cat is not None:
        known = {row["header_hash"] for row in cat.connection.execute(
            "SELECT DISTINCT header_hash FROM files"
            " WHERE header_hash IS NOT NULL").fetchall()}
    request = ingest_workflow.clean_plan_request(body.get("request"))
    plan = ingest_workflow.build_plan(items, request, existing_hashes=known)
    return {"plan": plan, "scanned": len(items)}


def start_ingest(body: dict) -> dict:
    """Copy from a card with verification, then add the results to the catalog.

    Verification is the point: a card is often erased right after, so a copy
    that silently truncated is the one failure mode that loses photographs for
    good. Sources are never modified or deleted here.
    """
    import ingest_workflow

    request = ingest_workflow.clean_plan_request(body.get("request"))
    plan = body.get("plan")
    if not plan or not plan.get("items"):
        scanned = scan_ingest_source({"path": body["path"],
                                      "request": body.get("request")})
        plan = scanned["plan"]
    items = plan["items"]
    with INGEST_LOCK:
        if INGEST["running"]:
            return {"error": "an ingest is already running"}
        INGEST.update(running=True, done=0, total=len(items), errors=[],
                      copied=0, bytes=0, cancelled=False)
        INGEST["jobId"] = JOBS.create(
            "ingest", total=len(items), state="running",
            cancel=cancel_ingest_job)["id"]

    def run() -> None:
        cat = catalog_handle()
        destinations: set[Path] = set()
        try:
            for item in items:
                with INGEST_LOCK:
                    if INGEST["cancelled"]:
                        break
                result = ingest_workflow.copy_item(item,
                                                   verify=request["verify"])
                with INGEST_LOCK:
                    INGEST["done"] += 1
                    if result["ok"]:
                        INGEST["copied"] += 1
                        INGEST["bytes"] += int(result.get("bytes", 0) or 0)
                        destinations.add(Path(result["destination"]).parent)
                    else:
                        INGEST["errors"].append(
                            {"source": item.get("source"),
                             "error": result.get("error")})
                    sync_job_status(dict(INGEST))
            if cat is not None and destinations:
                root = Path(request["destination"])
                source_id = cat.add_source(root)
                catalog_scan.scan_source(cat, source_id)
        except Exception as error:  # noqa: BLE001
            with INGEST_LOCK:
                INGEST["errors"].append({"error": str(error)})
                sync_job_status(dict(INGEST))
        finally:
            with INGEST_LOCK:
                INGEST["running"] = False
                sync_job_status(dict(INGEST))
            if cat is not None:
                cat.close()

    threading.Thread(target=run, name="lighttable-ingest", daemon=True).start()
    return {"queued": len(items)}


def rename_photos(body: dict) -> dict:
    """Batch rename originals and their sidecars, with an undo record.

    Renaming is safe here in a way it was not before: caches are keyed on
    content, and the catalog row follows the file, so a rename costs nothing
    and can be reversed from `rename_log`.
    """
    import ingest_workflow

    cat = require_catalog()
    names = [str(n) for n in body.get("names", [])][:5000]
    template = str(body.get("template", "{filename}"))
    try:
        start = max(1, int(body.get("start", 1)))
    except (TypeError, ValueError):
        start = 1
    batch = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16]
    planned: list[tuple[Path, Path, int, str]] = []
    taken: set[Path] = set()
    for offset, name in enumerate(names):
        path, source_id, relpath, _ = resolve_name(name)
        context = {
            "filename": path.stem,
            # Zero-padded so a renamed set sorts the way an ingested one does.
            "sequence": f"{start + offset:0{ingest_workflow.SEQUENCE_DIGITS}d}",
            "custom": str(body.get("custom", "")),
            "camera": "", "captureTime": "",
        }
        item = next((i for i in ingest_workflow.scan_source(path.parent,
                                                            limit=1)
                     if i["path"] == str(path)), None)
        if item:
            context["camera"] = item.get("camera", "")
            context["captureTime"] = item.get("captureTime", "")
        stem = ingest_workflow.render_path(template, context)
        target = path.with_name(f"{stem}{path.suffix}")
        suffix = 2
        while (target in taken or target.exists()) and target != path:
            target = path.with_name(f"{stem}-{suffix}{path.suffix}")
            suffix += 1
        taken.add(target)
        planned.append((path, target, source_id, relpath))

    if body.get("preview"):
        return {"preview": [{"from": str(a.name), "to": str(b.name)}
                            for a, b, _, _ in planned[:20]],
                "total": len(planned)}

    changes = []
    plans = []
    try:
        for path, target, source_id, relpath in planned:
            if target == path:
                continue
            actual_source = source_id if source_id is not None else PRIMARY_SOURCE_ID
            if actual_source is None:
                raise RuntimeError("the catalog source is unavailable")
            new_relpath = (relpath.rsplit("/", 1)[0] + "/" + target.name
                           if "/" in relpath else target.name)
            plans.append(_photo_move_plan(path, target))
            changes.append((actual_source, relpath, actual_source, new_relpath))
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "renamed": 0, "error": str(error)}

    staged = []
    try:
        for plan in plans:
            _stage_photo_move(plan)
            staged.append(plan)
        cat.relocate_files(changes, batch=batch)
    except Exception as error:
        rollback_errors = _rollback_photo_moves(staged)
        detail = str(error)
        if rollback_errors:
            detail += "; recovery also needs attention: " + "; ".join(
                rollback_errors)
        return {"ok": False, "renamed": 0, "error": detail}
    warnings = _finish_photo_moves(staged)
    renamed = len(staged)
    invalidate_library_cache()
    return {"ok": True, "renamed": renamed, "batch": batch,
            "warnings": warnings}


def trash_photos(body: dict) -> dict:
    """Ask the host to move rejected originals to the Trash.

    The server never unlinks a photograph. It resolves and validates the paths
    and hands them to the native host, so deletion always lands somewhere
    recoverable and always goes through the platform's own confirmation.
    """
    names = [str(n) for n in body.get("names", [])][:5000]
    paths: list[str] = []
    for name in names:
        try:
            path = src_path(name)
        except ValueError:
            continue
        paths.append(str(path))
        for sidecar in (path.with_suffix(".xmp"), Path(str(path) + ".xmp")):
            if sidecar.exists():
                paths.append(str(sidecar))
    return {"ok": True, "paths": paths, "count": len(paths)}


def reveal_photo(body: dict) -> dict:
    """Resolve one catalog name for the native host's Finder command."""
    name = str(body.get("name", ""))
    if not name:
        raise ValueError("no photo selected")
    return {"ok": True, "path": str(src_path(name))}


def auto_geometry(body: dict) -> dict:
    """Level or upright one photo from its own lines."""
    import geometry_auto

    name = str(body["name"])
    mode = str(body.get("mode", "level"))
    rotate = float(body.get("rotate", 0) or 0)
    source = CACHE / "semantic" / f"{file_key(name)}_{rot90k(rotate)}_source.jpg"
    if not source.exists():
        durable_io.atomic_write_bytes(source, orig_jpeg(name, 1400, rotate))
    image = np.asarray(Image.open(source).convert("RGB")).astype(np.float32) / 255.0
    return geometry_auto.analyze(image, mode, body.get("guides"))


def start_enhance(body: dict) -> dict:
    """Denoise or upscale one photo into a new master file."""
    import enhance_workflow

    name = str(body["name"])
    mode = str(body.get("mode", "denoise"))
    capabilities = enhance_workflow.capabilities()
    if not capabilities.get("available"):
        return {"ok": False, "error": capabilities.get("reason",
                                                       "not available")}
    source = src_path(name)
    destination = enhance_workflow.enhanced_destination(FOLDER, source.stem,
                                                        mode)
    result = enhance_workflow.enhance_file(
        source, destination, mode, request=body.get("request"))
    cat = catalog_handle()
    if result.get("ok") and cat is not None and PRIMARY_SOURCE_ID is not None:
        catalog_scan.scan_source(cat, PRIMARY_SOURCE_ID)
    return result


def _run_denoise(name: str, params: dict) -> None:
    """Build the fingerprinted RAW masters while publishing tile progress."""
    cat = catalog_handle()

    def progress(event: dict) -> None:
        with DENOISE_LOCK:
            DENOISE.update(progress=int(event.get("progress", 0)),
                            total=int(event.get("total", 0)))
            sync_job_status(dict(DENOISE), progress_key="progress")

    try:
        tiff_for(name, params, denoise_status=progress,
                 denoise_cancel=DENOISE)
        neutral_tiff_for(name, params)
        with DENOISE_LOCK:
            DENOISE.update(running=False, done=True, error="")
            sync_job_status(dict(DENOISE), progress_key="progress")
    except Exception as error:  # surfaced verbatim in the Develop panel
        with DENOISE_LOCK:
            cancelled = DENOISE.get("cancelled", False)
            DENOISE.update(running=False, done=False,
                            error=("Denoise cancelled" if cancelled
                                   else str(error)))
            sync_job_status(dict(DENOISE), progress_key="progress")
    finally:
        if cat is not None:
            cat.close()


def start_denoise(body: dict) -> dict:
    """Start one non-live learned denoise for a RAW photograph."""
    import enhance_workflow

    name = str(body.get("name", ""))
    if not is_raw(name):
        return {"ok": False, "error": "Learned denoise requires a RAW original"}
    capabilities = enhance_workflow.capabilities()
    if not capabilities.get("modes", {}).get("denoise"):
        return {"ok": False, "error": capabilities.get(
            "reason", "Learned denoise is unavailable")}
    params = fp.clean_params(body.get("params") or {})
    params["learned_denoise"] = True
    with DENOISE_LOCK:
        if DENOISE["running"]:
            return {"ok": False, "error": "A denoise is already running"}
        DENOISE.update(running=True, name=name, progress=0, total=0,
                       error="", cancelled=False, done=False)
        DENOISE["jobId"] = JOBS.create(
            "denoise", total=0, state="running",
            cancel=cancel_denoise_job)["id"]
    DENOISE_POOL.submit(_run_denoise, name, params)
    return {"ok": True, "started": True, "name": name,
            "params": params}


# How many images the initial payload carries. The grid pages beyond this
# through /api/catalog/query; shipping a hundred thousand records with their
# edit blobs would cost more than every other part of start-up combined.
LIBRARY_PAGE_LIMIT = int(os.environ.get("LIGHTTABLE_LIBRARY_PAGE", "600"))


def current_library_state() -> dict:
    """Collections, stacks, and virtual copies from whichever store is live.

    The client's shape is unchanged so the existing library rail keeps working;
    only where the rows come from has moved.
    """
    cat = catalog_handle()
    if cat is None:
        return library_state()
    collections = []
    for record in cat.collections():
        members: list[str] = []
        if record["type"] == "regular":
            rows = cat.connection.execute(
                "SELECT i.copy_ident, f.relpath, f.source_id"
                " FROM collection_images ci"
                " JOIN images i ON i.id=ci.image_id"
                " JOIN files f ON f.id=i.file_id"
                " WHERE ci.collection_id=? ORDER BY ci.position",
                (record["id"],)).fetchall()
            members = [catalog_module.qualified_name(
                row["source_id"], row["relpath"], row["copy_ident"])
                for row in rows]
        collections.append({
            "id": str(record["id"]), "name": record["name"],
            "type": record["type"], "members": members,
            "rules": record.get("rules") or {},
        })

    stacks = []
    for row in cat.connection.execute(
            "SELECT id, name, collapsed FROM stacks").fetchall():
        members = [catalog_module.qualified_name(
            member["source_id"], member["relpath"], member["copy_ident"])
            for member in cat.connection.execute(
                "SELECT i.copy_ident, f.relpath, f.source_id"
                " FROM stack_images si JOIN images i ON i.id=si.image_id"
                " JOIN files f ON f.id=i.file_id"
                " WHERE si.stack_id=? ORDER BY si.position",
                (row["id"],)).fetchall()]
        if len(members) >= 2:
            stacks.append({"id": str(row["id"]), "name": row["name"],
                           "members": members,
                           "collapsed": bool(row["collapsed"])})

    copies = []
    for row in cat.connection.execute(
            "SELECT i.id, i.copy_ident, i.display_name, i.created_at,"
            " f.relpath, f.source_id FROM images i"
            " JOIN files f ON f.id=i.file_id"
            " WHERE i.virtual=1 AND f.missing=0").fetchall():
        source = catalog_module.qualified_name(row["source_id"],
                                               row["relpath"])
        copies.append({
            "id": row["copy_ident"],
            "name": catalog_module.qualified_name(
                row["source_id"], row["relpath"], row["copy_ident"]),
            "source": source, "displayName": row["display_name"],
            "created": row["created_at"],
        })
    return {"collections": collections, "stacks": stacks,
            "virtualCopies": copies}


def browser_catalog_query(spec: dict | None = None, *,
                          include_state: bool = False) -> dict:
    """Query the catalog without exposing unsupported video rows to the UI."""
    query = dict(spec) if isinstance(spec, dict) else {}
    raw_excluded = query.get("excludeKinds")
    excluded = list(raw_excluded) if isinstance(
        raw_excluded, (list, tuple, set)) else []
    if "video" not in excluded:
        excluded.append("video")
    query["excludeKinds"] = excluded
    return require_catalog().query(query, include_state=include_state)


def library_payload(limit: int = LIBRARY_PAGE_LIMIT) -> tuple[list[dict], dict]:
    """Build the initial library payload from whichever store is live.

    With the catalog open this is a single SQL statement plus one keyword
    lookup, and the folder tree comes from the catalog rather than a fresh walk
    of the filesystem. The pre-catalog path is kept intact for folder mode and
    for the tests that cover it.
    """
    cat = catalog_handle()
    if cat is None:
        st = load_state()
        snapshot = library_snapshot()
        physical_names = [name for name in snapshot["names"]
                          if not is_video(name)]
        names = [name for name in library_item_names(st)
                 if not is_video(name)]
        ai_results = AI_INDEX.results(physical_names) if AI_INDEX else {}
        copies = {item["name"]: item
                  for item in library_state(st)["virtualCopies"]}
        rows = []
        for name in names:
            copy = copies.get(name)
            source = copy["source"] if copy else name
            parent = Path(source).parent.as_posix()
            entry = catalog_entry_for(name)
            rows.append(dict(
                entry, name=name, sourceName=source,
                raw=is_raw(source), virtual=bool(copy),
                kind=("video" if is_video(source) else
                      "raw" if is_raw(source) else "processed"),
                folder="" if parent == "." else parent,
                displayName=(copy["displayName"] if copy
                             else Path(source).name),
                fileKey=file_key(name),
                mtime=snapshot["mtimes"].get(source, 0.0),
                width=entry.get("width"),
                height=entry.get("height"),
                ai=ai_results.get(source),
            ))
        directories = {row["path"] for row in snapshot["folders"]}
        visible_snapshot = dict(snapshot)
        visible_snapshot.update(
            names=physical_names,
            mtimes={name: snapshot["mtimes"][name]
                    for name in physical_names},
            folders=_library_folder_rows(physical_names, directories),
            total=len(rows),
        )
        return rows, visible_snapshot

    limit = max(1, min(20000, int(limit or LIBRARY_PAGE_LIMIT)))
    page = browser_catalog_query({
        "limit": limit, "sort": {"field": "capture", "dir": "desc"},
    })
    rows = []
    for item in page["items"]:
        source = item["relpath"]
        rows.append({
            "name": item["name"],
            "sourceName": source,
            "raw": item["raw"],
            "kind": item["kind"],
            "virtual": item["virtual"],
            "folder": item["folder"],
            "displayName": item["displayName"],
            "fileKey": item["fileKey"],
            "mtime": item["mtime"],
            "date": item["captureTime"],
            "status": item["status"],
            "rating": item["rating"],
            "label": item["label"],
            "width": item.get("width"),
            "height": item.get("height"),
            "hasEdits": item.get("hasEdits", False),
            "stateLoaded": False,
            "id": item["id"],
            "catalogId": item["id"],
            "sourceId": item["sourceId"],
            "ai": None,
        })
    folders = [{"path": row["relpath"], "name": row["name"] or "",
                "count": row["count"]}
               for row in catalog_folder_rows()]
    snapshot = {"names": [row["name"] for row in rows], "folders": folders,
                "mtimes": {row["name"]: row["mtime"] for row in rows},
                "total": page["total"]}
    return rows, snapshot


if __name__ == "__main__":
    main()
