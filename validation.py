"""Strict request validation layered over LightTable's lenient cleaners."""
from __future__ import annotations

import math


class ValidationError(ValueError):
    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        first = issues[0] if issues else {"path": "request", "kind": "invalid"}
        self.field = first["path"]
        super().__init__(f"{self.field}: {first['kind']}")


def _same(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isfinite(float(left)) and math.isfinite(float(right)) \
            and abs(float(left) - float(right)) < 1e-9
    return type(left) is type(right) and left == right


def _compare(raw, cleaned, path: str, issues: list[dict], *,
             allowed: set[str] | None = None) -> None:
    if isinstance(raw, dict) and isinstance(cleaned, dict):
        for key, value in raw.items():
            child = f"{path}.{key}" if path else str(key)
            if allowed is not None and key not in allowed:
                issues.append({"path": child, "kind": "unknown",
                               "value": value})
            elif key not in cleaned:
                # Known optional identity values may legitimately disappear.
                if value not in (None, False, 0, 0.0, "", [], {}):
                    issues.append({"path": child, "kind": "rejected",
                                   "value": value})
            else:
                _compare(value, cleaned[key], child, issues)
        return
    if isinstance(raw, list) and isinstance(cleaned, list):
        if len(raw) != len(cleaned):
            issues.append({"path": path, "kind": "rejected",
                           "value": raw, "accepted": cleaned})
            return
        for index, value in enumerate(raw):
            _compare(value, cleaned[index], f"{path}[{index}]", issues)
        return
    if not _same(raw, cleaned):
        issues.append({"path": path, "kind": "clamped",
                       "value": raw, "accepted": cleaned})


def clean_state_patch(raw: dict, *, params_cleaner, grade_cleaner,
                      crop_cleaner, masks_cleaner, heals_cleaner,
                      optics_cleaner, keywords_cleaner, versions_cleaner,
                      label_cleaner, params_keys: set[str],
                      grade_keys: set[str], status_values: set[str],
                      strict: bool = False) -> tuple[dict, list[dict]]:
    """Clean the editable portion of a state request and report coercions."""
    if not isinstance(raw, dict):
        raise ValidationError([{"path": "request", "kind": "invalid"}])
    allowed = {
        "name", "origin", "historyLabel", "status", "rating", "label",
        "params", "grade", "crop", "masks", "heals", "optics",
        "keywords", "versions",
    }
    issues: list[dict] = []
    for key in raw:
        if key not in allowed:
            issues.append({"path": key, "kind": "unknown", "value": raw[key]})
    cleaned: dict = {}
    if "status" in raw:
        value = str(raw["status"])
        cleaned["status"] = value if value in status_values else "pending"
        _compare(raw["status"], cleaned["status"], "status", issues)
    if "rating" in raw:
        try:
            cleaned["rating"] = max(0, min(5, int(raw["rating"] or 0)))
        except (TypeError, ValueError):
            cleaned["rating"] = 0
        _compare(raw["rating"], cleaned["rating"], "rating", issues)
    if "label" in raw:
        cleaned["label"] = label_cleaner(raw["label"])
        _compare(raw["label"], cleaned["label"], "label", issues)
    cleaners = {
        "params": (params_cleaner, params_keys),
        "grade": (grade_cleaner, grade_keys),
        "crop": (crop_cleaner, {"x", "y", "w", "h"}),
        "masks": (masks_cleaner, None),
        "heals": (heals_cleaner, None),
        "optics": (optics_cleaner, set(optics_cleaner({}))),
        "keywords": (keywords_cleaner, None),
        "versions": (versions_cleaner, None),
    }
    for key, (cleaner, known) in cleaners.items():
        if key not in raw:
            continue
        cleaned[key] = cleaner(raw[key])
        _compare(raw[key], cleaned[key], key, issues, allowed=known)
    if strict and issues:
        raise ValidationError(issues)
    return cleaned, issues
