#!/usr/bin/env python3
"""Generate NSIS removal commands for shipped files, preserving other files."""

from __future__ import annotations

import argparse
from pathlib import Path, PureWindowsPath


def nsis_path(relative: Path) -> str:
    windows = PureWindowsPath(relative)
    if relative.is_absolute() or windows.drive or windows.root or ".." in windows.parts:
        raise ValueError(f"Unsafe package path: {relative}")
    value = str(relative).replace("\\", "/")
    if any(character in value for character in ('"', "\r", "\n", "<", ">", "|", "?", "*", ":")):
        raise ValueError(f"Unsupported package path: {relative}")
    return value.replace("$", "$$").replace("/", "\\")


def make_manifest(payload: Path) -> str:
    files = []
    directories = []
    for item in payload.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"A Windows payload cannot contain symlinks: {item}")
        relative = item.relative_to(payload)
        nsis_path(relative)  # Validate directories as well as files.
        (directories if item.is_dir() else files).append(relative)
    lines = ['; Generated from the exact payload. Never recursively delete the install directory.']
    lines.extend(f'  Delete "$INSTDIR\\{nsis_path(item)}"' for item in sorted(files))
    lines.extend(
        f'  RMDir "$INSTDIR\\{nsis_path(item)}"'
        for item in sorted(directories, key=lambda item: (-len(item.parts), str(item)))
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not args.payload.is_dir():
        parser.error("payload must be a directory")
    args.output.write_text(make_manifest(args.payload), encoding="utf-8")


if __name__ == "__main__":
    main()
