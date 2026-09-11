# SPDX-License-Identifier: GPL-3.0-only
"""Private SQLite storage for the optional local photo index."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class IndexStore:
    """Small, thread-safe index kept outside the user's photo library."""

    def __init__(self, database: Path):
        self.database = database
        self._lock = threading.RLock()
        self.last_recovery: Path | None = None

    @property
    def exists(self) -> bool:
        return self.database.is_file()

    def _open(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"local index integrity check failed: {check[0] if check else 'no result'}")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    name TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    ocr_json TEXT NOT NULL DEFAULT '[]',
                    faces_json TEXT NOT NULL DEFAULT '[]',
                    cull_json TEXT NOT NULL DEFAULT '{}',
                    analysis_version INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS photo_search USING fts5(
                    name UNINDEXED,
                    caption,
                    tags,
                    ocr,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            existing = {row["name"] for row in
                        connection.execute("PRAGMA table_info(photos)")}
            for column, definition in (
                ("cull_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("analysis_version", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE photos ADD COLUMN {column} {definition}")
        except Exception:
            connection.close()
            raise
        return connection

    def _quarantine(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        recovery = self.database.parent / "Recovery" / stamp
        recovery.mkdir(parents=True, exist_ok=False)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.database) + suffix)
            if source.exists():
                os.replace(source, recovery / source.name)
        self.last_recovery = recovery
        return recovery

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._open()
        except sqlite3.DatabaseError:
            # This index contains generated metadata only. Preserve damaged
            # bytes for diagnosis, then rebuild an empty index automatically.
            self._quarantine()
            return self._open()

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_current(self, name: str, fingerprint: str,
                   analysis_version: int = 0) -> bool:
        if not self.exists:
            return False
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT fingerprint, error, analysis_version
                   FROM photos WHERE name = ?""", (name,)
            ).fetchone()
            return bool(row and row["fingerprint"] == fingerprint
                        and not row["error"]
                        and int(row["analysis_version"] or 0) >= analysis_version)

    def upsert(self, name: str, fingerprint: str, result: dict, now: float,
               analysis_version: int = 0) -> None:
        tags = _clean_strings(result.get("tags"), 20, 100)
        ocr = _clean_strings(result.get("ocr"), 50, 240)
        faces = result.get("faces") if isinstance(result.get("faces"), list) else []
        caption = " ".join(str(result.get("caption", "")).split())[:500]
        provider = str(result.get("provider", ""))[:80]
        cull = result.get("cull") if isinstance(result.get("cull"), dict) else {}
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO photos (
                    name, fingerprint, caption, tags_json, ocr_json,
                    faces_json, cull_json, analysis_version, provider,
                    updated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(name) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    caption=excluded.caption,
                    tags_json=excluded.tags_json,
                    ocr_json=excluded.ocr_json,
                    faces_json=excluded.faces_json,
                    cull_json=excluded.cull_json,
                    analysis_version=excluded.analysis_version,
                    provider=excluded.provider,
                    updated_at=excluded.updated_at,
                    error=''
                """,
                (name, fingerprint, caption, json.dumps(tags), json.dumps(ocr),
                 json.dumps(faces), json.dumps(cull), int(analysis_version),
                 provider, now),
            )
            connection.execute("DELETE FROM photo_search WHERE name = ?", (name,))
            connection.execute(
                "INSERT INTO photo_search(name, caption, tags, ocr) VALUES (?, ?, ?, ?)",
                (name, caption, " ".join(tags), " ".join(ocr)),
            )

    def record_error(self, name: str, fingerprint: str, error: str, now: float) -> None:
        message = " ".join(str(error).split())[:500]
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO photos (name, fingerprint, updated_at, error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    updated_at=excluded.updated_at,
                    error=excluded.error
                """,
                (name, fingerprint, now, message),
            )
            connection.execute("DELETE FROM photo_search WHERE name = ?", (name,))

    def clear_error(self, name: str) -> None:
        """Retire an unavailable input's old failure, preserving valid metadata."""
        if not self.exists:
            return
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM photos WHERE name = ? AND error != ''", (name,))

    def remove_missing(self, names: list[str]) -> None:
        if not self.exists:
            return
        keep = set(names)
        with self._lock, self._connection() as connection:
            stored = [row[0] for row in connection.execute("SELECT name FROM photos")]
            for name in stored:
                if name not in keep:
                    connection.execute("DELETE FROM photos WHERE name = ?", (name,))
                    connection.execute("DELETE FROM photo_search WHERE name = ?", (name,))

    def results(self, names: list[str] | None = None) -> dict[str, dict]:
        if not self.exists:
            return {}
        allowed = set(names) if names is not None else None
        output = {}
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT name, caption, tags_json, ocr_json, faces_json,
                          cull_json, provider, error
                   FROM photos WHERE error = ''"""
            )
            for row in rows:
                if allowed is not None and row["name"] not in allowed:
                    continue
                faces = _json_list(row["faces_json"])
                output[row["name"]] = {
                    "caption": row["caption"],
                    "tags": _json_list(row["tags_json"]),
                    "ocr": _json_list(row["ocr_json"]),
                    "faces": faces,
                    "faceCount": len(faces),
                    "cull": _json_object(row["cull_json"]),
                    "provider": row["provider"],
                }
        return output

    def stats(self) -> dict:
        if not self.exists:
            return {"indexed": 0, "errors": 0}
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS errors
                   FROM photos"""
            ).fetchone()
            errors = int(row["errors"] or 0)
            return {"indexed": int(row["total"] or 0) - errors, "errors": errors}

    def reset(self) -> None:
        """Delete only generated index data; settings are owned by the service."""
        with self._lock:
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.database) + suffix).unlink(missing_ok=True)


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_list(value: str) -> list:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _clean_strings(values, limit: int, length: int) -> list[str]:
    if not isinstance(values, list):
        return []
    output = []
    seen = set()
    for value in values[:limit]:
        text = " ".join(str(value).split()).strip()[:length]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output
