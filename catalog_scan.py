"""Filesystem scanning, content hashing, and migration into the catalog.

Scanning is separated from `catalog.py` so the store stays a pure data layer.
Three jobs live here:

* walking a source and diffing it against what the catalog already holds,
* hashing file content so identity survives a move or a rename,
* importing the per-folder `.lighttable-state.json` files that were the
  library before the catalog existed.

The walk is deliberately shallow in what it reads. Only new or changed files
pay for a header hash and a metadata read; everything else is a `stat` compare.
That is what keeps a rescan of a large library close to the cost of the walk
itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import catalog as catalog_module
import durable_io
import dam_filters
import media_formats
import media_availability

RAW_EXTS = media_formats.RAW_EXTENSIONS
PROCESSED_EXTS = media_formats.PROCESSED_EXTENSIONS
VIDEO_EXTS = media_formats.VIDEO_EXTENSIONS
ALL_EXTS = media_formats.PHOTO_EXTENSIONS

EXPORT_DIR_NAME = "film-exports"
MERGE_DIR_NAME = "LightTable Merges"
# Merge masters are first-class derivatives and must be indexable. Ordinary
# delivery exports remain outside the working catalog.
SKIP_DIRS = {EXPORT_DIR_NAME, "__pycache__"}

HEADER_CHUNK = 65536
METADATA_VERSION = 4


def header_hash(path: Path, *, chunk: int = HEADER_CHUNK) -> str:
    """Content identity: BLAKE2b over the file size and its first 64 KiB.

    A full hash of a 60 MB raw file is too slow to run across a whole library,
    and a path is not identity at all. The header of a camera file carries its
    maker notes, timestamps, and thumbnail, so a size-plus-header digest
    separates distinct captures reliably while costing one read per file.

    This is a *fast* identity, not a cryptographic one: two files with the same
    size and identical first 64 KiB collide. Ingest verification uses a full
    hash where that matters.
    """
    digest = hashlib.blake2b(digest_size=16)
    try:
        stat = path.stat()
        if media_availability.from_stat(stat) != "local":
            return ""
        size = stat.st_size
    except OSError:
        return ""
    digest.update(str(size).encode("ascii"))
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(chunk))
    except OSError:
        return ""
    return digest.hexdigest()


def kind_for(ext: str) -> str:
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    return "processed"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S")


def walk_source(root: Path, *, limit: int = 500000,
                on_incomplete: Callable[[str], None] | None = None) -> Iterable[dict]:
    """Yield one lightweight record per acceptable file under `root`.

    Uses `os.scandir` rather than `rglob` because it returns the stat data
    from the directory read itself, which halves the syscalls on a cold walk.
    Report gaps so callers cannot mistake an unvisited file for a missing one.
    """
    if limit < 1:
        raise ValueError("scan limit must be positive")
    report = on_incomplete or (lambda message: None)
    root = Path(root)
    seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            report(f"{directory}: {error}")
            continue
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name not in SKIP_DIRS:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as error:
                report(f"{entry.path}: {error}")
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALL_EXTS:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                report(f"{entry.path}: {error}")
                continue
            if seen >= limit:
                report(f"Scan stopped at the limit of {limit} photos")
                return
            relpath = os.path.relpath(entry.path, root).replace(os.sep, "/")
            yield {
                "relpath": relpath,
                "filename": name,
                "ext": ext,
                "kind": kind_for(ext),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "mtime_iso": _iso(stat.st_mtime),
                "path": entry.path,
                "availability": media_availability.from_stat(stat),
            }
            seen += 1


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _largest_image_dimensions(fields: dict[str, object]) \
        -> tuple[int | None, int | None]:
    """Choose the full image IFD rather than a RAW's embedded thumbnail."""
    candidates: list[tuple[int, int]] = []
    suffix_pairs = (
        (".PixelXDimension", ".PixelYDimension"),
        (".ImageWidth", ".ImageHeight"),
        (".ImageWidth", ".ImageLength"),
    )
    for width_suffix, height_suffix in suffix_pairs:
        for key, raw_width in fields.items():
            if not key.endswith(width_suffix):
                continue
            prefix = key[:-len(width_suffix)]
            width = _positive_int(raw_width)
            height = _positive_int(fields.get(prefix + height_suffix))
            if width is not None and height is not None:
                candidates.append((width, height))
    return max(candidates, key=lambda pair: pair[0] * pair[1]) \
        if candidates else (None, None)


def read_metadata(path: Path) -> dict:
    """Capture time, camera, lens, and dimensions, read once at scan time.

    The detail panel still shells out to exiftool for its full field list; this
    only needs the handful of values the catalog sorts and filters on, so it
    uses the in-process binding on both platforms.
    """
    out: dict[str, object] = {}
    if media_availability.availability(path) != "local":
        return out
    try:
        import exiv2
    except ImportError:
        return out
    try:
        image = exiv2.ImageFactory.open(str(path))
        image.readMetadata()
        data = image.exifData()
    except Exception:
        return out

    def value(*keys: str) -> str:
        for key in keys:
            try:
                item = data.findKey(exiv2.ExifKey(key))
                if item != data.end():
                    return item.toString()
            except Exception:
                continue
        return ""

    captured = value("Exif.Photo.DateTimeOriginal", "Exif.Image.DateTime")
    if captured:
        # EXIF writes `2024:05:01 12:00:00`; ISO order sorts correctly as text.
        parts = captured.strip().split(" ")
        if len(parts) == 2 and parts[0].count(":") == 2:
            out["capture_time"] = f"{parts[0].replace(':', '-')}T{parts[1]}"
        else:
            out["capture_time"] = captured
    out["camera_make"] = value("Exif.Image.Make") or None
    out["camera_model"] = value("Exif.Image.Model") or None
    out["lens"] = (value("Exif.Photo.LensModel", "Exif.Image.LensInfo")
                   or None)
    for column, keys in {
        "iso": ("Exif.Photo.PhotographicSensitivity", "Exif.Photo.ISOSpeedRatings"),
        "focal_length": ("Exif.Photo.FocalLength",),
        "aperture": ("Exif.Photo.FNumber",),
        "shutter_seconds": ("Exif.Photo.ExposureTime",),
    }.items():
        out[column] = dam_filters.positive_number(value(*keys))
    dimension_fields: dict[str, str] = {}
    try:
        for item in data:
            key = item.key()
            if key.endswith((".PixelXDimension", ".PixelYDimension",
                             ".ImageWidth", ".ImageLength")):
                dimension_fields[key] = item.toString()
    except Exception:
        dimension_fields = {}
    out["width"], out["height"] = _largest_image_dimensions(
        dimension_fields)
    text = value("Exif.Image.Orientation")
    try:
        out["orientation"] = int(text) if text else None
    except ValueError:
        out["orientation"] = None
    out["metadata_version"] = METADATA_VERSION
    return out


def scan_source(cat: catalog_module.Catalog, source_id: int, *,
                progress: Callable[[dict], None] | None = None,
                on_local_file: Callable[[int, str], object] | None = None,
                read_metadata_for_new: bool = True,
                limit: int = 500000) -> dict:
    """Bring one source's rows in step with the filesystem.

    Returns counts rather than rows: the UI refreshes through the normal query
    path afterwards, so there is no need to ship the whole library twice.
    """
    source = cat.source_by_id(source_id)
    if not source:
        raise ValueError(f"unknown source {source_id}")
    root = Path(source["path"])
    if not root.is_dir():
        return {"added": 0, "updated": 0, "missing": 0, "relinked": 0,
                "unavailable": True, "complete": False,
                "error": f"Source is unavailable: {root}"}

    existing: dict[str, dict] = {}
    for row in cat.connection.execute(
            "SELECT relpath, id, size, mtime_ns, header_hash, metadata_version, missing, availability "
            "FROM files WHERE source_id=?",
            (source_id,)).fetchall():
        existing[row["relpath"]] = dict(row)

    added = updated = relinked = cloud_only = 0
    seen: set[str] = set()
    batch: list[dict] = []

    def flush(records: list[dict]) -> None:
        nonlocal added, updated, relinked, cloud_only
        if not records:
            return
        with cat.write() as conn:
            for record in records:
                previous = existing.get(record["relpath"])
                was_missing = bool(previous and previous["missing"])
                if was_missing:
                    # A file restored by Finder may retain its exact size and
                    # timestamp. Revive it before the unchanged-file fast path,
                    # without replacing its metadata or edit interpretations.
                    cat.restore_file(conn, previous["id"])
                    updated += 1
                availability = record.get("availability", "local")
                if availability != "local":
                    cloud_only += int(availability == "cloud-only")
                    # Eviction must never erase capture metadata, content identity
                    # or edits. A placeholder has no bytes to fingerprint/relink.
                    if previous:
                        if previous["availability"] != availability:
                            conn.execute("UPDATE files SET availability=? WHERE id=?",
                                         (availability, previous["id"]))
                            if not was_missing:
                                updated += 1
                    else:
                        cat.upsert_file(conn, source_id, record)
                        added += 1
                    continue
                became_local = previous and previous["availability"] != "local"
                if became_local:
                    conn.execute("UPDATE files SET availability='local' WHERE id=?",
                                 (previous["id"],))
                unchanged = previous \
                    and previous["size"] == record["size"] \
                    and previous["mtime_ns"] == record["mtime_ns"]
                metadata_current = not read_metadata_for_new or (
                    previous and previous["metadata_version"] >= METADATA_VERSION)
                if unchanged and metadata_current and not became_local:
                    continue
                if not previous:
                    record["header_hash"] = header_hash(Path(record["path"]))
                    if cat.relink_by_hash(conn, source_id, record) is not None:
                        relinked += 1
                        continue
                    if read_metadata_for_new:
                        record.update(read_metadata(Path(record["path"])))
                    cat.upsert_file(conn, source_id, record)
                    added += 1
                else:
                    record["header_hash"] = (previous["header_hash"]
                                             if unchanged and previous["header_hash"] else
                                             header_hash(Path(record["path"])))
                    if read_metadata_for_new:
                        metadata = read_metadata(Path(record["path"]))
                        # A version-only refresh is opportunistic. If the
                        # metadata binding is unavailable, retain the existing
                        # row and retry on a later scan instead of replacing
                        # useful fields with nulls.
                        if unchanged and not metadata.get("metadata_version"):
                            continue
                        record.update(metadata)
                    cat.upsert_file(conn, source_id, record)
                    if not was_missing:
                        updated += 1

        # Notify only after the rows commit, so a thumbnail worker can resolve
        # qualified names immediately. The callback only queues bounded work;
        # source decoding never runs inside the catalog write transaction.
        if on_local_file:
            for record in records:
                if record.get("availability", "local") == "local" \
                        and record.get("kind") != "video":
                    try:
                        on_local_file(source_id, record["relpath"])
                    except Exception:
                        pass  # disposable warm-up cannot invalidate a scan

    scan_errors: list[str] = []

    def incomplete(message: str) -> None:
        if len(scan_errors) < 10:
            scan_errors.append(message)

    for record in walk_source(root, limit=limit, on_incomplete=incomplete):
        seen.add(record["relpath"])
        batch.append(record)
        if len(batch) >= 200:
            flush(batch)
            batch = []
            if progress:
                progress({"seen": len(seen), "added": added,
                          "updated": updated, "relinked": relinked, "cloudOnly": cloud_only})
    flush(batch)

    # Only a complete traversal can establish that an unvisited photo is gone.
    # Keep indexing readable arrivals during partial scans, but leave existing
    # visibility and the last successful scan timestamp alone.
    missing = 0
    if not scan_errors:
        missing = cat.mark_missing(source_id, seen)
        cat.mark_scanned(source_id)
    result = {"added": added, "updated": updated, "missing": missing,
              "relinked": relinked, "seen": len(seen), "unavailable": False,
              "cloudOnly": cloud_only,
              "complete": not scan_errors}
    if scan_errors:
        result["error"] = "Incomplete scan: " + "; ".join(scan_errors)
    if progress:
        progress(result)
    return result


def scan_all(cat: catalog_module.Catalog, **kwargs) -> dict:
    totals = {"added": 0, "updated": 0, "missing": 0, "relinked": 0,
              "seen": 0, "sources": 0, "complete": True}
    for source in cat.sources():
        result = scan_source(cat, source["id"], **kwargs)
        totals["sources"] += 1
        for key in ("added", "updated", "missing", "relinked", "seen"):
            totals[key] += int(result.get(key, 0) or 0)
        if not result["complete"]:
            totals["complete"] = False
    return totals


def register_file(cat: catalog_module.Catalog, path: Path | str) -> int:
    """Register one settled file under its most specific active source."""
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise ValueError(f"file is not readable: {candidate}")
    sources = []
    for source in cat.sources():
        root = Path(source["path"]).expanduser().resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        sources.append((len(root.parts), int(source["id"]), root, relative))
    if not sources:
        raise ValueError(f"{candidate.parent} is not inside a catalog source")
    _, source_id, _, relative = max(sources, key=lambda item: item[0])
    stat = candidate.stat()
    media_availability.require_local(candidate, stat=stat)
    ext = candidate.suffix.lower()
    if ext not in ALL_EXTS:
        raise ValueError(f"unsupported photo type: {ext or 'none'}")
    record = {
        "relpath": relative.as_posix(), "filename": candidate.name,
        "ext": ext, "kind": kind_for(ext), "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns, "mtime_iso": _iso(stat.st_mtime),
        "header_hash": header_hash(candidate), "path": str(candidate),
    }
    record.update(read_metadata(candidate))
    with cat.write() as conn:
        file_id = cat.upsert_file(conn, source_id, record)
        row = conn.execute(
            "SELECT id FROM images WHERE file_id=? AND copy_ident IS NULL",
            (file_id,)).fetchone()
        if not row:
            raise RuntimeError("catalog did not create an image row")
        return int(row["id"])


# ------------------------------------------------------------- migration

STATE_FILENAME = ".lighttable-state.json"


def import_state_file(cat: catalog_module.Catalog, source_id: int,
                      state_path: Path | None = None) -> dict:
    """Fold a per-folder state file into the catalog.

    The old library wrote every rating, keyword, edit, collection, stack, and
    virtual copy into one JSON file beside the photos. This reads that file and
    reproduces it in the catalog. It is additive and idempotent: running it
    twice does not duplicate anything, and it never deletes the original file,
    so an older build can still open the folder.
    """
    source = cat.source_by_id(source_id)
    if not source:
        raise ValueError(f"unknown source {source_id}")
    path = Path(state_path) if state_path \
        else Path(source["path"]) / STATE_FILENAME
    if not path.is_file():
        return {"images": 0, "collections": 0, "stacks": 0, "virtual": 0,
                "skipped": 0, "present": False}

    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"images": 0, "collections": 0, "stacks": 0, "virtual": 0,
                "skipped": 0, "present": False, "error": "unreadable"}

    images = state.get("images") if isinstance(state, dict) else None
    images = images if isinstance(images, dict) else {}

    # Virtual copies first: their state entries are keyed by the marker name,
    # and the image rows have to exist before that state can be attached.
    created_virtual = 0
    id_by_name: dict[str, int] = {}
    for record in (state.get("virtualCopies") or []):
        if not isinstance(record, dict):
            continue
        base = str(record.get("source", ""))
        ident = str(record.get("id", "")) or None
        if not base or not ident:
            continue
        base_id = cat.image_id_for(source_id, base)
        if base_id is None:
            continue
        name = str(record.get("name", ""))
        existing = cat.image_id_for(source_id, base, ident)
        if existing is None:
            display = str(record.get("displayName", "")) or Path(base).name
            existing = cat.add_virtual_copy(base_id, ident, display)
            created_virtual += 1
        id_by_name[name] = existing

    imported = skipped = 0
    for name, entry in images.items():
        if not isinstance(entry, dict):
            continue
        image_id = id_by_name.get(name)
        if image_id is None:
            relpath, ident = name, None
            if catalog_module.VIRTUAL_MARKER in name:
                relpath, ident = name.split(catalog_module.VIRTUAL_MARKER, 1)
            image_id = cat.image_id_for(source_id, relpath, ident)
        if image_id is None:
            skipped += 1
            continue
        payload = {
            "status": entry.get("status", "pending"),
            "rating": int(entry.get("rating", 0) or 0),
            "label": str(entry.get("label", "none")),
            "params": entry.get("params"),
            "grade": entry.get("grade"),
            "crop": entry.get("crop"),
            "masks": entry.get("masks"),
            "heals": entry.get("heals"),
            "optics": entry.get("optics"),
            "provenance": entry.get("provenance"),
            "keywords": entry.get("keywords") or [],
        }
        cat.save_state(image_id, payload)
        if "captureTimeOverride" in entry:
            try:
                cat.set_capture_override(image_id, entry["captureTimeOverride"])
            except ValueError:
                pass  # Older or malformed portable dates cannot block import.
        versions = entry.get("versions")
        if isinstance(versions, list) and versions:
            cat.save_versions(image_id, versions)
        imported += 1

    collections = 0
    for record in (state.get("collections") or []):
        if not isinstance(record, dict):
            continue
        name = str(record.get("name", "")).strip()
        if not name:
            continue
        known = {c["name"] for c in cat.collections()}
        if name in known:
            continue
        kind = "smart" if record.get("type") == "smart" else "regular"
        rules = record.get("rules") if kind == "smart" else None
        collection_id = cat.add_collection(name, kind=kind, rules=rules)
        collections += 1
        if kind == "regular":
            members = []
            for member in (record.get("members") or []):
                relpath, ident = str(member), None
                if catalog_module.VIRTUAL_MARKER in relpath:
                    relpath, ident = relpath.split(
                        catalog_module.VIRTUAL_MARKER, 1)
                image_id = cat.image_id_for(source_id, relpath, ident)
                if image_id is not None:
                    members.append(image_id)
            if members:
                cat.set_collection_members(collection_id, members)

    stacks = 0
    for record in (state.get("stacks") or []):
        if not isinstance(record, dict):
            continue
        members = []
        for member in (record.get("members") or []):
            relpath, ident = str(member), None
            if catalog_module.VIRTUAL_MARKER in relpath:
                relpath, ident = relpath.split(
                    catalog_module.VIRTUAL_MARKER, 1)
            image_id = cat.image_id_for(source_id, relpath, ident)
            if image_id is not None:
                members.append(image_id)
        if len(members) < 2:
            continue
        with cat.write() as conn:
            already = conn.execute(
                "SELECT stack_id FROM stack_images WHERE image_id=?",
                (members[0],)).fetchone()
            if already:
                continue
            cur = conn.execute(
                "INSERT INTO stacks(name, collapsed) VALUES(?,?)",
                (str(record.get("name", "Stack"))[:80],
                 1 if record.get("collapsed", True) else 0))
            stack_id = int(cur.lastrowid)
            for position, image_id in enumerate(members):
                conn.execute(
                    "INSERT OR IGNORE INTO stack_images(stack_id, image_id,"
                    " position) VALUES(?,?,?)", (stack_id, image_id, position))
        stacks += 1

    return {"images": imported, "collections": collections, "stacks": stacks,
            "virtual": created_virtual, "skipped": skipped, "present": True}


def mirror_state_file(cat: catalog_module.Catalog, source_id: int) -> bool:
    """Best-effort write of the legacy per-folder file.

    Some people want a folder to carry its own edits so it can be copied to
    another machine. This keeps that promise without making it load-bearing: a
    read-only volume or a permission error is not an error the user hears
    about, because the catalog is the real store.
    """
    source = cat.source_by_id(source_id)
    if not source:
        return False
    root = Path(source["path"])
    if not root.is_dir():
        return False
    target = root / STATE_FILENAME
    existing: dict = {}
    try:
        candidate = json.loads(target.read_text())
        if isinstance(candidate, dict):
            existing = candidate
    except (OSError, json.JSONDecodeError):
        pass
    try:
        mirrored_revision = float(existing.get("mirroredRevision", 0) or 0)
    except (TypeError, ValueError):
        mirrored_revision = 0
    mirrored_at = time.time()
    rows = cat.connection.execute(
        "SELECT i.id, i.copy_ident, f.relpath,"
        " COALESCE(s.updated_at, 0) AS updated_at FROM images i"
        " JOIN files f ON f.id=i.file_id"
        " LEFT JOIN image_state s ON s.image_id=i.id"
        " WHERE f.source_id=? AND f.missing=0",
        (source_id,)).fetchall()
    images = dict(existing.get("images") or {}) \
        if isinstance(existing.get("images"), dict) else {}
    present: set[str] = set()
    for row in rows:
        name = row["relpath"]
        if row["copy_ident"]:
            name = f"{name}{catalog_module.VIRTUAL_MARKER}{row['copy_ident']}"
        present.add(name)
        # A row at or before the last completed mirror cannot have changed,
        # including a default row intentionally absent from `images`. Skipping
        # it avoids one state query per untouched catalog item.
        if float(row["updated_at"] or 0) <= mirrored_revision:
            continue
        state = cat.state_for(int(row["id"]))
        is_default = (
            state.get("status") == "pending" and not state.get("rating")
            and state.get("label", "none") == "none"
            and not state.get("keywords") and not state.get("grade")
            and not state.get("params") and not state.get("crop")
            and not state.get("masks") and not state.get("heals")
            and not state.get("optics") and not state.get("versions")
            and not state.get("captureTimeOverride")
        )
        if is_default:
            images.pop(name, None)
        else:
            images[name] = state
    for name in set(images) - present:
        images.pop(name, None)
    # Keep collections, stacks, virtual-copy descriptors, and any future
    # top-level keys written by an older or newer build. The catalog owns the
    # image rows, but mirroring them must never erase unrelated portable data.
    payload = dict(existing)
    payload.update(images=images, mirroredAt=_iso(mirrored_at),
                   mirroredRevision=mirrored_at)
    try:
        durable_io.atomic_write_json(target, payload)
        return True
    except OSError:
        return False


class ScanService:
    """One background scanner thread, shared by every source.

    Scanning yields to interactive rendering the way the local AI index does:
    the catalog is a background concern and must never make a slider feel slow.
    """

    def __init__(self, cat: catalog_module.Catalog,
                 render_busy: Callable[[], bool] | None = None,
                 on_local_file: Callable[[int, str], object] | None = None):
        self.catalog = cat
        self._render_busy = render_busy or (lambda: False)
        self._on_local_file = on_local_file
        self._thread: threading.Thread | None = None
        self._queue: list[int] = []
        self._adopt: set[int] = set()
        self._lock = threading.Lock()
        self._status: dict = {"running": False, "source": None, "last": None,
                              "adopted": None}

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status, queued=len(self._queue))

    def request(self, source_id: int | None = None, *,
                adopt_state_file: bool = False) -> None:
        """Queue a scan. `adopt_state_file` folds in a pre-catalog state file
        once the scan has created the rows it refers to."""
        with self._lock:
            if source_id is None:
                self._queue = [s["id"] for s in self.catalog.sources()]
                if adopt_state_file:
                    self._adopt.update(self._queue)
            elif source_id not in self._queue:
                self._queue.append(source_id)
                if adopt_state_file:
                    self._adopt.add(source_id)
            running = self._thread is not None and self._thread.is_alive()
            if not running:
                self._thread = threading.Thread(
                    target=self._run, name="lighttable-catalog-scan",
                    daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._status["running"] = False
                    self._status["source"] = None
                    self.catalog.close()
                    return
                source_id = self._queue.pop(0)
                self._status.update({"running": True, "source": source_id})
            for _ in range(25):
                if not self._render_busy():
                    break
                time.sleep(0.2)
            try:
                result = scan_source(self.catalog, source_id,
                                     on_local_file=self._on_local_file)
                with self._lock:
                    adopt = source_id in self._adopt
                    self._adopt.discard(source_id)
                if adopt:
                    adopted = import_state_file(self.catalog, source_id)
                    result = dict(result, adopted=adopted)
                    with self._lock:
                        self._status["adopted"] = adopted
            except Exception as error:  # a bad source must not kill the thread
                result = {"error": str(error)}
            with self._lock:
                self._status["last"] = dict(result, sourceId=source_id)
