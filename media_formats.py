# SPDX-License-Identifier: GPL-3.0-only
"""One source of truth for media suffixes shared by every application shell."""
from __future__ import annotations

import json
from pathlib import Path


FORMAT_FILE = Path(__file__).with_name("media-formats.json")
FORMAT_GROUPS = json.loads(FORMAT_FILE.read_text())


def extensions(group: str, *, dotted: bool = True) -> frozenset[str]:
    prefix = "." if dotted else ""
    return frozenset(prefix + str(value).lower()
                     for value in FORMAT_GROUPS[group])


RAW_EXTENSIONS = extensions("raw")
PROCESSED_EXTENSIONS = extensions("processed")
VIDEO_EXTENSIONS = extensions("video")
PHOTO_EXTENSIONS = RAW_EXTENSIONS | PROCESSED_EXTENSIONS | VIDEO_EXTENSIONS
