"""Atomic additive keyword batches with durable, conflict-checked undo."""
from __future__ import annotations

import json
import time
import uuid

from server_localization import T

MAX_BATCH = 5000


def change(cat, image_ids: list[int], values: list[str], action: str,
           *, undo_id: str = "") -> dict:
    if action not in {"add", "remove", "undo"}:
        raise ValueError(T("Choose add, remove, or undo keywords"))
    incoming = {value.casefold(): value for value in values}
    changes = []
    with cat.write() as conn:
        if action == "undo":
            key = "keywords.undo:" + undo_id
            record = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if not record:
                raise ValueError(T("That keyword change is no longer available to undo"))
            batch = json.loads(record[0])
            for item in batch["changes"]:
                if not cat.image_row(item["id"]):
                    raise ValueError(T("A photo in this keyword change is no longer available"))
                current = cat.keywords_for(item["id"])
                if current != item["after"]:
                    raise ValueError(T("Keywords changed after this batch. Review the photos before undoing."))
                changes.append({"id": item["id"], "before": current, "after": item["before"]})
        else:
            ids = list(dict.fromkeys(image_ids))
            if not ids or len(ids) > MAX_BATCH:
                raise ValueError(T("Select between 1 and {limit} photos", limit=MAX_BATCH))
            if not incoming:
                raise ValueError(T("Enter one or more keywords"))
            for image_id in ids:
                if not cat.image_row(image_id):
                    raise ValueError(T("A selected photo is no longer in the catalog"))
                before = cat.keywords_for(image_id)
                existing = {value.casefold(): value for value in before}
                if action == "add":
                    after = [*before, *(value for key, value in incoming.items() if key not in existing)]
                else:
                    after = [value for value in before if value.casefold() not in incoming]
                after = sorted(after)
                if len(after) > 100:
                    raise ValueError(T("A photo would exceed the limit of 100 keywords"))
                if before != after:
                    changes.append({"id": image_id, "before": before, "after": after})
        for item in changes:
            image_id = item["id"]
            before = cat.state_for(image_id)
            label = "Undo keyword batch" if action == "undo" else f"Keywords {action}"
            cat._add_history(conn, image_id, "Before " + label, before, origin="keywords")
            cat._save_state(conn, image_id, {"keywords": item["after"]})
            item["after"] = cat.keywords_for(image_id)
            cat._add_history(conn, image_id, label,
                             dict(before, keywords=item["after"]), origin="keywords")
        if action == "undo":
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
            undo_id = ""
        elif changes:
            undo_id = uuid.uuid4().hex
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (
                "keywords.undo:" + undo_id,
                json.dumps({"created": time.time(), "changes": changes})))
            # Keep a bounded history of batch undo records, separate from photo history.
            rows = conn.execute("SELECT key FROM meta WHERE key LIKE 'keywords.undo:%' "
                                "ORDER BY json_extract(value,'$.created') DESC LIMIT -1 OFFSET 20").fetchall()
            conn.executemany("DELETE FROM meta WHERE key=?", [(row[0],) for row in rows])
    return {"ok": True, "count": len(changes), "undoId": undo_id,
            "changes": [{"id": item["id"], "keywords": item["after"]} for item in changes]}
