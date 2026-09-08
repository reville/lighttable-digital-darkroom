#!/usr/bin/env python3
"""Generate an AUR recipe pinned to an official release; never download or publish."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import tarfile


SPEC = importlib.util.spec_from_file_location("arch_package", Path(__file__).with_name("make-arch-package.py"))
ARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCH)
RELEASES = "https://github.com/reville/lighttable-digital-darkroom/releases/download"


def generate(archive: Path, output: Path, *, version: str,
             source_revision: str, pkgrel: int = 1) -> Path:
    manifest = ARCH.release_metadata(archive, version=version, source_revision=source_revision)
    digest = ARCH.archive_sha256(archive)
    name = f"LightTable-{version}-linux-x86_64.tar.gz"
    source = f"{RELEASES}/v{version}/{name}"
    return ARCH.write_recipe(output, manifest, digest, source=source, pkgrel=pkgrel, release=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True, help="Canonical release version, e.g. 0.5.0")
    parser.add_argument("--source-revision", required=True, help="Full Git SHA pinned for that release")
    parser.add_argument("--pkgrel", type=int, default=1, help="Increase for recipe-only revisions")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate(args.archive.resolve(), args.output_dir.resolve(), version=args.version,
                          source_revision=args.source_revision, pkgrel=args.pkgrel)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"LightTable: {error}\n")
    print(f"Generated {result}, .SRCINFO and desktop entry. No files downloaded or published.")
    print("Publish the matching official release asset before submitting this recipe to the AUR.")


if __name__ == "__main__":
    main()
