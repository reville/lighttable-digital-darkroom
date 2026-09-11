# SPDX-License-Identifier: GPL-3.0-only
"""Validation and identity helpers for non-destructive library organization."""
from __future__ import annotations

import hashlib
import time

import dam_filters


VIRTUAL_MARKER = "::lighttable-copy::"


def source_name(name: str) -> str:
    return str(name).split(VIRTUAL_MARKER, 1)[0]


def is_virtual(name: str) -> bool:
    return VIRTUAL_MARKER in str(name)


def virtual_name(source: str, ident: str) -> str:
    return f"{source_name(source)}{VIRTUAL_MARKER}{ident}"


def _identifier(value, prefix: str) -> str:
    text = str(value or "").strip()[:100]
    if text:
        return text
    return prefix + "-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:14]


def _name(value, default: str) -> str:
    result = " ".join(str(value or default).split()).strip()
    return (result or default)[:100]


def _members(values) -> list[str]:
    result = []
    seen = set()
    for value in values[:10000] if isinstance(values, list) else []:
        name = str(value)[:1000]
        if name and name not in seen:
            seen.add(name); result.append(name)
    return result


def _rating(value, default: int = 0) -> int:
    try:
        return max(0, min(5, int(value)))
    except (TypeError, ValueError):
        return default


def clean_collections(values) -> list[dict]:
    result = []
    seen = set()
    for raw in values[:100] if isinstance(values, list) else []:
        if not isinstance(raw, dict):
            continue
        ident = _identifier(raw.get("id"), "collection")
        if ident in seen:
            continue
        seen.add(ident)
        kind = "smart" if raw.get("type") == "smart" else "regular"
        rules = raw.get("rules") if isinstance(raw.get("rules"), dict) else {}
        result.append({
            "id": ident, "name": _name(raw.get("name"), "Collection"),
            "type": kind, "members": _members(raw.get("members")),
            "rules": {
                **dam_filters.clean_filters(rules),
                "flag": str(rules.get("flag", "all"))
                if str(rules.get("flag", "all")) in
                ("all", "pending", "approved", "skipped") else "all",
                "ratingMin": _rating(rules.get("ratingMin", 0)),
                "kind": str(rules.get("kind", "all"))
                if str(rules.get("kind", "all")) in
                ("all", "raw", "processed", "virtual") else "all",
                "query": str(rules.get("query", ""))[:200],
                "fileTypes": [kind for kind in ("raw", "jpeg", "heic", "tiff", "png")
                              if isinstance(rules.get("fileTypes"), list) and kind in rules["fileTypes"]],
                "editState": rules.get("editState") if rules.get("editState") in
                ("edited", "unedited", "virtual") else "all",
                "unrated": rules.get("unrated") is True,
                "label": rules.get("label") if rules.get("label") in
                ("any", "none", "red", "yellow", "green", "blue", "purple") else "all",
            },
        })
    return result


def clean_stacks(values) -> list[dict]:
    result = []
    occupied = set()
    for raw in values[:1000] if isinstance(values, list) else []:
        if not isinstance(raw, dict):
            continue
        members = [name for name in _members(raw.get("members"))
                   if name not in occupied]
        if len(members) < 2:
            continue
        occupied.update(members)
        result.append({
            "id": _identifier(raw.get("id"), "stack"),
            "name": _name(raw.get("name"), "Photo stack"),
            "members": members, "collapsed": raw.get("collapsed") is not False,
        })
    return result


def clean_virtual_copies(values) -> list[dict]:
    result = []
    seen = set()
    for raw in values[:5000] if isinstance(values, list) else []:
        if not isinstance(raw, dict):
            continue
        source = source_name(str(raw.get("source", "")))[:1000]
        ident = _identifier(raw.get("id"), "copy")
        name = virtual_name(source, ident)
        if not source or name in seen:
            continue
        seen.add(name)
        result.append({
            "id": ident, "name": name, "source": source,
            "displayName": _name(raw.get("displayName"), "Virtual copy"),
            "created": str(raw.get("created", ""))[:40],
        })
    return result


def clean_library_state(state: dict | None) -> dict:
    state = state if isinstance(state, dict) else {}
    return {
        "collections": clean_collections(state.get("collections")),
        "stacks": clean_stacks(state.get("stacks")),
        "virtualCopies": clean_virtual_copies(state.get("virtualCopies")),
    }
