"""Face groups, user names and durable corrections, separate from generated tags."""

from __future__ import annotations

from server_localization import T

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

AUTO_THRESHOLD = 0.50
# A name is user-curated identity, so require stronger evidence to extend it.
NAMED_AUTO_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.40
AUTO_MARGIN = 0.08


def identifier():
    return uuid.uuid4().hex


class FaceStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA secure_delete=ON")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS groups (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                        hidden INTEGER NOT NULL DEFAULT 0, cover TEXT);
                    CREATE TABLE IF NOT EXISTS photos (
                        name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, model TEXT NOT NULL,
                        source_id TEXT NOT NULL DEFAULT '');
                    CREATE TABLE IF NOT EXISTS faces (
                        id TEXT PRIMARY KEY, photo TEXT NOT NULL REFERENCES photos(name) ON DELETE CASCADE,
                        group_id TEXT NOT NULL REFERENCES groups(id), box TEXT NOT NULL,
                        vector BLOB NOT NULL, thumbnail BLOB NOT NULL, quality REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS face_groups ON faces(group_id);
                    CREATE INDEX IF NOT EXISTS face_photos ON faces(photo);
                    CREATE TABLE IF NOT EXISTS exclusions (
                        a TEXT NOT NULL, b TEXT NOT NULL, PRIMARY KEY(a, b));
                    CREATE TABLE IF NOT EXISTS undo (id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL);
                """)
                if "source_id" not in {r[1] for r in db.execute("PRAGMA table_info(photos)")}:
                    db.execute("ALTER TABLE photos ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def stats(self):
        if not self.path.exists():
            return dict(photos=0, faces=0, groups=0, named=0, canUndo=False)
        with self.connect() as db:
            return dict(photos=db.execute("SELECT count(*) FROM photos").fetchone()[0],
                        faces=db.execute("SELECT count(*) FROM faces").fetchone()[0],
                        groups=db.execute("SELECT count(DISTINCT group_id) FROM faces").fetchone()[0],
                        named=db.execute("SELECT count(*) FROM groups WHERE name<>'' AND id IN (SELECT group_id FROM faces)").fetchone()[0],
                        canUndo=bool(db.execute("SELECT 1 FROM undo").fetchone()))

    def current(self, photo, fingerprint, model):
        if not self.path.exists():
            return False
        with self.connect() as db:
            row = db.execute("SELECT fingerprint, model FROM photos WHERE name=?", (photo,)).fetchone()
            return bool(row and row["fingerprint"] == fingerprint and row["model"] == model)

    @staticmethod
    def _profiles(db):
        rows = db.execute("SELECT f.group_id, f.vector, f.photo, g.name, g.hidden FROM faces f JOIN groups g ON g.id=f.group_id ORDER BY f.quality DESC").fetchall()
        profiles = {}
        for row in rows:
            item = profiles.setdefault(row["group_id"], {"name": row["name"], "hidden": row["hidden"], "vectors": [], "photos": set()})
            if len(item["vectors"]) < 5:
                item["vectors"].append(np.frombuffer(row["vector"], dtype="<f4"))
            item["photos"].add(row["photo"])
        return profiles

    def add_photo(self, photo, fingerprint, model, faces, source_id=""):
        with self.connect() as db:
            # Reuse assignments spatially on refreshed previews, including hidden
            # faces and rejected matches. Never discard user names on a rebuild.
            old = list(db.execute("SELECT id, group_id, box, vector FROM faces WHERE photo=?", (photo,)))
            db.execute("INSERT OR REPLACE INTO photos VALUES (?,?,?,?)", (photo, fingerprint, model, str(source_id or "")))
            profiles = self._profiles(db)
            used_groups = set()
            for face in sorted(faces, key=lambda f: -f["quality"]):
                vector = np.asarray(face["vector"], dtype="<f4")
                if vector.shape != (128,) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
                    raise ValueError(T("Invalid face embedding"))
                vector = vector / max(float(np.linalg.norm(vector)), 1e-8)
                best_old = max(old, key=lambda r: _iou(json.loads(r["box"]), face["box"]), default=None)
                if (best_old is not None and _iou(json.loads(best_old["box"]), face["box"]) >= 0.5
                        and float(np.frombuffer(best_old["vector"], dtype="<f4") @ vector) >= 0.5):
                    face_id, group = best_old["id"], best_old["group_id"]
                    old.remove(best_old)
                else:
                    face_id, group = identifier(), None
                    candidates = []
                    for gid, p in profiles.items():
                        if p["hidden"] or photo in p["photos"] or gid in used_groups:
                            continue
                        # Require both a strong exemplar and agreement across
                        # reference faces, avoiding single-link chaining.
                        scores = np.asarray(p["vectors"]) @ vector
                        score = min(float(scores.max()), float(scores.mean()) + 0.04)
                        candidates.append((score, gid))
                    candidates.sort(reverse=True)
                    if candidates:
                        score, best_group = candidates[0]
                        threshold = NAMED_AUTO_THRESHOLD if profiles[best_group]["name"] else AUTO_THRESHOLD
                        # Rank named and unnamed groups together, including those
                        # below their own threshold, so a plausible rival still
                        # prevents an ambiguous automatic assignment.
                        if score >= threshold and (
                                len(candidates) == 1 or score - candidates[1][0] >= AUTO_MARGIN):
                            group = best_group
                    if group is None:
                        group = identifier()
                        db.execute("INSERT INTO groups(id) VALUES (?)", (group,))
                used_groups.add(group)
                db.execute("INSERT INTO faces VALUES (?,?,?,?,?,?,?)", (
                    face_id, photo, group, json.dumps(face["box"]), vector.tobytes(),
                    face["thumbnail"], face["quality"]))
            db.execute("DELETE FROM undo")  # An undo never rewinds newly indexed photos.

    def remove_missing(self, names):
        if not self.path.exists():
            return
        with self.connect() as db:
            keep = set(names)
            missing = [r[0] for r in db.execute("SELECT name FROM photos") if r[0] not in keep]
            db.executemany("DELETE FROM photos WHERE name=?", [(n,) for n in missing])
            if missing:
                db.execute("DELETE FROM undo")

    def reconcile_names(self, identities):
        """Follow catalog image IDs through renames without losing corrections."""
        if not self.path.exists():
            return
        with self.connect() as db:
            targets = {str(value): name for name, value in identities.items() if value}
            changes = [(dict(row), targets[row["source_id"]])
                       for row in db.execute("SELECT * FROM photos")
                       if row["source_id"] in targets and row["name"] != targets[row["source_id"]]]
            # Stage every move before assigning final names, including swaps.
            staged = []
            for row, target in changes:
                temporary = "renaming:" + identifier()
                db.execute("INSERT INTO photos VALUES (?,?,?,?)", (
                    temporary, row["fingerprint"], row["model"], row["source_id"]))
                db.execute("UPDATE faces SET photo=? WHERE photo=?", (temporary, row["name"]))
                db.execute("DELETE FROM photos WHERE name=?", (row["name"],))
                staged.append((temporary, target, row))
            for temporary, target, row in staged:
                db.execute("INSERT OR REPLACE INTO photos VALUES (?,?,?,?)", (
                    target, row["fingerprint"], row["model"], row["source_id"]))
                db.execute("UPDATE faces SET photo=? WHERE photo=?", (target, temporary))
                db.execute("DELETE FROM photos WHERE name=?", (temporary,))
            # Populate identities on indexes created before this schema version.
            db.executemany("UPDATE photos SET source_id=? WHERE name=? AND source_id=''",
                           [(str(value), name) for name, value in identities.items() if value])

    def gallery(self):
        if not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute("""SELECT g.id, g.name, g.hidden,
                count(f.id) AS faceCount, count(DISTINCT f.photo) AS photoCount,
                COALESCE((SELECT id FROM faces WHERE id=g.cover AND group_id=g.id),
                  (SELECT id FROM faces WHERE group_id=g.id ORDER BY quality DESC, id LIMIT 1)) AS cover
                FROM groups g JOIN faces f ON f.group_id=g.id GROUP BY g.id
                ORDER BY (g.name=''), photoCount DESC, g.name COLLATE NOCASE, g.id""")
            return [dict(row) for row in rows]

    def members(self, group):
        if not self.path.exists():
            return []
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id, photo, box FROM faces WHERE group_id=? ORDER BY photo, id", (group,))]

    def thumbnail(self, face):
        with self.connect() as db:
            row = db.execute("SELECT thumbnail FROM faces WHERE id=?", (face,)).fetchone()
            return bytes(row[0]) if row else None

    def labels(self, names):
        if not self.path.exists():
            return {}
        allowed = set(names)
        result = {}
        with self.connect() as db:
            for row in db.execute("SELECT DISTINCT f.photo, g.name FROM faces f JOIN groups g ON f.group_id=g.id WHERE g.name<>'' AND g.hidden=0"):
                if row["photo"] in allowed:
                    result.setdefault(row["photo"], []).append(row["name"])
        return result

    def suggestions(self, limit=40):
        if not self.path.exists():
            return []
        with self.connect() as db:
            profiles = self._profiles(db)
            blocked = {tuple(r) for r in db.execute("SELECT a,b FROM exclusions")}
        keys = [k for k, p in profiles.items() if not p["hidden"]]
        # Chunked comparisons avoid an unbounded N x N allocation.
        centers = []
        for key in keys:
            v = np.mean(profiles[key]["vectors"], axis=0)
            centers.append(v / max(float(np.linalg.norm(v)), 1e-8))
        if not centers:
            return []
        matrix = np.asarray(centers)
        candidates = []
        for i, key in enumerate(keys):
            scores = matrix[i+1:] @ matrix[i]
            for offset in np.flatnonzero(scores >= REVIEW_THRESHOLD):
                other = keys[i + 1 + int(offset)]
                if tuple(sorted((key, other))) in blocked:
                    continue
                # Two faces in the same photo are different people. A manual
                # merge remains possible for mirrors or collages.
                if profiles[key]["photos"] & profiles[other]["photos"]:
                    continue
                candidates.append((float(scores[offset]), key, other))
            if len(candidates) > limit * 4:
                candidates = sorted(candidates, reverse=True)[:limit]
        return [{"a": a, "b": b} for _, a, b in sorted(candidates, reverse=True)[:limit]]

    @staticmethod
    def _snapshot(db):
        snapshot = {"groups": [dict(r) for r in db.execute("SELECT * FROM groups")],
                    "faces": [dict(r) for r in db.execute("SELECT id, group_id FROM faces")],
                    "exclusions": [list(r) for r in db.execute("SELECT a,b FROM exclusions")]}
        db.execute("DELETE FROM undo")
        db.execute("INSERT INTO undo VALUES (1,?)", (json.dumps(snapshot),))

    def edit(self, action, body):
        with self.connect() as db:
            if action == "undo":
                row = db.execute("SELECT snapshot FROM undo WHERE id=1").fetchone()
                if not row:
                    raise ValueError(T("There is no face-group change to undo"))
                saved = json.loads(row[0])
                for g in saved["groups"]:
                    db.execute("INSERT OR REPLACE INTO groups VALUES (?,?,?,?)", (g["id"], g["name"], g["hidden"], g["cover"]))
                for f in saved["faces"]:
                    db.execute("UPDATE faces SET group_id=? WHERE id=?", (f["group_id"], f["id"]))
                db.execute("DELETE FROM exclusions")
                db.executemany("INSERT INTO exclusions VALUES (?,?)", saved["exclusions"])
                db.execute("DELETE FROM undo")
                return
            group = str(body.get("group", ""))
            if not db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone():
                raise ValueError(T("This person group no longer exists"))
            self._snapshot(db)
            if action == "rename":
                name = " ".join(str(body.get("name", "")).split())
                if len(name) > 100:
                    raise ValueError(T("Use a name of 100 characters or fewer"))
                db.execute("UPDATE groups SET name=? WHERE id=?", (name, group))
            elif action == "hide":
                db.execute("UPDATE groups SET hidden=? WHERE id=?", (int(bool(body.get("hidden", True))), group))
            elif action == "cover":
                face = str(body.get("face", ""))
                if not db.execute("SELECT 1 FROM faces WHERE id=? AND group_id=?", (face, group)).fetchone():
                    raise ValueError(T("Choose a face from this group"))
                db.execute("UPDATE groups SET cover=? WHERE id=?", (face, group))
            elif action in ("merge", "reject"):
                other = str(body.get("other", ""))
                if other == group or not db.execute("SELECT 1 FROM faces WHERE group_id=?", (other,)).fetchone():
                    raise ValueError(T("Choose a different person group"))
                if action == "reject":
                    db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?)", tuple(sorted((group, other))))
                else:
                    # Redirect all exclusions when groups merge. Tombstones
                    # retain identity; undo can restore the original groups.
                    exclusions = [tuple(r) for r in db.execute("SELECT a,b FROM exclusions WHERE a=? OR b=?", (other, other))]
                    db.execute("DELETE FROM exclusions WHERE a=? OR b=?", (other, other))
                    for a, b in exclusions:
                        pair = tuple(sorted((group if a == other else a, group if b == other else b)))
                        if pair[0] != pair[1]:
                            db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?)", pair)
                    db.execute("DELETE FROM exclusions WHERE a=? AND b=?", tuple(sorted((group, other))))
                    target_name = db.execute("SELECT name FROM groups WHERE id=?", (group,)).fetchone()[0]
                    if not target_name:
                        db.execute("UPDATE groups SET name=(SELECT name FROM groups WHERE id=?) WHERE id=?", (other, group))
                    db.execute("UPDATE faces SET group_id=? WHERE group_id=?", (group, other))
            elif action == "split":
                faces = body.get("faces")
                if not isinstance(faces, list) or not faces or len(faces) > 10000:
                    raise ValueError(T("Select faces to move to a separate group"))
                members = {r[0] for r in db.execute("SELECT id FROM faces WHERE group_id=?", (group,))}
                selected = set(map(str, faces))
                if not selected <= members or selected == members:
                    raise ValueError(T("Select some, but not all, faces in this group"))
                new_group = identifier()
                db.execute("INSERT INTO groups(id) VALUES (?)", (new_group,))
                db.executemany("UPDATE faces SET group_id=? WHERE id=?", [(new_group, f) for f in selected])
                db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?)", tuple(sorted((group, new_group))))
            else:
                raise ValueError(T("Unknown face-group action"))

    def clear(self):
        with self.connect() as db:
            for table in ("faces", "photos", "groups", "exclusions", "undo"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            db.execute("VACUUM")


def _iou(a, b):
    overlap = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return overlap / max(a[2]*a[3]+b[2]*b[3]-overlap, 1e-8)
