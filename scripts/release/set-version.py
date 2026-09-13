#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Set or check the one LightTable release version.

    python3 scripts/release/set-version.py 0.7.1    # before tagging v0.7.1
    python3 scripts/release/set-version.py --check  # also run by the unit tests

`app_version.py` is the source. The Xcode project mirrors it for developer
builds. Published release records (the aggregate manifest and npm metadata) may
trail an unreleased bump but must never be ahead of it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SOURCE_LINE = re.compile(r'^VERSION = "([^"]*)"$', re.MULTILINE)
MARKETING = re.compile(r"MARKETING_VERSION = ([^;]+);")
XCODE_PROJECT = "LightTable.xcodeproj/project.pbxproj"
PUBLISHED_RECORDS = (
    "release/manifest.json",
    "packaging/npm/package.json",
    "packaging/npm/release.json",
)


def version_key(version: str) -> tuple[int, int, int]:
    base = version.split("-", 1)[0]
    if not STABLE.fullmatch(base):
        raise ValueError(f"not a release version: {version!r}")
    return tuple(int(part) for part in base.split("."))


def source_version(root: Path = ROOT) -> str:
    match = SOURCE_LINE.search((root / "app_version.py").read_text())
    if not match:
        raise ValueError("app_version.py does not define VERSION")
    return match.group(1)


def published_versions(root: Path = ROOT) -> dict[str, str]:
    found = {}
    for relative in PUBLISHED_RECORDS:
        path = root / relative
        if not path.is_file():
            continue
        record = json.loads(path.read_text())
        found[relative] = record["version"]
        for platform, entry in (record.get("platforms") or {}).items():
            if isinstance(entry, dict) and entry.get("version"):
                found[f"{relative} {platform}"] = entry["version"]
    return found


def problems(root: Path = ROOT) -> list[str]:
    version = source_version(root)
    if not STABLE.fullmatch(version):
        return [f"app_version.py VERSION must be MAJOR.MINOR.PATCH, not {version!r}"]
    found = []
    marketing = set(MARKETING.findall((root / XCODE_PROJECT).read_text()))
    if marketing != {version}:
        found.append(f"{XCODE_PROJECT} MARKETING_VERSION is {sorted(marketing)}, expected {version}")
    for record, published in published_versions(root).items():
        if version_key(published) > version_key(version):
            found.append(
                f"{record} records {published}, but app_version.py is still {version}; "
                "bump it with scripts/release/set-version.py"
            )
    return found


def set_version(version: str, root: Path = ROOT) -> None:
    if not STABLE.fullmatch(version):
        raise ValueError("Use a stable MAJOR.MINOR.PATCH version such as 0.7.1")
    current = source_version(root)
    if version_key(version) < version_key(current):
        raise ValueError(f"Refusing to lower the version from {current} to {version}")
    source = root / "app_version.py"
    source.write_text(SOURCE_LINE.sub(f'VERSION = "{version}"', source.read_text(), count=1))
    project = root / XCODE_PROJECT
    project.write_text(MARKETING.sub(f"MARKETING_VERSION = {version};", project.read_text()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", nargs="?", help="new MAJOR.MINOR.PATCH release version")
    parser.add_argument("--check", action="store_true", help="verify every copy agrees")
    args = parser.parse_args()
    if bool(args.version) == args.check:
        parser.error("pass either a version or --check")
    try:
        if args.version:
            set_version(args.version)
    except ValueError as error:
        parser.error(str(error))
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"LightTable version {source_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
