"""The photo catalog: one SQLite database spanning every source folder.

The library used to be one folder tree chosen at launch, with edit state in a
JSON file written beside the photos. That model could not answer "every
photograph I own, newest first", and it tied a photo's identity to its path, so
a file moved in Finder lost its edits and every cached render.

This module owns the replacement. Three ideas carry the design:

* **Identity is content, not location.** `files.header_hash` is derived from the
  size and first 64 KiB of a file, so a move, a rename, or a touch keeps the
  same row, and a file that reappears anywhere in any source relinks to its
  edits automatically.
* **Queryable fields are columns; edits stay JSON.** Rating, flag, label, and
  capture time are indexed columns because they drive every filter and sort.
  The edit blobs keep their existing shapes so the `clean_*` functions in
  `server.py`, `edits.py`, and `grade.py` remain the single owners of that
  schema.
* **Generated data lives outside the photo folders.** The catalog sits in the
  user's Application Support directory. Nothing here writes beside originals;
  the optional per-folder mirror in `server.py` is the only thing that does,
  and it is best-effort.

The store is deliberately plain SQL. It is opened from HTTP handler threads, so
every connection is per-thread and every write goes through a short transaction.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import sqlite3
import threading
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import durable_io
import platform_paths

SCHEMA_VERSION = 5


class CatalogVersionError(RuntimeError):
    """The catalog is healthy but belongs to a newer LightTable build."""

# How many live rows sharing a content hash are checked on disk before a scan
# concludes it is looking at duplicates rather than a moved file. Without a
# bound, a library holding many identical files makes every new file stat every
# other one.
RELINK_CANDIDATE_LIMIT = 24

# Filters accept these sort fields; anything else falls back to capture time.
SORT_FIELDS = {
    "capture": "julianday(COALESCE(ct.capture_time, f.capture_time, f.mtime_iso))",
    "name": "f.filename COLLATE NOCASE",
    "rating": "s.rating",
    "status": "s.status",
    "label": "s.label",
    "added": "i.created_at",
    "size": "f.size",
}

STATUS_VALUES = ("pending", "approved", "skipped")
LABEL_VALUES = ("none", "red", "yellow", "green", "blue", "purple")
KIND_VALUES = ("all", "raw", "processed", "video", "virtual")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    favorite      INTEGER NOT NULL DEFAULT 0,
    available     INTEGER NOT NULL DEFAULT 1,
    active        INTEGER NOT NULL DEFAULT 1,
    added_at      REAL NOT NULL,
    last_scan_at  REAL,
    bookmark      BLOB
);

CREATE TABLE IF NOT EXISTS folders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    relpath   TEXT NOT NULL,
    name      TEXT NOT NULL,
    UNIQUE (source_id, relpath)
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    folder_id    INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    relpath      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    ext          TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'processed',
    size         INTEGER NOT NULL DEFAULT 0,
    mtime_ns     INTEGER NOT NULL DEFAULT 0,
    mtime_iso    TEXT,
    header_hash  TEXT,
    capture_time TEXT,
    camera_make  TEXT,
    camera_model TEXT,
    lens         TEXT,
    width        INTEGER,
    height       INTEGER,
    orientation  INTEGER,
    metadata_version INTEGER NOT NULL DEFAULT 0,
    availability TEXT NOT NULL DEFAULT 'local',
    missing      INTEGER NOT NULL DEFAULT 0,
    added_at     REAL NOT NULL,
    UNIQUE (source_id, relpath)
);
CREATE INDEX IF NOT EXISTS files_hash ON files(header_hash);
CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS files_capture ON files(capture_time);

CREATE TABLE IF NOT EXISTS capture_overrides (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    capture_time TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    virtual      INTEGER NOT NULL DEFAULT 0,
    copy_ident   TEXT,
    display_name TEXT NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE (file_id, copy_ident)
);
CREATE INDEX IF NOT EXISTS images_file ON images(file_id);

CREATE TABLE IF NOT EXISTS image_state (
    image_id    INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending',
    rating      INTEGER NOT NULL DEFAULT 0,
    label       TEXT NOT NULL DEFAULT 'none',
    params_json TEXT,
    grade_json  TEXT,
    crop_json   TEXT,
    masks_json  TEXT,
    heals_json  TEXT,
    optics_json TEXT,
    provenance_json TEXT,
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS state_rating ON image_state(rating);
CREATE INDEX IF NOT EXISTS state_status ON image_state(status);
CREATE INDEX IF NOT EXISTS state_label ON image_state(label);

CREATE TABLE IF NOT EXISTS keywords (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES keywords(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    path      TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS image_keywords (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, keyword_id)
);

CREATE TABLE IF NOT EXISTS iptc (
    image_id  INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    title     TEXT, caption TEXT, creator TEXT, copyright TEXT,
    credit    TEXT, headline TEXT, city TEXT, state TEXT, country TEXT,
    gps_lat   REAL, gps_lon REAL, gps_alt REAL
);

CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'regular',
    rules_json TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collection_images (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection_id, image_id)
);

CREATE TABLE IF NOT EXISTS stacks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    collapsed INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stack_images (
    stack_id INTEGER NOT NULL REFERENCES stacks(id) ON DELETE CASCADE,
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE
                     UNIQUE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stack_id, image_id)
);

CREATE TABLE IF NOT EXISTS versions (
    id         TEXT PRIMARY KEY,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created    TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_image ON versions(image_id);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    created    REAL NOT NULL,
    label      TEXT NOT NULL,
    origin     TEXT NOT NULL DEFAULT 'edit',
    state_blob BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS history_image ON history(image_id, seq);

CREATE TABLE IF NOT EXISTS rename_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch     TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    old_relpath TEXT NOT NULL,
    new_relpath TEXT NOT NULL,
    created   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rename_batch ON rename_log(batch);

CREATE TABLE IF NOT EXISTS watch_ledger (
    watch_id    TEXT NOT NULL,
    header_hash TEXT NOT NULL,
    handled_at  REAL NOT NULL,
    PRIMARY KEY (watch_id, header_hash)
);

CREATE VIRTUAL TABLE IF NOT EXISTS image_search USING fts5(
    image_id UNINDEXED, filename, keywords, title, caption, camera, lens,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def default_catalog_path() -> Path:
    """The platform's persistent catalog, or LIGHTTABLE_CATALOG_FILE."""
    return platform_paths.catalog_file()


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _json_or(value, fallback=None):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _text_or(value, fallback=""):
    """Return JSON-safe text when a damaged SQLite cell has the wrong type."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return fallback
    if value is None:
        return fallback
    try:
        return str(value)
    except Exception:
        return fallback


def _int_or(value, fallback=0, *, minimum=None, maximum=None) -> int:
    """Coerce an integer cell without letting logical corruption escape."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _optional_nonnegative_int(value) -> int | None:
    if value is None:
        return None
    result = _int_or(value, -1)
    return result if result >= 0 else None


def _finite_number_or(value, fallback=None):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback


def _enum_or(value, choices, fallback: str) -> str:
    value = _text_or(value, fallback)
    return value if value in choices else fallback


def _catalog_file_ok(path: Path | str, *, strict: bool = True) -> bool:
    """Validate a standalone SQLite catalog without changing it.

    ``strict`` also requires clean foreign keys, which every automatic
    backup and restore insists on.  A snapshot taken deliberately *before*
    a repair, or a backup a person has chosen to restore, relaxes that:
    the rows are readable and a repair can finish the job.
    """
    path = Path(path)
    if not path.is_file():
        return False
    connection = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        if strict and connection.execute("PRAGMA foreign_key_check").fetchone():
            return False
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return bool(version) and 1 <= int(version[0]) <= SCHEMA_VERSION
    except (OSError, ValueError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


BACKUP_GLOB = "LightTable-catalog-*.zip"

# Tables in dependency order, so a salvage can insert parents before children
# and a repair can delete orphans after their parents are known to be gone.
_SALVAGE_TABLES = (
    "sources", "folders", "files", "capture_overrides", "images", "image_state", "keywords",
    "image_keywords", "iptc", "collections", "collection_images", "stacks",
    "stack_images", "versions", "history", "rename_log", "watch_ledger",
)

# Rows whose parent no longer exists. Foreign keys with cascades make these
# impossible through the API, so any that exist came from damage.
_ORPHAN_QUERIES = {
    "folders": "FROM folders t LEFT JOIN sources p ON p.id=t.source_id"
               " WHERE p.id IS NULL",
    "files": "FROM files t LEFT JOIN sources p ON p.id=t.source_id"
             " WHERE p.id IS NULL",
    "capture_overrides": "FROM capture_overrides t LEFT JOIN files p ON p.id=t.file_id"
                         " WHERE p.id IS NULL",
    "images": "FROM images t LEFT JOIN files p ON p.id=t.file_id"
              " WHERE p.id IS NULL",
    "image_state": "FROM image_state t LEFT JOIN images p ON p.id=t.image_id"
                   " WHERE p.id IS NULL",
    "image_keywords": "FROM image_keywords t"
                      " LEFT JOIN images p ON p.id=t.image_id"
                      " LEFT JOIN keywords k ON k.id=t.keyword_id"
                      " WHERE p.id IS NULL OR k.id IS NULL",
    "iptc": "FROM iptc t LEFT JOIN images p ON p.id=t.image_id"
            " WHERE p.id IS NULL",
    "collection_images": "FROM collection_images t"
                         " LEFT JOIN images p ON p.id=t.image_id"
                         " LEFT JOIN collections c ON c.id=t.collection_id"
                         " WHERE p.id IS NULL OR c.id IS NULL",
    "stack_images": "FROM stack_images t"
                    " LEFT JOIN images p ON p.id=t.image_id"
                    " LEFT JOIN stacks s ON s.id=t.stack_id"
                    " WHERE p.id IS NULL OR s.id IS NULL",
    "versions": "FROM versions t LEFT JOIN images p ON p.id=t.image_id"
                " WHERE p.id IS NULL",
    "history": "FROM history t LEFT JOIN images p ON p.id=t.image_id"
               " WHERE p.id IS NULL",
}


def _orphan_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: int(conn.execute(f"SELECT COUNT(*) {clause}").fetchone()[0])
            for table, clause in _ORPHAN_QUERIES.items()}


def _delete_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove childless rows, parents first so cascades stay consistent."""
    removed = {}
    for table, clause in _ORPHAN_QUERIES.items():
        rows = [r[0] for r in conn.execute(
            f"SELECT t.rowid {clause}").fetchall()]
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"({','.join('?' * len(chunk))})", chunk)
        removed[table] = len(rows)
    return removed


def _search_index_report(conn: sqlite3.Connection) -> dict:
    """Compare the full-text index with the images it should describe."""
    images = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
    try:
        indexed = int(conn.execute(
            "SELECT COUNT(DISTINCT image_id) FROM image_search").fetchone()[0])
        stale = int(conn.execute(
            "SELECT COUNT(*) FROM image_search"
            " WHERE image_id NOT IN (SELECT id FROM images)").fetchone()[0])
        unindexed = int(conn.execute(
            "SELECT COUNT(*) FROM images"
            " WHERE id NOT IN (SELECT image_id FROM image_search)").fetchone()[0])
    except sqlite3.Error as error:
        return {"ok": False, "images": images, "error": str(error)}
    return {"ok": stale == 0 and unindexed == 0, "images": images,
            "indexed": indexed, "stale": stale, "unindexed": unindexed}


def _rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Recreate the full-text index from the tables it summarises."""
    conn.execute("DELETE FROM image_search")
    rows = conn.execute(
        "SELECT i.id, f.filename, f.camera_make, f.camera_model, f.lens"
        " FROM images i JOIN files f ON f.id=i.file_id").fetchall()
    keywords: dict[int, list[str]] = {}
    for row in conn.execute(
            "SELECT ik.image_id, k.path FROM image_keywords ik"
            " JOIN keywords k ON k.id=ik.keyword_id ORDER BY k.path"):
        keywords.setdefault(int(row[0]), []).append(str(row[1]))
    iptc = {int(row[0]): (row[1], row[2]) for row in conn.execute(
        "SELECT image_id, title, caption FROM iptc")}
    count = 0
    for row in rows:
        image_id = int(row[0])
        title, caption = iptc.get(image_id, (None, None))
        camera = " ".join(filter(None, (row[2], row[3])))
        conn.execute(
            "INSERT INTO image_search(image_id, filename, keywords, title,"
            " caption, camera, lens) VALUES(?,?,?,?,?,?,?)",
            (image_id, row[1] or "", " ".join(keywords.get(image_id, [])),
             title or "", caption or "", camera, row[4] or ""))
        count += 1
    return count


BACKUP_SCOPE = {
    "included": ["Catalog organization", "Photo edits and stored masks", "Edit history and variants", "Source locations"],
    "excluded": ["Original photo files", "External presets and profiles", "Application preferences", "Rebuildable previews and caches"],
}


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _extract_archive(archive: Path, staged: Path) -> bool:
    """Extract the single catalog member of a backup zip to ``staged``.

    Archives are read without trust in their member names.  Returns False for
    anything that is not exactly one plain ``.sqlite3`` file in a zip that
    passes its own CRC check.
    """
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            members = [item for item in bundle.infolist()
                       if not item.is_dir()
                       and Path(item.filename).name == item.filename
                       and item.filename.endswith(".sqlite3")]
            if len(members) != 1 or bundle.testzip() is not None:
                return False
            with bundle.open(members[0], "r") as reader, \
                    staged.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if "manifest.json" in bundle.namelist():
                if bundle.getinfo("manifest.json").file_size > 1024 * 1024:
                    return False
                manifest = json.loads(bundle.read("manifest.json"))
                if (manifest.get("format") != "lighttable-catalog-backup"
                        or manifest.get("version") != 1
                        or manifest.get("catalog", {}).get("sha256") != _file_digest(staged)):
                    return False
        return True
    except (OSError, ValueError, TypeError, AttributeError, zipfile.BadZipFile):
        return False


def verify_backup(archive: Path | str, *, strict=True) -> dict:
    """Read back and reopen a backup in a fresh temporary directory, without caches."""
    archive = Path(archive)
    with tempfile.TemporaryDirectory(prefix="lighttable-verify-backup-") as folder:
        staged = Path(folder) / "library.sqlite3"
        if not _extract_archive(archive, staged) or not _catalog_file_ok(staged, strict=strict):
            raise ValueError("The backup failed archive, checksum, or catalog verification")
        summary = catalog_summary(staged)
    return {"archive": str(archive), "verifiedAt": _now(), "summary": summary,
            "scope": BACKUP_SCOPE}


def _install_verified(path: Path, staged: Path, *, label: str = "") -> Path:
    """Put a verified database at ``path``, quarantining whatever was there.

    The current main database is copied into ``Recovery`` and stays in place
    until the replacement is atomically installed: if power fails during the
    swap, the next launch sees the same damage and retries; it never mistakes
    a missing catalog for a new empty library.  The WAL and shared-memory
    files move with it so the quarantined set is complete.
    """
    recovery_root = path.parent / "Recovery"
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    quarantine = recovery_root / (f"{stamp}-{label}" if label else stamp)
    quarantine.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        if path.exists():
            durable_io.copy_file_no_replace(path, quarantine / path.name)
        for suffix in ("-wal", "-shm"):
            source = Path(str(path) + suffix)
            if source.exists():
                target = quarantine / source.name
                os.replace(source, target)
                moved.append((source, target))
        durable_io.publish_file(staged, path)
    except Exception as install_error:
        rollback_errors = []
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                try:
                    os.replace(target, source)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                "catalog recovery could not restore the original after "
                f"{install_error}: {'; '.join(rollback_errors)}"
            ) from install_error
        raise
    return quarantine


def _backup_archives(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(BACKUP_GLOB),
                  key=lambda item: (item.stat().st_mtime_ns, item.name),
                  reverse=True)


def recover_latest_backup(path: Path | str) -> dict | None:
    """Restore the newest valid backup, preserving every damaged DB file.

    Recovery is intentionally conservative: archives are read without trust in
    their member names, extracted to a sibling staging file, and checked by
    SQLite before the live path moves.  The damaged database, WAL, and shared
    memory files remain together in ``Recovery`` for forensic/manual recovery.
    """
    path = Path(path)
    for archive in _backup_archives(path.parent / "Backups"):
        staged = durable_io.temporary_path(path, "recovery")
        try:
            if not _extract_archive(archive, staged):
                continue
            if not _catalog_file_ok(staged):
                continue
            quarantine = _install_verified(path, staged)
            return {"archive": str(archive), "quarantine": str(quarantine)}
        except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error):
            continue
        finally:
            staged.unlink(missing_ok=True)
    return None


def restore_backup(path: Path | str, archive: Path | str) -> dict:
    """Install one chosen, verified backup; the previous database is kept.

    Unlike :func:`recover_latest_backup` this is a user's explicit decision,
    so a bad archive is an error rather than a reason to try the next one.
    """
    path, archive = Path(path), Path(archive)
    if not archive.is_file():
        raise FileNotFoundError(f"backup not found: {archive}")
    staged = durable_io.temporary_path(path, "restore")
    try:
        if not _extract_archive(archive, staged):
            raise ValueError("the backup archive is unreadable or malformed")
        # A chosen backup may be a before-repair snapshot with the orphans
        # it was taken to preserve; structure and schema are what matter.
        if not _catalog_file_ok(staged, strict=False):
            raise ValueError("the backup failed its integrity check")
        quarantine = _install_verified(path, staged, label="before-restore")
    finally:
        staged.unlink(missing_ok=True)
    return {"archive": str(archive), "quarantine": str(quarantine)}


_SUMMARY_CACHE: dict[tuple[str, int, int], dict | None] = {}
_SUMMARY_LOCK = threading.Lock()


def catalog_summary(database: Path | str) -> dict | None:
    """Row counts from a standalone catalog file, read-only; None if unreadable."""
    database = Path(database)
    connection = None
    try:
        uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)

        def count(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0])
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return {
            "schema": int(version[0]) if version else None,
            "sources": count("SELECT COUNT(*) FROM sources WHERE active=1"),
            "images": count("SELECT COUNT(*) FROM images"),
            "edited": count(
                "SELECT COUNT(*) FROM image_state WHERE params_json IS NOT NULL"
                " OR grade_json IS NOT NULL OR rating>0 OR status!='pending'"
                " OR label!='none'"),
            "history": count("SELECT COUNT(*) FROM history"),
            "collections": count("SELECT COUNT(*) FROM collections"),
            "keywords": count("SELECT COUNT(*) FROM keywords"),
        }
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()


def backup_summary(archive: Path | str) -> dict | None:
    """What a backup holds, so a person can choose between backups."""
    archive = Path(archive)
    try:
        stat = archive.stat()
    except OSError:
        return None
    key = (str(archive), stat.st_size, stat.st_mtime_ns)
    with _SUMMARY_LOCK:
        if key in _SUMMARY_CACHE:
            return _SUMMARY_CACHE[key]
    staged = durable_io.temporary_path(archive.with_suffix(".sqlite3"), "peek")
    try:
        summary = (catalog_summary(staged)
                   if _extract_archive(archive, staged) else None)
    finally:
        staged.unlink(missing_ok=True)
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE[key] = summary
        if len(_SUMMARY_CACHE) > 64:
            _SUMMARY_CACHE.pop(next(iter(_SUMMARY_CACHE)))
    return summary


def list_backups(directory: Path | str, *, summarize: int = 12) -> list[dict]:
    """Newest first. The first ``summarize`` archives carry their contents."""
    items = []
    for index, archive in enumerate(_backup_archives(Path(directory))):
        try:
            stat = archive.stat()
        except OSError:
            continue
        items.append({
            "path": str(archive), "name": archive.name,
            "size": int(stat.st_size), "modified": stat.st_mtime,
            "modifiedIso": _iso(stat.st_mtime),
            "summary": backup_summary(archive) if index < summarize else None,
        })
    return items


def salvage(damaged: Path | str, target: Path | str) -> dict:
    """Copy every readable row of a damaged catalog into a fresh database.

    SQLite reads a corrupt file page by page; rows on intact pages come back
    and the first bad page raises.  Each table is copied until it raises, in
    dependency order, then orphans are dropped and the search index rebuilt
    so the result passes the same checks as any other catalog.  The damaged
    file is never modified.  The report says which tables were cut short, so
    the person choosing between this and a backup knows what each holds.
    """
    damaged, target = Path(damaged), Path(target)
    for suffix in ("", "-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    source = None
    fresh = None
    try:
        try:
            uri = damaged.resolve().as_uri() + "?mode=ro"
            source = sqlite3.connect(uri, uri=True, timeout=5.0)
            source.row_factory = sqlite3.Row
            version = source.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version and int(version[0]) > SCHEMA_VERSION:
                raise CatalogVersionError(
                    f"catalog schema {version[0]} is newer than this build "
                    f"understands ({SCHEMA_VERSION}); update LightTable")
        except CatalogVersionError:
            raise
        except (OSError, ValueError, sqlite3.Error) as error:
            return {"ok": False, "complete": False, "counts": counts,
                    "errors": {"open": str(error)}, "path": str(target)}

        fresh = Catalog(target)
        conn = fresh.connection
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in _SALVAGE_TABLES:
                target_columns = [row[1] for row in conn.execute(
                    f"PRAGMA table_info({table})").fetchall()]
                try:
                    cursor = source.execute(f"SELECT * FROM {table}")
                    shared = [column[0] for column in cursor.description
                              if column[0] in target_columns]
                except sqlite3.Error as error:
                    # Older catalogs legitimately predate this additive table.
                    if table != "capture_overrides" or not version or int(version[0]) >= 5:
                        errors[table] = str(error)
                    counts[table] = 0
                    continue
                if not shared:
                    counts[table] = 0
                    continue
                insert = (f"INSERT OR IGNORE INTO {table} "
                          f"({', '.join(shared)}) VALUES "
                          f"({', '.join('?' * len(shared))})")
                copied = 0
                while True:
                    try:
                        row = cursor.fetchone()
                    except sqlite3.Error as error:
                        errors[table] = str(error)
                        break
                    if row is None:
                        break
                    conn.execute(insert, [row[column] for column in shared])
                    copied += 1
                counts[table] = copied
            try:
                for row in source.execute("SELECT key, value FROM meta"):
                    if row["key"] != "schema_version":
                        conn.execute(
                            "INSERT OR IGNORE INTO meta(key, value) VALUES(?,?)",
                            (row["key"], row["value"]))
            except sqlite3.Error as error:
                errors["meta"] = str(error)
            removed = _delete_orphans(conn)
            counts["searchIndex"] = _rebuild_search_index(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        fresh.close()
        fresh = None
        ok = _catalog_file_ok(target)
        return {"ok": ok, "complete": not errors, "counts": counts,
                "removedOrphans": removed, "errors": errors,
                "path": str(target)}
    finally:
        if fresh is not None:
            fresh.close()
        if source is not None:
            source.close()


def rebuild_catalog(path: Path | str) -> dict:
    """Salvage the live catalog into a fresh file and install the result.

    The damaged database is quarantined, never deleted.  Raises when nothing
    usable could be read so the caller can offer a backup instead.
    """
    path = Path(path)
    staged = durable_io.temporary_path(path, "rebuild")
    try:
        report = salvage(path, staged)
        if not report.get("ok"):
            raise RuntimeError(
                "the damaged catalog could not be salvaged: "
                + "; ".join(f"{k}: {v}" for k, v in report["errors"].items()))
        quarantine = _install_verified(path, staged, label="before-rebuild")
        return dict(report, quarantine=str(quarantine), path=str(path))
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(staged) + suffix).unlink(missing_ok=True)


def create_fresh_catalog(path: Path | str) -> dict:
    """Set the current catalog aside and start an empty one at the same path."""
    path = Path(path)
    staged = durable_io.temporary_path(path, "fresh")
    try:
        fresh = Catalog(staged)
        fresh.close()
        quarantine = _install_verified(path, staged, label="before-reset")
        return {"quarantine": str(quarantine), "path": str(path)}
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(staged) + suffix).unlink(missing_ok=True)


class Catalog:
    """A thread-safe handle on one catalog database.

    Connections are per-thread because the HTTP server answers from a pool of
    handler threads; SQLite objects cannot cross threads safely. Writes take a
    process-wide lock so a busy scan and an interactive edit cannot interleave
    into `database is locked`.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.retired = False
        self._ensure_schema()

    # ------------------------------------------------------------ plumbing

    @property
    def connection(self) -> sqlite3.Connection:
        if self.retired:
            raise RuntimeError(
                "the catalog is unavailable while it is being replaced")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __del__(self) -> None:
        # Python 3.13 warns when a sqlite connection reaches collection while
        # still open. Catalogs created by short-lived tools and tests should
        # release their calling-thread connection even without an explicit
        # close call.
        try:
            self.close()
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        with self._write_lock:
            conn = self.connection
            # Refuse a newer catalog before even idempotent schema work. An old
            # build must never add objects to a database it does not understand.
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if has_meta:
                existing = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if existing and int(existing["value"]) > SCHEMA_VERSION:
                    raise CatalogVersionError(
                        f"catalog schema {existing['value']} is newer than this "
                        f"build understands ({SCHEMA_VERSION}); update LightTable"
                    )
                if existing and int(existing["value"]) < SCHEMA_VERSION:
                    # Back up and migrate before idempotent current-schema SQL
                    # has any chance to add objects to the older database.
                    self._migrate(int(existing["value"]))
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))

    def _migrate(self, from_version: int) -> None:
        """Upgrade an older catalog in place.

        Every migration is additive, backed up first, and committed with its
        version marker in the same transaction. A crash therefore leaves the
        old version or the complete new one, never an ambiguous halfway state.
        """
        if from_version > SCHEMA_VERSION:
            raise CatalogVersionError(
                f"catalog schema {from_version} is newer than this build "
                f"understands ({SCHEMA_VERSION}); update LightTable")
        if from_version >= SCHEMA_VERSION:
            return
        self.backup()
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(sources)").fetchall()}
            if from_version < 2 and "active" not in columns:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if from_version < 3:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS watch_ledger ("
                    " watch_id TEXT NOT NULL, header_hash TEXT NOT NULL,"
                    " handled_at REAL NOT NULL,"
                    " PRIMARY KEY (watch_id, header_hash))"
                )
            if from_version < 4:
                file_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(files)").fetchall()}
                if "metadata_version" not in file_columns:
                    conn.execute(
                        "ALTER TABLE files ADD COLUMN metadata_version "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            if from_version < 5:
                conn.execute("CREATE TABLE IF NOT EXISTS capture_overrides ("
                             "file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,"
                             "capture_time TEXT NOT NULL, updated_at REAL NOT NULL)")
                file_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(files)").fetchall()}
                if "availability" not in file_columns:
                    conn.execute("ALTER TABLE files ADD COLUMN availability "
                                 "TEXT NOT NULL DEFAULT 'local'")
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def integrity_ok(self) -> bool:
        row = self.connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        return self.connection.execute(
            "PRAGMA foreign_key_check").fetchone() is None

    def quick_check_ok(self) -> bool:
        """The fast structural check that belongs on the startup path.

        ``integrity_check`` reads every page and can hold a large catalog
        for tens of seconds before the window can open; ``quick_check``
        catches the same corruption classes that make a database unopenable
        and leaves the exhaustive pass to background maintenance.
        """
        try:
            row = self.connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError:
            return False
        return bool(row) and row[0] == "ok"

    # ---------------------------------------------------------- health

    def verify(self, *, full: bool = True) -> dict:
        """A complete health report that changes nothing.

        Beyond SQLite's own page check this looks for the logical damage a
        crash can leave behind: rows whose parents are gone, a full-text
        index that has drifted from the tables it summarises, files flagged
        missing, and a write-ahead log that has grown without a checkpoint.
        """
        conn = self.connection
        problems: list[str] = []
        pragma = "integrity_check" if full else "quick_check"
        try:
            messages = [str(row[0]) for row in
                        conn.execute(f"PRAGMA {pragma}").fetchall()]
        except sqlite3.DatabaseError as error:
            # A badly truncated file makes the check itself raise.
            messages = [str(error)]
        integrity_ok = messages == ["ok"]
        if not integrity_ok:
            problems.append("SQLite reports structural damage")

        def guarded(compute, fallback):
            try:
                return compute()
            except sqlite3.DatabaseError:
                return fallback
        fk_violations = guarded(
            lambda: len(conn.execute("PRAGMA foreign_key_check").fetchall()), 0)
        if fk_violations:
            problems.append(f"{fk_violations} foreign-key violations")
        orphans = guarded(lambda: _orphan_counts(conn), {})
        orphan_total = sum(orphans.values())
        if orphan_total:
            problems.append(f"{orphan_total} orphaned rows")
        search = guarded(lambda: _search_index_report(conn),
                         {"ok": not integrity_ok and False})
        if not search.get("ok"):
            problems.append("the search index is out of step with the library")
        missing = guarded(lambda: int(conn.execute(
            "SELECT COUNT(*) FROM files f JOIN sources s ON s.id=f.source_id"
            " WHERE f.missing=1 AND s.active=1").fetchone()[0]), 0)
        wal_bytes = 0
        try:
            wal_bytes = Path(str(self.path) + "-wal").stat().st_size
        except OSError:
            pass
        return {
            "checkedAt": _now(), "full": bool(full),
            "ok": integrity_ok and not fk_violations and not orphan_total
                  and bool(search.get("ok")),
            "repairable": integrity_ok and (
                fk_violations > 0 or orphan_total > 0
                or not search.get("ok")),
            "integrity": "ok" if integrity_ok else messages[:10],
            "foreignKeyViolations": fk_violations,
            "orphans": orphans,
            "searchIndex": search,
            "missingFiles": missing,
            "walBytes": int(wal_bytes),
            "problems": problems,
        }

    def repair(self, *, backup_directory: Path | str | None = None) -> dict:
        """Fix the logical damage :meth:`verify` can find, after a backup.

        Structural (page-level) damage is not repairable in place; for that
        the caller salvages into a fresh file.  Everything here is derived or
        orphaned data, so nothing a person made is lost by removing it.
        """
        # The snapshot must accept the very inconsistencies being repaired.
        archive = self.backup(backup_directory, strict=False,
                              label="before-repair")
        with self.write() as conn:
            removed = _delete_orphans(conn)
            reindexed = _rebuild_search_index(conn)
        self.checkpoint()
        try:
            self.connection.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        return {"backup": str(archive), "removedOrphans": removed,
                "reindexed": reindexed, "verify": self.verify(full=False)}

    def rebuild_search_index(self) -> int:
        """Recreate the full-text index; the answer to stale search results."""
        with self.write() as conn:
            return _rebuild_search_index(conn)

    def checkpoint(self, *, truncate: bool = True) -> dict:
        """Fold the write-ahead log back into the main file.

        SQLite only checkpoints on its own when the log passes a page
        threshold and no reader is in the way; a long-running server with a
        steady trickle of reads can leave a log that grows for days and
        doubles the damage a bad shutdown can do.
        """
        mode = "TRUNCATE" if truncate else "PASSIVE"
        try:
            row = self.connection.execute(
                f"PRAGMA wal_checkpoint({mode})").fetchone()
        except sqlite3.Error as error:
            return {"ok": False, "error": str(error)}
        busy, log_pages, checkpointed = (int(v) for v in row) if row else (1, 0, 0)
        return {"ok": busy == 0, "logPages": log_pages,
                "checkpointed": checkpointed}

    def retire(self) -> None:
        """Refuse further writes; used while the file is being replaced."""
        with self._write_lock:
            self.retired = True
            self.close()

    def write(self):
        """Context manager for a short exclusive write transaction."""
        return _WriteTransaction(self)

    # ------------------------------------------------------------- sources

    def add_source(self, path: Path | str, *, display_name: str = "",
                   favorite: bool = False, bookmark: bytes | None = None) -> int:
        resolved = str(Path(path).expanduser().resolve())
        name = display_name or Path(resolved).name or resolved
        with self.write() as conn:
            existing = conn.execute("SELECT id FROM sources WHERE path=?",
                                    (resolved,)).fetchone()
            if existing:
                # Removing a source is a reversible retirement. Re-adding its
                # path revives the same rows, edit state, history, and copies.
                conn.execute(
                    "UPDATE sources SET active=1, available=1 WHERE id=?",
                    (int(existing["id"]),),
                )
                return int(existing["id"])
            cur = conn.execute(
                "INSERT INTO sources(path, display_name, favorite, available,"
                " added_at, bookmark) VALUES(?,?,?,1,?,?)",
                (resolved, name, 1 if favorite else 0, _now(), bookmark))
            return int(cur.lastrowid)

    def remove_source(self, source_id: int) -> None:
        """Hide a source without deleting any catalog state.

        The path's uniqueness makes this reversible: adding the same folder
        later revives these exact image IDs and all dependent rows.
        """
        with self.write() as conn:
            conn.execute(
                "UPDATE sources SET active=0, favorite=0 WHERE id=?",
                (source_id,),
            )

    def set_source_favorite(self, source_id: int, favorite: bool) -> None:
        with self.write() as conn:
            conn.execute("UPDATE sources SET favorite=? WHERE id=? AND active=1",
                         (1 if favorite else 0, source_id))

    def rename_source(self, source_id: int, display_name: str) -> None:
        with self.write() as conn:
            conn.execute("UPDATE sources SET display_name=? WHERE id=? AND active=1",
                         (display_name[:200], source_id))

    def sources(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM files f WHERE f.source_id=s.id"
            "   AND f.missing=0) AS photo_count"
            " FROM sources s WHERE s.active=1"
            " ORDER BY s.favorite DESC, s.display_name COLLATE NOCASE"
        ).fetchall()
        out = []
        for row in rows:
            source_path = _text_or(row["path"])
            display_name = _text_or(row["display_name"])
            if not display_name:
                display_name = Path(source_path).name if source_path else "Unavailable source"
            out.append({
                "id": row["id"], "path": source_path,
                "name": display_name,
                "favorite": bool(row["favorite"]),
                "available": bool(source_path) and Path(source_path).is_dir(),
                "count": _int_or(row["photo_count"], 0, minimum=0),
                "lastScan": _finite_number_or(row["last_scan_at"]),
            })
        return out

    def source_by_id(self, source_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM sources WHERE id=? AND active=1",
                                      (source_id,)).fetchone()
        return dict(row) if row else None

    def mark_scanned(self, source_id: int) -> None:
        with self.write() as conn:
            conn.execute("UPDATE sources SET last_scan_at=? WHERE id=?",
                         (_now(), source_id))

    # --------------------------------------------------------- watch ledger

    def watch_handled(self, watch_id: str, header_hash: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM watch_ledger WHERE watch_id=? AND header_hash=?",
            (str(watch_id), str(header_hash))).fetchone() is not None

    def record_watch_handled(self, watch_id: str, header_hash: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watch_ledger(watch_id, header_hash,"
                " handled_at) VALUES(?,?,?)",
                (str(watch_id), str(header_hash), _now()))

    # --------------------------------------------------------------- files

    def folder_id(self, conn: sqlite3.Connection, source_id: int,
                  relpath: str) -> int | None:
        """Intern a folder row, creating parents as needed."""
        relpath = relpath.strip("/")
        if not relpath:
            relpath = ""
        row = conn.execute(
            "SELECT id FROM folders WHERE source_id=? AND relpath=?",
            (source_id, relpath)).fetchone()
        if row:
            return int(row["id"])
        name = relpath.rsplit("/", 1)[-1] if relpath else ""
        cur = conn.execute(
            "INSERT INTO folders(source_id, relpath, name) VALUES(?,?,?)",
            (source_id, relpath, name))
        return int(cur.lastrowid)

    def upsert_file(self, conn: sqlite3.Connection, source_id: int,
                    record: dict) -> int:
        """Insert or refresh one file row and guarantee its base image row."""
        relpath = record["relpath"]
        parent = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
        folder = self.folder_id(conn, source_id, parent)
        existing = conn.execute(
            "SELECT id FROM files WHERE source_id=? AND relpath=?",
            (source_id, relpath)).fetchone()
        values = (
            folder, record["filename"], record["ext"],
            record.get("kind", "processed"), record.get("size", 0),
            record.get("mtime_ns", 0), record.get("mtime_iso"),
            record.get("header_hash"), record.get("capture_time"),
            record.get("camera_make"), record.get("camera_model"),
            record.get("lens"), record.get("width"), record.get("height"),
            record.get("orientation"), record.get("metadata_version", 0),
            record.get("availability", "local"), 0,
        )
        if existing:
            file_id = int(existing["id"])
            conn.execute(
                "UPDATE files SET folder_id=?, filename=?, ext=?, kind=?,"
                " size=?, mtime_ns=?, mtime_iso=?, header_hash=?,"
                " capture_time=?, camera_make=?, camera_model=?, lens=?,"
                " width=?, height=?, orientation=?, metadata_version=?, availability=?, missing=?"
                " WHERE id=?", (*values, file_id))
        else:
            cur = conn.execute(
                "INSERT INTO files(source_id, folder_id, filename, ext, kind,"
                " size, mtime_ns, mtime_iso, header_hash, capture_time,"
                " camera_make, camera_model, lens, width, height, orientation,"
                " metadata_version, availability, missing, relpath, added_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, *values, relpath, _now()))
            file_id = int(cur.lastrowid)
        self._ensure_image(conn, file_id, record["filename"])
        if existing:
            self._refresh_file_images(conn, file_id, record["filename"])
        return file_id

    def _ensure_image(self, conn: sqlite3.Connection, file_id: int,
                      display_name: str) -> int:
        row = conn.execute(
            "SELECT id FROM images WHERE file_id=? AND copy_ident IS NULL",
            (file_id,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO images(file_id, virtual, copy_ident, display_name,"
            " created_at) VALUES(?,0,NULL,?,?)",
            (file_id, display_name, _now()))
        image_id = int(cur.lastrowid)
        conn.execute("INSERT OR IGNORE INTO image_state(image_id, updated_at)"
                     " VALUES(?,?)", (image_id, _now()))
        # Index the new photo now. Search rows used to appear only once a
        # photo was edited, renamed, or captioned, so every image row without
        # one looked like damage to the health check and, worse, an unedited
        # photo could not be found by its file name.
        self._reindex(conn, image_id)
        return image_id

    def _refresh_file_images(self, conn: sqlite3.Connection, file_id: int,
                             filename: str) -> None:
        """Refresh names and search after a file rename or metadata update."""
        conn.execute(
            "UPDATE images SET display_name=? WHERE file_id=?"
            " AND copy_ident IS NULL", (filename, file_id))
        for image in conn.execute(
                "SELECT id FROM images WHERE file_id=?", (file_id,)).fetchall():
            self._reindex(conn, int(image["id"]))
        # The portable mirror is incremental; a changed path must invalidate
        # its old entry even when the photographic edit itself is unchanged.
        conn.execute(
            "UPDATE image_state SET updated_at=? WHERE image_id IN"
            " (SELECT id FROM images WHERE file_id=?)", (_now(), file_id))

    def restore_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Revive a returned file and its portable state, including copies."""
        conn.execute("UPDATE files SET missing=0 WHERE id=?", (file_id,))
        conn.execute(
            "UPDATE image_state SET updated_at=? WHERE image_id IN"
            " (SELECT id FROM images WHERE file_id=?)", (_now(), file_id))

    def mark_missing(self, source_id: int, keep_relpaths: Iterable[str]) -> int:
        """Flag rows whose files vanished; never delete their edits."""
        keep = set(keep_relpaths)
        with self.write() as conn:
            rows = conn.execute(
                "SELECT id, relpath FROM files WHERE source_id=? AND missing=0",
                (source_id,)).fetchall()
            gone = [int(r["id"]) for r in rows if r["relpath"] not in keep]
            for file_id in gone:
                conn.execute("UPDATE files SET missing=1 WHERE id=?", (file_id,))
            return len(gone)

    def relink_by_hash(self, conn: sqlite3.Connection, source_id: int,
                       record: dict) -> int | None:
        """Reattach a moved or renamed file to the row that holds its edits.

        This is the payoff of hashing content: a folder reorganised in Finder
        keeps every rating, keyword, and edit, and the render caches keyed on
        the same hash stay warm.
        """
        digest = record.get("header_hash")
        if not digest:
            return None
        # Prefer missing rows, but verify both groups against the filesystem.
        # A missing flag may be stale after a restore; an unavailable source
        # does not prove a move. Bound each group so duplicate-heavy libraries
        # cannot turn a scan into an exhaustive filesystem search.
        row = None
        for missing in (1, 0):
            candidates = conn.execute(
                "SELECT f.id, f.relpath, s.path AS source_path"
                " FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.header_hash=? AND f.missing=?"
                " ORDER BY (f.source_id=?) DESC LIMIT ?",
                (digest, missing, source_id, RELINK_CANDIDATE_LIMIT)).fetchall()
            for candidate in candidates:
                old = Path(candidate["source_path"]) / candidate["relpath"]
                try:
                    old.stat()
                except FileNotFoundError:
                    try:
                        if Path(candidate["source_path"]).is_dir():
                            row = candidate
                            break
                    except OSError:
                        pass
                except OSError:
                    # Permission and device errors are not evidence of absence.
                    continue
            if row is not None:
                break
        if not row:
            return None
        relpath = record["relpath"]
        parent = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
        conn.execute(
            "UPDATE files SET source_id=?, folder_id=?, relpath=?, filename=?,"
            " ext=?, size=?, mtime_ns=?, mtime_iso=?, missing=0 WHERE id=?",
            (source_id, self.folder_id(conn, source_id, parent), relpath,
             record["filename"], record["ext"], record.get("size", 0),
             record.get("mtime_ns", 0), record.get("mtime_iso"),
             int(row["id"])))
        self._refresh_file_images(conn, int(row["id"]), record["filename"])
        return int(row["id"])

    def rename_file(self, source_id: int, old_relpath: str, new_relpath: str,
                    *, batch: str = "") -> None:
        """Record a rename: move the row, reindex it, and log the reversal.

        The search index has to be rebuilt here. Updating `files.filename`
        alone would leave the photo findable only under the name it no longer
        has, which is the sort of drift a rename is supposed to avoid.
        """
        self.relocate_files(
            [(source_id, old_relpath, source_id, new_relpath)], batch=batch)

    def relocate_files(
        self,
        moves: Sequence[tuple[int, str, int, str]],
        *,
        batch: str = "",
    ) -> int:
        """Atomically move catalog rows after their files moved on disk.

        One transaction covers the whole batch, so a full disk, conflicting
        row, or reindex failure cannot leave half the selection at old paths.
        """
        normalized = [
            (int(old_source), str(old_relpath), int(new_source), str(new_relpath))
            for old_source, old_relpath, new_source, new_relpath in moves
            if (int(old_source), str(old_relpath))
            != (int(new_source), str(new_relpath))
        ]
        if not normalized:
            return 0
        with self.write() as conn:
            rows = []
            for old_source, old_relpath, new_source, new_relpath in normalized:
                row = conn.execute(
                    "SELECT id FROM files WHERE source_id=? AND relpath=?",
                    (old_source, old_relpath),
                ).fetchone()
                if not row:
                    raise ValueError(f"catalog file is missing: {old_relpath}")
                conflict = conn.execute(
                    "SELECT id FROM files WHERE source_id=? AND relpath=?",
                    (new_source, new_relpath),
                ).fetchone()
                if conflict and int(conflict["id"]) != int(row["id"]):
                    raise ValueError(f"catalog destination exists: {new_relpath}")
                rows.append((int(row["id"]), old_source, old_relpath,
                             new_source, new_relpath))
            for file_id, old_source, old_relpath, new_source, new_relpath in rows:
                filename = new_relpath.rsplit("/", 1)[-1]
                parent = new_relpath.rsplit("/", 1)[0] \
                    if "/" in new_relpath else ""
                conn.execute(
                    "UPDATE files SET source_id=?, relpath=?, filename=?, folder_id=?"
                    " WHERE id=?",
                    (new_source, new_relpath, filename,
                     self.folder_id(conn, new_source, parent), file_id),
                )
                self._refresh_file_images(conn, file_id, filename)
                if batch and old_source == new_source:
                    conn.execute(
                        "INSERT INTO rename_log(batch, source_id, old_relpath,"
                        " new_relpath, created) VALUES(?,?,?,?,?)",
                        (batch, old_source, old_relpath, new_relpath, _now()),
                    )
        return len(normalized)

    def rename_folder(self, source_id: int, old_prefix: str,
                      new_prefix: str, *, batch: str = "") -> int:
        """Move every catalog row under a renamed source subfolder."""
        old_prefix = old_prefix.strip("/")
        new_prefix = new_prefix.strip("/")
        descendant_prefix = old_prefix + "/"
        rows = self.connection.execute(
            "SELECT relpath FROM files WHERE source_id=?"
            " AND (relpath=? OR substr(relpath,1,?)=?)",
            (source_id, old_prefix, len(descendant_prefix), descendant_prefix),
        ).fetchall()
        moves = []
        for row in rows:
            old = str(row["relpath"])
            moves.append((source_id, old, source_id,
                          new_prefix + old[len(old_prefix):]))
        return self.relocate_files(moves, batch=batch)

    def rename_batch(self, batch: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT source_id, old_relpath, new_relpath FROM rename_log"
            " WHERE batch=? ORDER BY id", (batch,)).fetchall()
        return [dict(row) for row in rows]

    def duplicates(self) -> list[dict]:
        """Groups of present files that share a header hash."""
        rows = self.connection.execute(
            "SELECT header_hash, COUNT(*) AS n FROM files"
            " WHERE missing=0 AND header_hash IS NOT NULL"
            " AND source_id IN (SELECT id FROM sources WHERE active=1)"
            " GROUP BY header_hash HAVING n > 1").fetchall()
        out = []
        for row in rows:
            members = self.connection.execute(
                "SELECT f.id, f.relpath, s.path AS source_path, f.size"
                " FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.header_hash=? AND f.missing=0 AND s.active=1",
                (row["header_hash"],)).fetchall()
            out.append({"hash": row["header_hash"],
                        "files": [dict(m) for m in members]})
        return out

    # -------------------------------------------------------------- images

    def image_row(self, image_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT i.*, f.relpath, f.source_id, f.header_hash, f.kind,"
            "       f.camera_make, f.camera_model, f.lens,"
            "       s.path AS source_path"
            " FROM images i JOIN files f ON f.id=i.file_id"
            " JOIN sources s ON s.id=f.source_id WHERE i.id=?",
            (image_id,)).fetchone()
        return dict(row) if row else None

    def image_id_for(self, source_id: int, relpath: str,
                     copy_ident: str | None = None) -> int | None:
        row = self.connection.execute(
            "SELECT i.id FROM images i JOIN files f ON f.id=i.file_id"
            " WHERE f.source_id=? AND f.relpath=?"
            "   AND ((? IS NULL AND i.copy_ident IS NULL) OR i.copy_ident=?)",
            (source_id, relpath, copy_ident, copy_ident)).fetchone()
        return int(row["id"]) if row else None

    def add_virtual_copy(self, image_id: int, copy_ident: str,
                         display_name: str) -> int:
        """A second interpretation of one file, with its own state."""
        with self.write() as conn:
            base = conn.execute("SELECT file_id FROM images WHERE id=?",
                                (image_id,)).fetchone()
            if not base:
                raise ValueError("unknown image")
            cur = conn.execute(
                "INSERT INTO images(file_id, virtual, copy_ident, display_name,"
                " created_at) VALUES(?,1,?,?,?)",
                (int(base["file_id"]), copy_ident, display_name, _now()))
            copy_id = int(cur.lastrowid)
            source = conn.execute(
                "SELECT * FROM image_state WHERE image_id=?",
                (image_id,)).fetchone()
            if source:
                columns = [k for k in source.keys()
                           if k not in ("image_id", "updated_at")]
                conn.execute(
                    f"INSERT INTO image_state(image_id, {', '.join(columns)},"
                    " updated_at)"
                    f" VALUES(?, {', '.join('?' * len(columns))}, ?)",
                    (copy_id, *[source[c] for c in columns], _now()))
            else:
                conn.execute("INSERT INTO image_state(image_id, updated_at)"
                             " VALUES(?,?)", (copy_id, _now()))
            # A virtual interpretation starts with the source's descriptive
            # metadata and saved edit versions, then diverges independently.
            conn.execute(
                "INSERT INTO image_keywords(image_id, keyword_id)"
                " SELECT ?, keyword_id FROM image_keywords WHERE image_id=?",
                (copy_id, image_id))
            fields = ", ".join(self.IPTC_FIELDS)
            conn.execute(
                f"INSERT INTO iptc(image_id, {fields})"
                f" SELECT ?, {fields} FROM iptc WHERE image_id=?",
                (copy_id, image_id))
            for version in conn.execute(
                    "SELECT id, name, created, state_json FROM versions"
                    " WHERE image_id=?", (image_id,)).fetchall():
                conn.execute(
                    "INSERT INTO versions(id, image_id, name, created,"
                    " state_json) VALUES(?,?,?,?,?)",
                    (f"copy-{copy_id}-{version['id']}"[:100], copy_id,
                     version["name"], version["created"],
                     version["state_json"]))
            self._reindex(conn, copy_id)
            return copy_id

    def delete_virtual_copy(self, image_id: int) -> None:
        """Delete one virtual interpretation without touching its source file."""
        with self.write() as conn:
            row = conn.execute(
                "SELECT virtual FROM images WHERE id=?", (image_id,)).fetchone()
            if not row or not row["virtual"]:
                raise ValueError("not a virtual copy")
            conn.execute("DELETE FROM images WHERE id=?", (image_id,))
            conn.execute(
                "DELETE FROM stacks WHERE id IN ("
                " SELECT st.id FROM stacks st LEFT JOIN stack_images si"
                " ON si.stack_id=st.id GROUP BY st.id HAVING COUNT(si.image_id)<2)"
            )

    # --------------------------------------------------------------- state

    def state_for(self, image_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM image_state WHERE image_id=?", (image_id,)).fetchone()
        if not row:
            return {"status": "pending", "rating": 0, "label": "none"}
        out = {
            "status": _enum_or(row["status"], STATUS_VALUES, "pending"),
            "rating": _int_or(row["rating"], 0, minimum=0, maximum=5),
            "label": _enum_or(row["label"], LABEL_VALUES, "none"),
        }
        for key, column in (("params", "params_json"), ("grade", "grade_json"),
                            ("crop", "crop_json"), ("masks", "masks_json"),
                            ("heals", "heals_json"), ("optics", "optics_json"),
                            ("provenance", "provenance_json")):
            raw = row[column]
            out[key] = _json_or(raw)
        out["keywords"] = self.keywords_for(image_id)
        out["versions"] = self.versions_for(image_id)
        capture = self.capture_details(image_id)
        if capture and capture["override"] is not None:
            out["captureTimeOverride"] = capture["override"]
        return out

    def save_state(self, image_id: int, entry: dict) -> None:
        """Persist one image's editing state. Keys absent are left alone."""
        self.save_states({image_id: entry})

    def save_states(self, entries: dict[int, dict]) -> None:
        """Persist a batch, including paired metadata, in one transaction."""
        with self.write() as conn:
            for image_id, entry in entries.items():
                self._save_state(conn, image_id, entry)

    def mutate_masks(self, image_id: int, mutation, *, label: str,
                     batch_id: str = "", name: str = "") -> dict:
        """Merge background mask work into the latest state in one transaction.

        The callback runs after acquiring the writer, never around inference.
        A batch stores only its new mask IDs for a selective, durable Undo.
        """
        with self.write() as conn:
            if not conn.execute("SELECT id FROM images WHERE id=?", (image_id,)).fetchone():
                raise ValueError("The photo is no longer in this catalog")
            before = self.state_for(image_id)
            masks = mutation(before)
            if masks == (before.get("masks") or []):
                return before
            self._add_history(conn, image_id, "Before " + label, before, origin="batch-masks")
            self._save_state(conn, image_id, {"masks": masks})
            after = dict(before, masks=masks)
            self._add_history(conn, image_id, label, after, origin="batch-masks")
            if batch_id:
                # One small row per photo avoids rewriting a growing batch
                # manifest after every detection in a large catalog.
                key = f"mask-batch:{batch_id}:{image_id}"
                old_ids = {mask.get("id") for mask in before.get("masks") or []}
                record = {"created": _now(), "imageId": image_id,
                    "ids": [mask["id"] for mask in masks if mask["id"] not in old_ids]}
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                             (key, json.dumps(record)))
            return after

    def undo_mask_batch(self, batch_id: str) -> list[dict]:
        """Remove this batch's generated masks, preserving subsequent manual edits."""
        pattern = "mask-batch:" + batch_id + ":%"
        with self.write() as conn:
            rows = conn.execute("SELECT value FROM meta WHERE key LIKE ?", (pattern,)).fetchall()
            if not rows:
                raise ValueError("There are no saved masks to undo for this batch")
            changes = []
            for row in rows:
                item = json.loads(row[0])
                image_id = item["imageId"]
                image = self.image_row(image_id)
                if not image:
                    continue
                before = self.state_for(image_id)
                removed = set(item["ids"])
                masks = [mask for mask in before.get("masks") or [] if mask.get("id") not in removed]
                if masks != (before.get("masks") or []):
                    self._save_state(conn, image_id, {"masks": masks})
                    self._add_history(conn, image_id, "Undo generated masks", dict(before, masks=masks),
                                      origin="batch-masks")
                    changes.append({"name": qualified_name(image["source_id"], image["relpath"], image["copy_ident"]),
                                    "masks": masks, "removed": item["ids"]})
            conn.execute("DELETE FROM meta WHERE key LIKE ?", (pattern,))
            return changes

    def _save_state(self, conn, image_id: int, entry: dict) -> None:
        conn.execute("INSERT OR IGNORE INTO image_state(image_id,"
                     " updated_at) VALUES(?,?)", (image_id, _now()))
        assignments, values = [], []
        simple = {"status": "status", "rating": "rating", "label": "label"}
        for key, column in simple.items():
            if key in entry:
                assignments.append(f"{column}=?")
                value = entry[key]
                if key == "status":
                    value = _enum_or(value, STATUS_VALUES, "pending")
                elif key == "rating":
                    value = _int_or(value, 0, minimum=0, maximum=5)
                elif key == "label":
                    value = _enum_or(value, LABEL_VALUES, "none")
                values.append(value)
        blobs = {"params": "params_json", "grade": "grade_json",
                 "crop": "crop_json", "masks": "masks_json",
                 "heals": "heals_json", "optics": "optics_json",
                 "provenance": "provenance_json"}
        for key, column in blobs.items():
            if key in entry:
                assignments.append(f"{column}=?")
                value = entry[key]
                values.append(None if value is None
                              else json.dumps(value, separators=(",", ":")))
        assignments.append("updated_at=?")
        values.append(_now())
        conn.execute(
            f"UPDATE image_state SET {', '.join(assignments)}"
            " WHERE image_id=?", (*values, image_id))
        if "keywords" in entry:
            self._set_keywords(conn, image_id, entry["keywords"] or [])
        if "captureTimeOverride" in entry:
            import capture_time
            value = capture_time.normalized_timestamp(entry["captureTimeOverride"])
            row = conn.execute("SELECT file_id FROM images WHERE id=?", (image_id,)).fetchone()
            if row:
                self._write_capture_override(conn, row["file_id"], value)
        self._reindex(conn, image_id)


    def paired_image_names(self, image_id: int) -> list[str]:
        """Physical RAW/JPEG companions in exactly the same catalog folder.

        Query the catalog, not the loaded UI page. Virtual copies, missing
        originals, retired sources and other folders never join a capture.
        """
        row = self.connection.execute(
            "SELECT i.virtual, f.*, s.active FROM images i"
            " JOIN files f ON f.id=i.file_id JOIN sources s ON s.id=f.source_id"
            " WHERE i.id=?", (image_id,)).fetchone()
        if not row or row["virtual"] or row["missing"] or not row["active"]:
            return []
        raw = row["kind"] == "raw"
        if not raw and Path(row["filename"]).suffix.casefold() not in {".jpg", ".jpeg"}:
            return []
        stem = Path(row["filename"]).stem.casefold()
        candidates = self.connection.execute(
            "SELECT i.id, f.relpath, f.filename, f.kind FROM files f"
            " JOIN images i ON i.file_id=f.id"
            " WHERE f.source_id=? AND f.folder_id IS ?"
            " AND f.missing=0 AND i.virtual=0 AND i.copy_ident IS NULL",
            (row["source_id"], row["folder_id"])).fetchall()
        members = [candidate for candidate in candidates
                   if Path(candidate["relpath"]).parent == Path(row["relpath"]).parent
                   and Path(candidate["filename"]).stem.casefold() == stem
                   and (candidate["kind"] == "raw" or
                        Path(candidate["filename"]).suffix.casefold() in {".jpg", ".jpeg"})]
        # Multiple RAW variants or .jpg + .jpeg are ambiguous: keep independent.
        if len(members) != 2 or sum(member["kind"] == "raw" for member in members) != 1:
            return []
        return [qualified_name(row["source_id"], member["relpath"])
                for member in members if member["id"] != image_id]

    def mark_metadata_for(self, image_id: int) -> dict:
        """Read small mark fields without decoding image-sized mask state."""
        row = self.connection.execute(
            "SELECT status, rating, label FROM image_state WHERE image_id=?",
            (image_id,)).fetchone()
        return {**(dict(row) if row else {"status": "pending", "rating": 0, "label": "none"}),
                "keywords": self.keywords_for(image_id)}

    # ------------------------------------------------------------ keywords

    def _keyword_id(self, conn: sqlite3.Connection, path: str) -> int:
        """Intern a `parent > child` keyword path, creating each level."""
        parts = [p.strip() for p in str(path).replace("|", ">").split(">")]
        parts = [p for p in parts if p][:8]
        if not parts:
            raise ValueError("empty keyword")
        parent_id, full = None, ""
        for part in parts:
            full = f"{full} > {part}" if full else part
            row = conn.execute("SELECT id FROM keywords WHERE path=?",
                               (full,)).fetchone()
            if row:
                parent_id = int(row["id"])
                continue
            cur = conn.execute(
                "INSERT INTO keywords(parent_id, name, path) VALUES(?,?,?)",
                (parent_id, part[:60], full[:400]))
            parent_id = int(cur.lastrowid)
        return int(parent_id)

    def _set_keywords(self, conn: sqlite3.Connection, image_id: int,
                      values: Sequence[str]) -> None:
        conn.execute("DELETE FROM image_keywords WHERE image_id=?", (image_id,))
        seen = set()
        for value in list(values)[:100]:
            text = " ".join(str(value).split()).strip()
            if not text or text.casefold() in seen:
                continue
            seen.add(text.casefold())
            keyword_id = self._keyword_id(conn, text)
            conn.execute("INSERT OR IGNORE INTO image_keywords(image_id,"
                         " keyword_id) VALUES(?,?)", (image_id, keyword_id))

    def keywords_for(self, image_id: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT k.path FROM image_keywords ik"
            " JOIN keywords k ON k.id=ik.keyword_id"
            " WHERE ik.image_id=? ORDER BY k.path", (image_id,)).fetchall()
        return [r["path"] for r in rows]

    def keyword_tree(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT k.id, k.parent_id, k.name, k.path,"
            " (SELECT COUNT(*) FROM image_keywords ik WHERE ik.keyword_id=k.id)"
            "   AS count FROM keywords k ORDER BY k.path").fetchall()
        return [dict(r) for r in rows]

    def rename_keyword(self, keyword_id: int, name: str) -> None:
        with self.write() as conn:
            row = conn.execute("SELECT path FROM keywords WHERE id=?",
                               (keyword_id,)).fetchone()
            if not row:
                return
            old = row["path"]
            head = old.rsplit(" > ", 1)[0] if " > " in old else ""
            clean = " ".join(str(name).split()).strip()[:60] or "Keyword"
            new = f"{head} > {clean}" if head else clean
            descendant_prefix = old + " > "
            affected = conn.execute(
                "SELECT DISTINCT ik.image_id FROM image_keywords ik"
                " JOIN keywords k ON k.id=ik.keyword_id"
                " WHERE k.id=? OR substr(k.path,1,?)=?",
                (keyword_id, len(descendant_prefix), descendant_prefix)
            ).fetchall()
            conn.execute("UPDATE keywords SET name=?, path=? WHERE id=?",
                         (clean, new, keyword_id))
            for child in conn.execute(
                    "SELECT id, path FROM keywords"
                    " WHERE substr(path,1,?)=?",
                    (len(descendant_prefix), descendant_prefix)).fetchall():
                conn.execute("UPDATE keywords SET path=? WHERE id=?",
                             (new + child["path"][len(old):], child["id"]))
            for image in affected:
                self._reindex(conn, int(image["image_id"]))
                conn.execute("UPDATE image_state SET updated_at=? WHERE image_id=?",
                             (_now(), int(image["image_id"])))

    # ------------------------------------------------------- capture clock

    def capture_details(self, image_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT f.id AS fileId, f.capture_time AS original, ct.capture_time AS override"
            " FROM images i JOIN files f ON f.id=i.file_id"
            " LEFT JOIN capture_overrides ct ON ct.file_id=f.id WHERE i.id=?",
            (image_id,)).fetchone()
        return dict(row) if row else None

    def set_capture_override(self, image_id: int, value: str | None) -> None:
        """Import a portable override without making a user history step."""
        import capture_time
        value = capture_time.normalized_timestamp(value)
        info = self.capture_details(image_id)
        if not info:
            raise ValueError("unknown photo")
        with self.write() as conn:
            self._write_capture_override(conn, info["fileId"], value)

    def _write_capture_override(self, conn, file_id: int, value: str | None) -> None:
        if value is None:
            conn.execute("DELETE FROM capture_overrides WHERE file_id=?", (file_id,))
        else:
            conn.execute("INSERT INTO capture_overrides(file_id,capture_time,updated_at) VALUES(?,?,?)"
                         " ON CONFLICT(file_id) DO UPDATE SET capture_time=excluded.capture_time,"
                         " updated_at=excluded.updated_at", (file_id, value, _now()))
        conn.execute("UPDATE image_state SET updated_at=? WHERE image_id IN"
                     " (SELECT id FROM images WHERE file_id=?)", (_now(), file_id))

    def apply_capture_changes(self, changes: list[dict], *, label="Capture time corrected") -> list[str]:
        """Compare-and-set one reviewed batch with reversible history atomically."""
        import capture_time
        import zlib
        if not changes or len(changes) > capture_time.MAX_BATCH * 2:
            raise ValueError("No capture-time changes, or selection is too large")
        normalized, seen = [], set()
        for item in changes:
            file_id = int(item["fileId"])
            if file_id in seen:
                raise ValueError("Duplicate photo in capture-time batch")
            seen.add(file_id)
            normalized.append({**item, "after": capture_time.normalized_timestamp(item["after"])})
        affected = []
        with self.write() as conn:
            for item in normalized:
                row = conn.execute(
                    "SELECT f.capture_time, ct.capture_time AS override FROM files f"
                    " LEFT JOIN capture_overrides ct ON ct.file_id=f.id WHERE f.id=?",
                    (item["fileId"],)).fetchone()
                if (not row or row["override"] != item["beforeOverride"]
                        or row["capture_time"] != item["original"]):
                    raise ValueError("Capture time changed since preview. Preview again before applying.")
            for item in normalized:
                members = conn.execute(
                    "SELECT i.id, i.copy_ident, f.relpath, f.source_id FROM images i"
                    " JOIN files f ON f.id=i.file_id WHERE f.id=?", (item["fileId"],)).fetchall()
                for member in members:
                    for step_label, value in (("Before capture time correction", item["beforeOverride"]),
                                              (label, item["after"])):
                        blob = zlib.compress(json.dumps({"captureTimeOnly": True,
                            "captureTimeOverride": value}).encode(), 6)
                        seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM history WHERE image_id=?",
                                           (member["id"],)).fetchone()[0]
                        conn.execute("INSERT INTO history(image_id,seq,created,label,origin,state_blob)"
                                     " VALUES(?,?,?,?,?,?)", (member["id"], seq, _now(), step_label,
                                                               "capture-time", blob))
                        conn.execute("DELETE FROM history WHERE image_id=? AND seq<=?",
                                     (member["id"], seq - 200))
                    affected.append(qualified_name(member["source_id"], member["relpath"], member["copy_ident"]))
                self._write_capture_override(conn, item["fileId"], item["after"])
        return affected

    # ---------------------------------------------------------------- IPTC

    IPTC_FIELDS = ("title", "caption", "creator", "copyright", "credit",
                   "headline", "city", "state", "country",
                   "gps_lat", "gps_lon", "gps_alt")

    def iptc_for(self, image_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM iptc WHERE image_id=?",
                                      (image_id,)).fetchone()
        if not row:
            return {key: None for key in self.IPTC_FIELDS}
        return {key: row[key] for key in self.IPTC_FIELDS}

    def save_iptc(self, image_id: int, fields: dict) -> None:
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO iptc(image_id) VALUES(?)",
                         (image_id,))
            assignments, values = [], []
            for key in self.IPTC_FIELDS:
                if key in fields:
                    assignments.append(f"{key}=?")
                    value = fields[key]
                    if key.startswith("gps_"):
                        try:
                            value = float(value) if value is not None else None
                        except (TypeError, ValueError):
                            value = None
                    elif value is not None:
                        value = str(value)[:600]
                    values.append(value)
            if assignments:
                conn.execute(f"UPDATE iptc SET {', '.join(assignments)}"
                             " WHERE image_id=?", (*values, image_id))
            self._reindex(conn, image_id)

    # -------------------------------------------------------------- search

    def _reindex(self, conn: sqlite3.Connection, image_id: int) -> None:
        row = conn.execute(
            "SELECT f.filename, f.camera_make, f.camera_model, f.lens"
            " FROM images i JOIN files f ON f.id=i.file_id WHERE i.id=?",
            (image_id,)).fetchone()
        if not row:
            return
        keywords = " ".join(
            r["path"] for r in conn.execute(
                "SELECT k.path FROM image_keywords ik"
                " JOIN keywords k ON k.id=ik.keyword_id WHERE ik.image_id=?",
                (image_id,)).fetchall())
        iptc = conn.execute(
            "SELECT title, caption FROM iptc WHERE image_id=?",
            (image_id,)).fetchone()
        camera = " ".join(filter(None, (row["camera_make"], row["camera_model"])))
        conn.execute("DELETE FROM image_search WHERE image_id=?", (image_id,))
        conn.execute(
            "INSERT INTO image_search(image_id, filename, keywords, title,"
            " caption, camera, lens) VALUES(?,?,?,?,?,?,?)",
            (image_id, row["filename"] or "", keywords,
             (iptc["title"] if iptc else "") or "",
             (iptc["caption"] if iptc else "") or "",
             camera, row["lens"] or ""))

    # --------------------------------------------------------------- query

    def query(self, spec: dict | None = None, *,
              include_state: bool = False) -> dict:
        """The one read path behind the grid, filmstrip, and every filter.

        Filtering and sorting used to happen in the browser over the whole
        library. Doing it here in SQL is what makes a hundred-thousand-image
        catalog scroll, and it removes the second copy of the smart-collection
        rules that had to be kept in step by hand.
        """
        spec = spec if isinstance(spec, dict) else {}
        where = ["f.missing=0", "src.active=1"]
        params: list[Any] = []

        scope = str(spec.get("scope", "all"))
        if scope == "source" and spec.get("sourceId"):
            where.append("f.source_id=?")
            params.append(int(spec["sourceId"]))
        elif scope == "folder" and spec.get("folderId"):
            if spec.get("includeSubfolders", True):
                row = self.connection.execute(
                    "SELECT source_id, relpath FROM folders WHERE id=?",
                    (int(spec["folderId"]),)).fetchone()
                if row:
                    prefix = row["relpath"]
                    if prefix:
                        descendant_prefix = prefix + "/"
                        where.append(
                            "f.source_id=? AND substr(f.relpath,1,?)=?")
                        params.extend([
                            row["source_id"], len(descendant_prefix),
                            descendant_prefix,
                        ])
                    else:
                        where.append("f.source_id=?")
                        params.append(row["source_id"])
                else:
                    # A stale folder selection must never broaden a batch
                    # query to all photographs.
                    where.append("0")
            else:
                where.append("f.folder_id=?")
                params.append(int(spec["folderId"]))
        elif scope == "collection" and spec.get("collectionId"):
            collection = self.collection(int(spec["collectionId"]))
            if collection and collection["type"] == "smart":
                merged = dict(spec.get("filter") or {})
                merged.update(collection.get("rules") or {})
                spec = dict(spec, filter=merged)
            else:
                where.append(
                    "i.id IN (SELECT image_id FROM collection_images"
                    " WHERE collection_id=?)")
                params.append(int(spec["collectionId"]))

        flt = spec.get("filter") if isinstance(spec.get("filter"), dict) else {}
        status = str(flt.get("status", "all"))
        if status in STATUS_VALUES:
            where.append("s.status=?")
            params.append(status)
        try:
            rating_min = int(flt.get("ratingMin", 0) or 0)
        except (TypeError, ValueError):
            rating_min = 0
        if rating_min > 0:
            where.append("s.rating>=?")
            params.append(rating_min)
        label = str(flt.get("label", "all"))
        if label in LABEL_VALUES:
            where.append("s.label=?")
            params.append(label)
        elif label == "any":
            where.append("s.label!='none'")
        kind = str(flt.get("kind", "all"))
        if kind == "raw":
            where.append("f.kind='raw'")
        elif kind == "processed":
            where.append("f.kind='processed'")
        elif kind == "video":
            where.append("f.kind='video'")
        elif kind == "virtual":
            where.append("i.virtual=1")
        # Saved toolbar filters use the same OR-within-types / AND-between-
        # fields semantics as the browser, including virtual-copy source types.
        file_types = flt.get("fileTypes")
        if isinstance(file_types, list):
            type_clauses = []
            groups = {"jpeg": (".jpg", ".jpeg", ".jpe"),
                      "heic": (".heic", ".heif", ".hif"),
                      "tiff": (".tif", ".tiff"), "png": (".png",)}
            if "raw" in file_types:
                type_clauses.append("f.kind='raw'")
            for name, suffixes in groups.items():
                if name in file_types:
                    placeholders = ",".join("?" for _ in suffixes)
                    type_clauses.append(f"(f.kind='processed' AND f.ext IN ({placeholders}))")
                    params.extend(suffixes)
            if type_clauses:
                where.append("(" + " OR ".join(type_clauses) + ")")
        edit_state = flt.get("editState")
        if edit_state in ("edited", "unedited"):
            edited = "(" + " OR ".join(
                f"s.{field}_json IS NOT NULL" for field in
                ("params", "grade", "crop", "masks", "heals", "optics")) + ")"
            where.append(edited if edit_state == "edited" else "NOT " + edited)
        elif edit_state == "virtual":
            where.append("i.virtual=1")
        if flt.get("unrated") is True:
            where.append("COALESCE(s.rating,0)=0")
        raw_excluded_kinds = spec.get("excludeKinds", [])
        if not isinstance(raw_excluded_kinds, (list, tuple, set)):
            raw_excluded_kinds = []
        excluded_kinds = {
            str(value) for value in raw_excluded_kinds
            if str(value) in {"raw", "processed", "video"}
        }
        if excluded_kinds:
            placeholders = ",".join("?" for _ in excluded_kinds)
            where.append(f"f.kind NOT IN ({placeholders})")
            params.extend(sorted(excluded_kinds))
        if flt.get("camera"):
            where.append("(f.camera_model LIKE ? OR f.camera_make LIKE ?)")
            params.extend([f"%{flt['camera']}%", f"%{flt['camera']}%"])
        if flt.get("lens"):
            where.append("f.lens LIKE ?")
            params.append(f"%{flt['lens']}%")
        if flt.get("keyword"):
            keyword = str(flt["keyword"])
            descendant_prefix = keyword + " > "
            where.append(
                "i.id IN (SELECT ik.image_id FROM image_keywords ik"
                " JOIN keywords k ON k.id=ik.keyword_id"
                " WHERE k.path=? OR substr(k.path,1,?)=?)")
            params.extend([
                keyword, len(descendant_prefix), descendant_prefix,
            ])
        date_from, date_to = flt.get("dateFrom"), flt.get("dateTo")
        if date_from:
            where.append("COALESCE(ct.capture_time, f.capture_time, f.mtime_iso) >= ?")
            params.append(str(date_from))
        if date_to:
            bound = str(date_to)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", bound):
                # A bare date bound is inclusive: capture times carry a time
                # of day, so comparing the full text excluded everything shot
                # after midnight on the last day.
                where.append(
                    "substr(COALESCE(ct.capture_time, f.capture_time, f.mtime_iso), 1, 10) <= ?")
            else:
                where.append("COALESCE(ct.capture_time, f.capture_time, f.mtime_iso) <= ?")
            params.append(bound)
        query_text = " ".join(str(flt.get("query", "")).split())
        if query_text:
            where.append(
                "i.id IN (SELECT image_id FROM image_search"
                " WHERE image_search MATCH ?)")
            params.append(_fts_query(query_text))

        sort = spec.get("sort") if isinstance(spec.get("sort"), dict) else {}
        field = SORT_FIELDS.get(str(sort.get("field", "capture")),
                                SORT_FIELDS["capture"])
        direction = "DESC" if str(sort.get("dir", "asc")).lower() == "desc" \
            else "ASC"
        try:
            limit = max(1, min(5000, int(spec.get("limit", 500))))
        except (TypeError, ValueError):
            limit = 500
        try:
            offset = max(0, int(spec.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        clause = " AND ".join(where)
        base = (" FROM images i"
                " JOIN files f ON f.id=i.file_id"
                " JOIN sources src ON src.id=f.source_id"
                " LEFT JOIN image_state s ON s.image_id=i.id"
                " LEFT JOIN capture_overrides ct ON ct.file_id=f.id"
                f" WHERE {clause}")
        total = self.connection.execute(
            f"SELECT COUNT(*) AS n{base}", params).fetchone()["n"]
        # The edit blobs are pulled in the same statement when asked for.
        # Fetching them per image turned one page of the grid into hundreds of
        # round trips, which is what made a large library feel unopenable.
        blobs = (", s.params_json, s.grade_json, s.crop_json, s.masks_json,"
                 " s.heals_json, s.optics_json, s.provenance_json"
                 if include_state else "")
        rows = self.connection.execute(
            "SELECT i.id, i.virtual, i.copy_ident, i.display_name,"
            " f.id AS file_id, f.relpath, f.filename, f.ext, f.kind, f.size,"
            " f.mtime_ns, COALESCE(ct.capture_time, f.capture_time) AS capture_time,"
            " f.mtime_iso, f.header_hash,"
            " f.camera_make, f.camera_model, f.lens, f.width, f.height,"
            " f.orientation, f.availability, f.source_id, src.path AS source_path,"
            " COALESCE(s.status,'pending') AS status,"
            " COALESCE(s.rating,0) AS rating,"
            " COALESCE(s.label,'none') AS label,"
            " (s.grade_json IS NOT NULL OR s.params_json IS NOT NULL"
            "  OR s.crop_json IS NOT NULL OR s.masks_json IS NOT NULL"
            "  OR s.heals_json IS NOT NULL OR s.optics_json IS NOT NULL)"
            " AS has_edits"
            f"{blobs}"
            f"{base} ORDER BY {field} {direction}, f.filename COLLATE NOCASE, i.id"
            " LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        items = [_item(row) for row in rows]
        if include_state:
            keywords = self._keywords_for_many([row["id"] for row in rows])
            for item, row in zip(items, rows):
                for key, column in (("params", "params_json"),
                                    ("grade", "grade_json"),
                                    ("crop", "crop_json"),
                                    ("masks", "masks_json"),
                                    ("heals", "heals_json"),
                                    ("optics", "optics_json"),
                                    ("provenance", "provenance_json")):
                    raw = row[column]
                    item[key] = _json_or(raw)
                item["keywords"] = keywords.get(row["id"], [])
        return {"total": int(total), "offset": offset, "limit": limit,
                "items": items}

    def _keywords_for_many(self, image_ids: Sequence[int]) -> dict[int, list]:
        """Keywords for a page of images in one statement."""
        if not image_ids:
            return {}
        out: dict[int, list] = {}
        for chunk_start in range(0, len(image_ids), 500):
            chunk = list(image_ids[chunk_start:chunk_start + 500])
            placeholders = ",".join("?" * len(chunk))
            rows = self.connection.execute(
                "SELECT ik.image_id, k.path FROM image_keywords ik"
                " JOIN keywords k ON k.id=ik.keyword_id"
                f" WHERE ik.image_id IN ({placeholders}) ORDER BY k.path",
                chunk).fetchall()
            for row in rows:
                out.setdefault(int(row["image_id"]), []).append(row["path"])
        return out

    # ---------------------------------------------------------- collections

    def collection(self, collection_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM collections WHERE id=?",
                                      (collection_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "type": row["type"],
                "parentId": row["parent_id"],
                "rules": _json_or(row["rules_json"])}

    def collections(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM collection_images ci"
            "   WHERE ci.collection_id=c.id) AS count"
            " FROM collections c ORDER BY c.sort_order, c.name COLLATE NOCASE"
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "type": r["type"],
                 "parentId": r["parent_id"], "count": r["count"],
                 "rules": _json_or(r["rules_json"])} for r in rows]

    def add_collection(self, name: str, *, kind: str = "regular",
                       rules: dict | None = None,
                       parent_id: int | None = None) -> int:
        with self.write() as conn:
            cur = conn.execute(
                "INSERT INTO collections(parent_id, name, type, rules_json)"
                " VALUES(?,?,?,?)",
                (parent_id, " ".join(str(name).split())[:120] or "Collection",
                 "smart" if kind == "smart" else "regular",
                 json.dumps(rules) if rules else None))
            return int(cur.lastrowid)

    def delete_collection(self, collection_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))

    def set_collection_members(self, collection_id: int,
                               image_ids: Sequence[int]) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM collection_images WHERE collection_id=?",
                         (collection_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO collection_images(collection_id,"
                " image_id, position) VALUES(?,?,?)",
                [(collection_id, int(i), n)
                 for n, i in enumerate(image_ids[:10000])])

    def add_to_collection(self, collection_id: int,
                          image_ids: Sequence[int]) -> None:
        with self.write() as conn:
            start = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS n"
                " FROM collection_images WHERE collection_id=?",
                (collection_id,)).fetchone()["n"]
            conn.executemany(
                "INSERT OR IGNORE INTO collection_images(collection_id,"
                " image_id, position) VALUES(?,?,?)",
                [(collection_id, int(i), start + n)
                 for n, i in enumerate(image_ids)])

    # --------------------------------------------------------------- stacks

    def stack_id_for(self, image_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT stack_id FROM stack_images WHERE image_id=?",
            (int(image_id),)).fetchone()
        return int(row["stack_id"]) if row else None

    def add_to_stack(self, stack_id: int, image_id: int) -> None:
        with self.write() as conn:
            occupied = conn.execute(
                "SELECT stack_id FROM stack_images WHERE image_id=?",
                (int(image_id),)).fetchone()
            if occupied:
                if int(occupied["stack_id"]) == int(stack_id):
                    return
                raise ValueError("a photo can belong to only one stack")
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS n"
                " FROM stack_images WHERE stack_id=?", (int(stack_id),)
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO stack_images(stack_id, image_id, position)"
                " VALUES(?,?,?)", (int(stack_id), int(image_id), position))

    def copy_organizational_state(self, source_id: int,
                                  destination_id: int) -> None:
        """Copy culling and descriptive state, never baked develop edits."""
        with self.write() as conn:
            source = conn.execute(
                "SELECT status, rating, label FROM image_state WHERE image_id=?",
                (int(source_id),)).fetchone()
            if not source:
                raise ValueError("unknown source image")
            conn.execute(
                "INSERT OR IGNORE INTO image_state(image_id, updated_at) VALUES(?,?)",
                (int(destination_id), _now()))
            conn.execute(
                "UPDATE image_state SET status=?, rating=?, label=?, updated_at=?"
                " WHERE image_id=?",
                (source["status"], source["rating"], source["label"], _now(),
                 int(destination_id)))
            conn.execute("DELETE FROM image_keywords WHERE image_id=?",
                         (int(destination_id),))
            conn.execute(
                "INSERT INTO image_keywords(image_id, keyword_id)"
                " SELECT ?, keyword_id FROM image_keywords WHERE image_id=?",
                (int(destination_id), int(source_id)))
            conn.execute("DELETE FROM iptc WHERE image_id=?",
                         (int(destination_id),))
            fields = ", ".join(self.IPTC_FIELDS)
            conn.execute(
                f"INSERT INTO iptc(image_id, {fields})"
                f" SELECT ?, {fields} FROM iptc WHERE image_id=?",
                (int(destination_id), int(source_id)))
            self._reindex(conn, int(destination_id))

    def add_stack(self, name: str, image_ids: Sequence[int], *,
                  collapsed: bool = True) -> int:
        members = list(dict.fromkeys(int(value) for value in image_ids))[:1000]
        if len(members) < 2:
            raise ValueError("a stack needs at least two photos")
        with self.write() as conn:
            occupied = conn.execute(
                "SELECT image_id FROM stack_images WHERE image_id IN ("
                + ",".join("?" * len(members)) + ")", members).fetchall()
            if occupied:
                raise ValueError("a photo can belong to only one stack")
            cur = conn.execute(
                "INSERT INTO stacks(name, collapsed) VALUES(?,?)",
                (" ".join(str(name).split())[:80] or "Photo stack",
                 1 if collapsed else 0))
            stack_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO stack_images(stack_id, image_id, position)"
                " VALUES(?,?,?)",
                [(stack_id, image_id, position)
                 for position, image_id in enumerate(members)])
            return stack_id

    def toggle_stack(self, stack_id: int) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE stacks SET collapsed=CASE collapsed WHEN 0 THEN 1"
                " ELSE 0 END WHERE id=?", (stack_id,))

    def delete_stack(self, stack_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM stacks WHERE id=?", (stack_id,))

    # ------------------------------------------------------------- versions

    def versions_for(self, image_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id, name, created, state_json FROM versions"
            " WHERE image_id=? ORDER BY created", (image_id,)).fetchall()
        out = []
        for row in rows:
            entry = _json_or(row["state_json"])
            if not isinstance(entry, dict):
                continue
            entry.update({"id": row["id"], "name": row["name"],
                          "created": row["created"]})
            out.append(entry)
        return out

    def save_versions(self, image_id: int, versions: Sequence[dict]) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM versions WHERE image_id=?", (image_id,))
            for version in list(versions)[:50]:
                payload = {k: v for k, v in version.items()
                           if k not in ("id", "name", "created")}
                conn.execute(
                    "INSERT OR REPLACE INTO versions(id, image_id, name,"
                    " created, state_json) VALUES(?,?,?,?,?)",
                    (str(version.get("id", ""))[:100], image_id,
                     str(version.get("name", ""))[:80],
                     str(version.get("created", "")), json.dumps(payload)))
            conn.execute("UPDATE image_state SET updated_at=? WHERE image_id=?",
                         (_now(), image_id))

    # -------------------------------------------------------------- history

    def add_history(self, image_id: int, label: str, state: dict,
                    *, origin: str = "edit", cap: int = 200) -> int:
        """Append one persistent editing step.

        Snapshots are compressed because a step holds the whole edit record,
        including mask geometry, and a long session produces hundreds.
        """
        with self.write() as conn:
            return self._add_history(conn, image_id, label, state, origin=origin, cap=cap)

    def _add_history(self, conn, image_id, label, state, *, origin="edit", cap=200):
        import zlib
        blob = zlib.compress(json.dumps(state, separators=(",", ":")).encode("utf-8"), 6)
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM history"
            " WHERE image_id=?", (image_id,)).fetchone()["n"]
        conn.execute(
            "INSERT INTO history(image_id, seq, created, label, origin,"
            " state_blob) VALUES(?,?,?,?,?,?)",
            (image_id, seq, _now(), str(label)[:80], str(origin)[:20],
             blob))
        conn.execute(
            "DELETE FROM history WHERE image_id=? AND seq <= ?",
            (image_id, seq - cap))
        return int(seq)

    def history_for(self, image_id: int, *, limit: int = 200) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id, seq, created, label, origin FROM history"
            " WHERE image_id=? ORDER BY seq DESC LIMIT ?",
            (image_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def history_state(self, history_id: int) -> dict | None:
        import zlib

        row = self.connection.execute(
            "SELECT state_blob FROM history WHERE id=?", (history_id,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(zlib.decompress(row["state_blob"]).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, UnicodeDecodeError, zlib.error):
            return None

    def truncate_history_after(self, image_id: int, seq: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM history WHERE image_id=? AND seq>?",
                         (image_id, seq))

    def clear_history(self, image_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM history WHERE image_id=?", (image_id,))

    # --------------------------------------------------------------- backup

    def backup(self, directory: Path | str | None = None, *,
               strict: bool = True, label: str = "") -> Path:
        """A dated zip of the catalog, taken through SQLite's own backup API.

        Copying the file directly would race the write-ahead log; this is
        consistent even while the app is running.  ``label`` names the
        occasion in the file name (for instance ``before-repair``).
        """
        target_dir = Path(directory) if directory else self.path.parent / "Backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        # Two backups taken in quick succession must not overwrite each
        # other. The final no-replace publish also closes the race between two
        # processes that both observed the same apparently-free name.
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")[:-3]
        suffix = f"-{label}" if label else ""
        base = target_dir / f"LightTable-catalog-{stamp}{suffix}.zip"
        archive = base
        staged = durable_io.temporary_path(
            target_dir / f"library-{archive.stem}.sqlite3", "snapshot")
        partial = durable_io.temporary_path(archive, "archive")
        destination = None
        try:
            destination = sqlite3.connect(staged)
            self.connection.backup(destination)
            destination.close()
            destination = None
            if not _catalog_file_ok(staged, strict=strict):
                raise RuntimeError("catalog backup failed its integrity check")
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(staged, "library.sqlite3")
                bundle.writestr("manifest.json", json.dumps({
                    "format": "lighttable-catalog-backup", "version": 1,
                    "created": _now(), "scope": BACKUP_SCOPE,
                    "catalog": {"file": "library.sqlite3", "sha256": _file_digest(staged),
                                "bytes": staged.stat().st_size, "summary": catalog_summary(staged)},
                }, indent=2))
            with zipfile.ZipFile(partial, "r") as bundle:
                if bundle.testzip() is not None:
                    raise RuntimeError("catalog backup archive failed verification")
            suffix = 2
            while True:
                try:
                    durable_io.publish_file_no_replace(partial, archive)
                    try:
                        verification = verify_backup(archive, strict=strict)
                    except Exception:
                        # Keep recovery evidence but exclude a failed readback
                        # from the list of usable backup archives.
                        try:
                            archive.rename(archive.with_suffix('.zip.unverified'))
                        except OSError:
                            pass
                        raise
                    with self.write() as conn:
                        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                                     ("lastVerifiedBackup", json.dumps(verification)))
                    return archive
                except FileExistsError:
                    archive = target_dir / f"{base.stem}-{suffix}.zip"
                    suffix += 1
        finally:
            if destination is not None:
                destination.close()
            staged.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)

    def backup_if_due(self, *, max_age: float = 24 * 60 * 60,
                      directory: Path | str | None = None) -> Path | None:
        """Take at most one automatic verified backup in ``max_age`` seconds."""
        target_dir = (Path(directory) if directory is not None
                      else self.path.parent / "Backups")
        try:
            latest = max(
                target_dir.glob("LightTable-catalog-*.zip"),
                key=lambda item: item.stat().st_mtime,
                default=None,
            )
            if latest is not None and _now() - latest.stat().st_mtime < max_age:
                return None
            return self.backup(target_dir)
        except OSError:
            return None

    def prune_backups(self, directory: Path | str | None = None,
                      keep: int = 8, *, daily: int = 7, weekly: int = 5,
                      monthly: int = 6) -> int:
        """Thin old backups while keeping a history that reaches back.

        The newest ``keep`` archives always stay.  Beyond those, one per
        day is kept for ``daily`` days, one per week for ``weekly`` weeks,
        and one per month for ``monthly`` months.  Corruption that goes
        unnoticed for a while therefore still has a clean ancestor to
        return to, which eight consecutive daily snapshots could not offer.
        """
        target_dir = Path(directory) if directory else self.path.parent / "Backups"
        if not target_dir.is_dir():
            return 0
        # The name carries the timestamp, so it breaks ties that `st_mtime`
        # alone leaves ambiguous when several backups land in the same
        # millisecond.
        archives = sorted(target_dir.glob(BACKUP_GLOB),
                          key=lambda p: (p.stat().st_mtime, p.name),
                          reverse=True)
        keep = max(1, int(keep))
        retained = set(archives[:keep])
        now = datetime.now()
        buckets: dict[tuple[str, str], Path] = {}
        # Buckets span every archive, so a period whose newest backup is
        # already among the kept ones does not retain a second copy.
        for path in archives:
            when = datetime.fromtimestamp(path.stat().st_mtime)
            age_days = (now - when).total_seconds() / 86400
            tiers = []
            if age_days <= daily:
                tiers.append(("day", when.strftime("%Y-%m-%d")))
            if age_days <= weekly * 7:
                tiers.append(("week", when.strftime("%G-W%V")))
            if age_days <= monthly * 31:
                tiers.append(("month", when.strftime("%Y-%m")))
            for tier in tiers:
                # Archives are newest-first, so the first one seen in a
                # bucket is the one to keep.
                buckets.setdefault(tier, path)
        retained.update(buckets.values())
        removed = 0
        for path in archives:
            if path in retained:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def stats(self) -> dict:
        conn = self.connection
        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])
        return {
            "sources": count("SELECT COUNT(*) FROM sources WHERE active=1"),
            "retiredSources": count(
                "SELECT COUNT(*) FROM sources WHERE active=0"),
            "files": count(
                "SELECT COUNT(*) FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.missing=0 AND s.active=1"),
            "missing": count(
                "SELECT COUNT(*) FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.missing=1 AND s.active=1"),
            "images": count(
                "SELECT COUNT(*) FROM images i JOIN files f ON f.id=i.file_id"
                " JOIN sources s ON s.id=f.source_id WHERE s.active=1"),
            "keywords": count("SELECT COUNT(*) FROM keywords"),
            "collections": count("SELECT COUNT(*) FROM collections"),
            "schema": SCHEMA_VERSION,
            "path": str(self.path),
        }


class _WriteTransaction:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def __enter__(self) -> sqlite3.Connection:
        self.catalog._write_lock.acquire()
        try:
            if getattr(self.catalog, "retired", False) is True:
                raise RuntimeError(
                    "the catalog is unavailable while it is being replaced")
            self.connection = self.catalog.connection
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection
        except Exception:
            self.catalog._write_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        conn = self.connection
        try:
            if exc_type is None:
                try:
                    conn.execute("COMMIT")
                except Exception:
                    # A disk-full or I/O error at commit must not leave this
                    # per-thread connection holding an open write transaction.
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        self.catalog.close()
                    raise
            else:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    # The original operation error remains the useful one;
                    # discard a connection that could still hold a transaction.
                    self.catalog.close()
        finally:
            self.catalog._write_lock.release()
        return False


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 prefix query.

    Quoting each term keeps punctuation in filenames from being read as FTS
    operators, which would otherwise raise on input as ordinary as `f/2.8`.
    """
    terms = []
    for raw in text.split():
        cleaned = raw.replace('"', " ").strip()
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " AND ".join(terms) if terms else '""'


def source_revision(header_hash: str, size: int, mtime_ns: int) -> str:
    """The same revision for scanned rows and a freshly inspected source."""
    identity = f"{header_hash}\0{size}\0{mtime_ns}"
    return hashlib.md5(identity.encode()).hexdigest()


def _item(row: sqlite3.Row) -> dict:
    """The lean per-image record the grid needs; edits are fetched on open."""
    source_id = _int_or(row["source_id"], 0, minimum=0)
    relpath = _text_or(row["relpath"])
    copy_ident = _text_or(row["copy_ident"], None)
    kind = _enum_or(row["kind"], KIND_VALUES, "processed")
    mtime_ns = _int_or(row["mtime_ns"], 0, minimum=0)
    camera_make = _text_or(row["camera_make"])
    camera_model = _text_or(row["camera_model"])
    width = _optional_nonnegative_int(row["width"])
    height = _optional_nonnegative_int(row["height"])
    orientation = _optional_nonnegative_int(row["orientation"])
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    return {
        "id": row["id"],
        "name": qualified_name(source_id, relpath, copy_ident),
        "sourceId": source_id,
        "sourcePath": _text_or(row["source_path"]),
        "relpath": relpath,
        "displayName": _text_or(row["display_name"]),
        "filename": _text_or(row["filename"]),
        "folder": relpath.rsplit("/", 1)[0] if "/" in relpath else "",
        "raw": kind == "raw",
        "kind": kind,
        "virtual": bool(row["virtual"]),
        "status": _enum_or(row["status"], STATUS_VALUES, "pending"),
        "rating": _int_or(row["rating"], 0, minimum=0, maximum=5),
        "label": _enum_or(row["label"], LABEL_VALUES, "none"),
        "captureTime": _text_or(
            row["capture_time"] or row["mtime_iso"], None),
        "mtime": mtime_ns / 1e9,
        "fileKey": _text_or(row["header_hash"]),
        "recoverySourceKey": source_revision(_text_or(row["header_hash"]),
            _int_or(row["size"], 0, minimum=0), mtime_ns),
        "availability": _text_or(row["availability"], "local"),
        "width": width,
        "height": height,
        "camera": " ".join(filter(None, (camera_make, camera_model))),
        "lens": _text_or(row["lens"], None),
        "hasEdits": bool(row["has_edits"]),
        "size": _int_or(row["size"], 0, minimum=0),
    }


# --------------------------------------------------------- qualified names

def qualified_name(source_id: int, relpath: str,
                   copy_ident: str | None = None) -> str:
    """`3:trip/frame.RAF` — a name unique across sources.

    The API used to pass a path relative to the one open folder. With several
    sources open at once those names collide, so every name now carries its
    source. `parse_name` is the only place that splits it.
    """
    base = f"{int(source_id)}:{relpath}"
    return f"{base}{VIRTUAL_MARKER}{copy_ident}" if copy_ident else base


VIRTUAL_MARKER = "::lighttable-copy::"


def parse_name(name: str) -> tuple[int | None, str, str | None]:
    """Split a qualified name into (source_id, relpath, copy_ident)."""
    text = str(name)
    copy_ident = None
    if VIRTUAL_MARKER in text:
        text, copy_ident = text.split(VIRTUAL_MARKER, 1)
    if ":" in text:
        head, rest = text.split(":", 1)
        if head.isdigit():
            return int(head), rest, copy_ident
    return None, text, copy_ident
