"""Shared validation for catalog camera, exposure, keyword, and date rules."""
from __future__ import annotations

import math
from datetime import date
from fractions import Fraction

# Public filter name, stored column, browser metadata name.
EXPOSURE_FIELDS = (
    ("iso", "iso", "iso"),
    ("focalLength", "focal_length", "focalLength"),
    ("aperture", "aperture", "aperture"),
    ("shutter", "shutter_seconds", "shutterSeconds"),
)


def positive_number(value):
    """Read EXIF rationals and exposure input without accepting NaN or infinity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(Fraction(str(value).strip()))
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def clean_filters(value: dict) -> dict:
    result = {}
    for key in ("camera", "lens", "keyword"):
        if value.get(key):
            result[key] = " ".join(str(value[key]).split())[:200]
    for field, _, _ in EXPOSURE_FIELDS:
        for bound in ("Min", "Max"):
            key = field + bound
            raw = value.get(key)
            if raw is None or raw == "":
                continue
            number = positive_number(raw)
            if number is None:
                raise ValueError(f"{key} must be a positive number")
            result[key] = number
        if (field + "Min" in result and field + "Max" in result
                and result[field + "Min"] > result[field + "Max"]):
            raise ValueError(f"{field} minimum must not exceed maximum")
    for key in ("dateFrom", "dateTo"):
        if value.get(key):
            raw = str(value[key])
            # Existing callers may use an ISO timestamp; retain it after validation.
            from datetime import datetime
            try:
                if len(raw) == 10:
                    date.fromisoformat(raw)
                else:
                    datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"{key} must be an ISO date or timestamp") from None
            result[key] = raw
    if (result.get("dateFrom") and result.get("dateTo")
            and result["dateFrom"][:10] > result["dateTo"][:10]):
        raise ValueError("Capture date start must not follow end")
    return result
