#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Keep an SPDX license identifier on every first-party source file.

The tag states the license a file is under once it is separated from this tree.
Authorship is not repeated per file: LICENSE covers the work and git records who
wrote what.

Run with no arguments to add missing tags, or `--check` to fail when any
first-party source file lacks one. `tests/test_license_headers.py` runs the
check, so a new source file without a tag fails the suite.

Vendored upstream code keeps its own notices and is never touched; see
EXCLUDED_PREFIXES.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SPDX = "SPDX-License-Identifier: GPL-3.0-only"

# Upstream code vendored into this repository carries its own notices and must
# not be relabeled; provenance is recorded next to each vendored tree. The
# Flathub submission directory is assembled by scripts/flatpak/
# make-flathub-submission.py, so its copies inherit their tag from the source.
EXCLUDED_PREFIXES = ("rust-engine/vendor/", "packaging/flathub/")

HASH_STYLE = {".py", ".sh", ".ps1"}
SLASH_STYLE = {".rs", ".js", ".mjs", ".swift", ".wgsl", ".metal", ".cu"}
BLOCK_STYLE = {".css"}
EXTENSIONS = HASH_STYLE | SLASH_STYLE | BLOCK_STYLE

# Executable scripts that carry a shebang instead of a suffix.
EXTENSIONLESS = ("lighttable", "lighttable-cli", "lighttable-desktop")

CODING_DECLARATION = re.compile(rb"^#.*coding[:=]\s*[-\w.]+")


def tracked_sources() -> list[Path]:
    """Source files git tracks. Empty when git is unavailable, as in a tarball."""
    try:
        result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    listing = result.stdout.split()
    sources = []
    for relative in listing:
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        if path.suffix in EXTENSIONS or (not path.suffix and path.name in EXTENSIONLESS):
            sources.append(path)
    return sources


def _header(suffix: str, newline: str) -> bytes:
    if suffix in BLOCK_STYLE:
        line = f"/* {SPDX} */"
    elif suffix in SLASH_STYLE:
        line = f"// {SPDX}"
    else:
        line = f"# {SPDX}"
    return (line + newline).encode()


def add_header(path: Path) -> bool:
    """Insert the tag, preserving any BOM, shebang, encoding line and newline."""
    raw = path.read_bytes()
    if not raw.strip() or SPDX.encode() in raw[:2000]:
        return False

    byte_order_mark = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        byte_order_mark, raw = raw[:3], raw[3:]

    first = raw.split(b"\n", 1)[0]
    newline = "\r\n" if first.endswith(b"\r") else "\n"
    lines = raw.split(newline.encode())

    suffix = path.suffix
    insert_at = 0
    if lines and lines[0].startswith(b"#!"):
        insert_at = 1
        if suffix == ".py" and len(lines) > 1 and CODING_DECLARATION.match(lines[1]):
            insert_at = 2
    elif suffix == ".py" and lines and CODING_DECLARATION.match(lines[0]):
        insert_at = 1

    head = newline.encode().join(lines[:insert_at])
    if head:
        head += newline.encode()
    tail = newline.encode().join(lines[insert_at:])
    path.write_bytes(byte_order_mark + head + _header(suffix, newline) + tail)
    return True


def missing() -> list[str]:
    absent = []
    for path in tracked_sources():
        raw = path.read_bytes()
        if raw.strip() and SPDX.encode() not in raw[:2000]:
            absent.append(str(path.relative_to(ROOT)))
    return sorted(absent)


def main() -> int:
    if "--check" in sys.argv:
        absent = missing()
        if absent:
            print("Missing a license tag; run scripts/add-license-headers.py",
                  file=sys.stderr)
            for relative in absent:
                print(f"  {relative}", file=sys.stderr)
            return 1
        return 0
    changed = [path for path in tracked_sources() if add_header(path)]
    print(f"Added a license tag to {len(changed)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
