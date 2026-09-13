# SPDX-License-Identifier: GPL-3.0-only
"""Versioned upgrades for saved edit records.

A saved edit is a recipe, not a picture. When the meaning of a control
changes, every recipe written under the old meaning has to be rewritten so
the photo people already finished keeps looking the way they left it. This
module holds that translation in one place; every store that keeps edits
(the catalog, the per-folder state file, the preset library, sidecars,
browser recovery drafts) calls it with the version the record was written
under.

Version history:

1. Original layout. Whites was inverted: a positive value raised the white
   point and darkened the highlights, the opposite of every comparable
   editor and of the control's own name.
2. Whites corrected so a positive value brightens. Stored Whites values,
   global and inside every mask, are negated to preserve their appearance.

The module deliberately imports nothing heavy so metadata readers can use it
without pulling in numpy.
"""
from __future__ import annotations

import copy
import math

EDIT_SCHEMA_VERSION = 2
# Grade keys whose stored sign flips between version 1 and 2.
_NEGATED_AT_2 = ("whites",)


def record_version(value) -> int:
    """The schema a stored record declares, or 1 when it predates the marker."""
    if not isinstance(value, dict):
        return 1
    version = value.get("editSchema", 1)
    if isinstance(version, bool) or not isinstance(version, (int, float)):
        return 1
    if not math.isfinite(version):
        return 1
    return max(1, int(version))


def _negate(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number) or number == 0.0:
        return value
    return -number


def upgrade_grade(grade, from_version: int) -> dict:
    """Return the grade as the current schema reads it."""
    if not isinstance(grade, dict):
        return grade
    if from_version >= EDIT_SCHEMA_VERSION:
        return grade
    out = dict(grade)
    if from_version < 2:
        for key in _NEGATED_AT_2:
            if key in out:
                out[key] = _negate(out[key])
    return out


def upgrade_masks(masks, from_version: int):
    if not isinstance(masks, list) or from_version >= EDIT_SCHEMA_VERSION:
        return masks
    out = []
    for mask in masks:
        if isinstance(mask, dict) and isinstance(mask.get("grade"), dict):
            mask = dict(mask, grade=upgrade_grade(mask["grade"], from_version))
        out.append(mask)
    return out


def upgrade_edit(entry, from_version: int):
    """Upgrade one edit record: its grade, its masks, and any versions it holds.

    Presets, history steps, version checkpoints, and image states all share
    this shape, so the same function serves every store.
    """
    if not isinstance(entry, dict) or from_version >= EDIT_SCHEMA_VERSION:
        return entry
    out = dict(entry)
    if isinstance(out.get("grade"), dict):
        out["grade"] = upgrade_grade(out["grade"], from_version)
    if isinstance(out.get("masks"), list):
        out["masks"] = upgrade_masks(out["masks"], from_version)
    if isinstance(out.get("versions"), list):
        out["versions"] = [upgrade_edit(version, from_version)
                           for version in out["versions"]]
    return out


def upgrade_record(entry):
    """Upgrade a record that carries its own `editSchema` marker and stamp it."""
    if not isinstance(entry, dict):
        return entry
    out = upgrade_edit(entry, record_version(entry))
    out = dict(out)
    out["editSchema"] = EDIT_SCHEMA_VERSION
    return out


def upgrade_state(state) -> tuple[dict, bool]:
    """Upgrade a per-folder state file (`{"images": {name: entry}}`).

    Returns the upgraded state and whether anything had to change. The root
    marker is set either way, so a file written back is recognised as current.
    """
    if not isinstance(state, dict):
        return state, False
    version = record_version(state)
    if version >= EDIT_SCHEMA_VERSION:
        return state, False
    out = copy.deepcopy(state)
    images = out.get("images")
    if isinstance(images, dict):
        out["images"] = {name: upgrade_edit(entry, version)
                         for name, entry in images.items()}
    out["editSchema"] = EDIT_SCHEMA_VERSION
    return out, True
