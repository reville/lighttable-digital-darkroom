#!/usr/bin/env python3
"""Prepare a strict-confinement Snap candidate from a verified Linux release bundle.

This generates a local Snapcraft project. It does not build, register or publish
a snap. Candidates remain grade: devel until native confinement tests pass.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("lighttable_arch", ROOT / "scripts/linux/make-arch-package.py")
ARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCH)


def generate(archive: Path, output: Path, *, version: str, source_revision: str) -> Path:
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Bundle must be a regular file")
    manifest = ARCH.release_metadata(archive, version=version, source_revision=source_revision)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Output directory must be new or empty and cannot be a symlink")
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.exists() and (not sidecar.read_text().split() or sidecar.read_text().split()[0] != checksum):
        raise ValueError("Bundle checksum does not match its .sha256 file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lighttable-snap-", dir=output.parent) as temporary:
        staged = Path(temporary) / "project"
        payload = staged / "payload"
        payload.mkdir(parents=True)
        # archive_metadata checks all members and links. The data filter also
        # rejects link-mediated escapes and strips privileged modes on extraction.
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(payload, filter="data")
        bundle = payload / "LightTable"
        for name in ("install.sh", "uninstall.sh", "desktop-integration.py"):
            (bundle / name).unlink(missing_ok=True)
        applications = bundle / "share/applications"
        applications.mkdir(parents=True, exist_ok=True)
        (applications / "app.lighttable.LightTable.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=LightTable\n"
            "Comment=RAW photo editing and film simulation\n"
            "Exec=lighttable %u\nIcon=${SNAP}/LightTable/share/icons/lighttable.png\n"
            "Terminal=false\nCategories=Graphics;Photography;\n"
            "MimeType=x-scheme-handler/lighttable;\n"
            "StartupWMClass=app.lighttable.LightTable\nStartupNotify=true\n")
        commands = payload / "bin"
        commands.mkdir()
        environment = commands / "lighttable-snap-environment"
        shutil.copy2(ROOT / "packaging/snap/environment.sh", environment)
        environment.chmod(0o755)
        (staged / "snapcraft.yaml").write_text(
            (ROOT / "packaging/snap/snapcraft.yaml.in").read_text().replace("@VERSION@", version))
        (staged / "candidate.json").write_text(json.dumps({
            "version": version, "source_revision": source_revision,
            "archive": archive.name, "sha256": checksum,
            "architecture": manifest["architecture"], "store_published": False,
            "validation": "Native strict-confinement import/edit/export/upgrade testing required",
        }, indent=2) + "\n")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError("Bundle changed while preparing Snap project")
        if output.exists():
            output.rmdir()
        staged.rename(output)
    return output / "snapcraft.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate(args.archive, args.output_dir, version=args.version, source_revision=args.source_revision)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(1, f"LightTable: {error}\n")
    print(f"Prepared {result}; build on Ubuntu 24.04 amd64 with snapcraft pack.")
    print("This is an unpublished development candidate. Native confinement validation remains required.")


if __name__ == "__main__":
    main()
