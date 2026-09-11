# SPDX-License-Identifier: GPL-3.0-only
"""Translate shell-friendly photo selectors to the catalog query contract."""
from __future__ import annotations

import json
from pathlib import Path

from .client import Client, query


def parse_where(expressions: list[str] | None, *, limit: int = 5000,
                offset: int = 0, sort: str = "capture:desc") -> dict:
    filters: dict = {}
    spec: dict = {"filter": filters, "limit": limit, "offset": offset}
    field, _, direction = sort.partition(":")
    spec["sort"] = {"field": field, "dir": direction or "asc"}
    mapping = {
        "status": "status", "label": "label", "kind": "kind",
        "camera": "camera", "lens": "lens", "keyword": "keyword",
        "from": "dateFrom", "to": "dateTo", "q": "query",
    }
    for expression in expressions or []:
        numeric = {"iso": "iso", "focal-length": "focalLength", "aperture": "aperture", "shutter": "shutter"}
        matched = False
        for operator, suffix in ((">=", "Min"), ("<=", "Max"), ("=", "")):
            if operator not in expression:
                continue
            key, value = expression.split(operator, 1)
            if key not in numeric:
                continue
            from dam_filters import positive_number
            number = positive_number(value)
            if number is None:
                raise ValueError(f"invalid exposure selector: {expression}")
            for bound in ([suffix] if suffix else ["Min", "Max"]):
                filters[numeric[key] + bound] = number
            matched = True
            break
        if matched:
            continue
        if ">=" in expression:
            key, value = expression.split(">=", 1)
            if key != "rating":
                raise ValueError(f"unsupported selector: {expression}")
            filters["ratingMin"] = int(value)
            continue
        key, separator, value = expression.partition("=")
        if not separator:
            raise ValueError(f"selector must be key=value: {expression}")
        if key in mapping:
            filters[mapping[key]] = value
        elif key in {"source", "folder", "collection"}:
            spec["scope"] = key
            spec[{"source": "sourceId", "folder": "folderId",
                  "collection": "collectionId"}[key]] = int(value)
        else:
            raise ValueError(f"unsupported selector: {key}")
    return spec


def list_names(client: Client, expressions: list[str] | None, *,
               limit: int = 5000, offset: int = 0,
               sort: str = "capture:desc") -> list[str]:
    result = client.post("/api/catalog/query", parse_where(
        expressions, limit=limit, offset=offset, sort=sort))
    return [str(item["name"]) for item in result.get("items", [])]


def resolve_reference(client: Client, value: str) -> list[str]:
    if value == "@current" or value == "@selection":
        state = client.get("/api/ui/state")
        if value == "@current":
            return [state["current"]] if state.get("current") else []
        return [str(name) for name in state.get("selection", [])]
    if value.startswith("#") and value[1:].isdigit():
        result = client.post("/api/catalog/query", {"limit": 5000})
        return [str(item["name"]) for item in result.get("items", [])
                if int(item.get("id", -1)) == int(value[1:])]
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return [str(client.get(query("/api/resolve", path=str(candidate)))["name"])]
    return [value]


def resolve_many(client: Client, references: list[str] | None, *,
                 where: list[str] | None = None, names_from: str | None = None,
                 limit: int = 5000, sort: str = "capture:desc") -> list[str]:
    values = list(references or [])
    if names_from:
        if names_from == "-":
            import sys
            values += [line.strip() for line in sys.stdin if line.strip()]
        else:
            values += [line.strip() for line in
                       Path(names_from).read_text(encoding="utf-8").splitlines()
                       if line.strip()]
    names = []
    for value in values:
        names.extend(resolve_reference(client, value))
    if where:
        names.extend(list_names(client, where, limit=limit, sort=sort))
    return list(dict.fromkeys(names))
