#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Remove build-machine paths from a bundled standalone Python runtime."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


MARKER = "__LIGHTTABLE_PYTHON_RUNTIME__"


def rewrite_sysconfig(runtime: Path) -> None:
    candidates = list(
        (runtime / "lib").glob("python*/_sysconfigdata__darwin_darwin.py")
    )
    if len(candidates) != 1:
        raise SystemExit(f"expected one sysconfig data file, found {len(candidates)}")
    config = candidates[0]
    source = config.read_text()
    match = re.search(r'^    "prefix": "([^"]+)",$', source, re.MULTILINE)
    if not match:
        raise SystemExit("configured prefix was not present in sysconfig data")
    source = source.replace(match.group(1), MARKER)
    source += f'''\n\n# Resolve installation paths from this file after the app is moved.\nfrom pathlib import Path as _RuntimePath\n_runtime_prefix = str(_RuntimePath(__file__).resolve().parents[2])\nfor _runtime_key, _runtime_value in tuple(build_time_vars.items()):\n    if isinstance(_runtime_value, str):\n        build_time_vars[_runtime_key] = _runtime_value.replace(\n            "{MARKER}", _runtime_prefix\n        )\ndel _RuntimePath, _runtime_prefix, _runtime_key, _runtime_value\n'''
    config.write_text(source)


def rewrite_entry_points(runtime: Path) -> None:
    prefix = str(runtime.resolve())
    replacement = '"$(dirname "$0")/python3.13"'
    for candidate in (runtime / "bin").iterdir():
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            source = candidate.read_text()
        except UnicodeDecodeError:
            continue
        quoted_prefix = f"'{prefix}/bin/python3.13'"
        if quoted_prefix in source:
            candidate.write_text(source.replace(quoted_prefix, replacement))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if not (runtime / "bin/python3.13").is_file():
        raise SystemExit(f"not a Python runtime: {runtime}")
    rewrite_sysconfig(runtime)
    rewrite_entry_points(runtime)


if __name__ == "__main__":
    main()
