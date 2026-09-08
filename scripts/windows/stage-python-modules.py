#!/usr/bin/env python3
"""Stage the application's root Python modules for the Windows runtime.

The repository root contains runtime modules; development scripts and tests
live in their own directories. Include the complete root module set so imports
inside optional features and transitive dependencies cannot escape an allowlist.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


def stage_modules(project: Path, resources: Path) -> list[Path]:
    if not (project / "server.py").is_file():
        raise ValueError(f"No LightTable server in {project}")
    modules = sorted(project.glob("*.py"))
    for source in modules:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Runtime module must be a regular file: {source.name}")
    resources.mkdir(parents=True, exist_ok=True)
    for source in modules:
        shutil.copyfile(source, resources / source.name)
    return [resources / source.name for source in modules]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("resources", type=Path)
    args = parser.parse_args()
    staged = stage_modules(args.project.resolve(), args.resources.resolve())
    print(f"Staged {len(staged)} Python runtime modules")


if __name__ == "__main__":
    main()
