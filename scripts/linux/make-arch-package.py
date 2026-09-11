#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate a checksum-pinned local PKGBUILD from an existing Linux bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("desktop_integration", ROOT / "scripts/linux/desktop-integration.py")
DESKTOP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DESKTOP)


def archive_metadata(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as archive:
        seen = set()
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "LightTable":
                raise ValueError(f"Unsafe or unexpected bundle member: {member.name}")
            if str(name) in seen:
                raise ValueError(f"Duplicate bundle member: {member.name}")
            seen.add(str(name))
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f"Unsupported bundle member: {member.name}")
            if member.issym() or member.islnk():
                target = (posixpath.join(str(name.parent), member.linkname)
                          if member.issym() else member.linkname)
                if not posixpath.normpath(target).startswith("LightTable/"):
                    raise ValueError(f"Bundle link escapes its root: {member.name}")
        manifest_file = archive.getmember("LightTable/build-manifest.json")
        if not manifest_file.isfile() or manifest_file.size > 65536:
            raise ValueError("Invalid bundle manifest")
        manifest = json.load(archive.extractfile(manifest_file))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid bundle manifest")
        architecture = manifest.get("architecture")
        if manifest.get("platform") != "linux" or architecture not in ("x86_64", "aarch64"):
            raise ValueError("Expected an x86_64 or aarch64 Linux bundle")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?", manifest.get("version", "")):
            raise ValueError("Invalid bundle version")
        expected_machine = {"x86_64": 62, "aarch64": 183}[architecture]
        for binary in ("bin/lighttable-desktop-shell", "Resources/LightTable/engine/lighttable-engine",
                       "Resources/LightTable/engine/spektrafilm-rs"):
            if not archive.getmember("LightTable/" + binary).isfile():
                raise ValueError(f"Expected regular native binary: {binary}")
            stream = archive.extractfile("LightTable/" + binary)
            header = stream.read(20)
            if (header[:6] != b"\x7fELF\x02\x01" or len(header) < 20
                    or int.from_bytes(header[18:20], "little") != expected_machine):
                raise ValueError(f"Native binary does not match manifest architecture: {binary}")
    return manifest


def release_metadata(archive: Path, *, version: str, source_revision: str) -> dict:
    """Validate a stable x86_64 release against its separately pinned identity."""
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Release version must be canonical major.minor.patch, without a CI suffix")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("Release source revision must be a full lowercase Git SHA")
    manifest = archive_metadata(archive)
    if manifest["version"] != version:
        raise ValueError("Bundle version does not match the requested release")
    if manifest.get("source_dirty") is not False:
        raise ValueError("A release bundle must have a clean source manifest")
    if manifest.get("source_revision") != source_revision:
        raise ValueError("Bundle source revision does not match the requested release")
    if manifest["architecture"] != "x86_64":
        raise ValueError("Official release packaging requires x86_64")
    return manifest


def archive_sha256(archive: Path) -> str:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.is_file():
        recorded = sidecar.read_text().split()
        if not recorded or recorded[0].lower() != digest:
            raise ValueError("Archive checksum does not match its .sha256 file")
    return digest


def write_recipe(output: Path, manifest: dict, digest: str, *, source: str,
                 pkgrel: int = 1, release: bool = False) -> Path:
    if type(pkgrel) is not int or pkgrel < 1:
        raise ValueError("pkgrel must be a positive integer")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty")
    architecture = manifest["architecture"]
    desktop = DESKTOP.desktop_entry(Path("/opt/lighttable"))
    values = {
        "PKGVER": manifest["version"].replace("-", "_"), "PKGREL": str(pkgrel), "ARCH": architecture,
        "DESCRIPTION": "Photo editing and film simulation" + (" (experimental ARM64)" if architecture == "aarch64" else ""),
        "ARCHIVE": source, "ARCHIVE_SHA": digest,
        "DESKTOP_SHA": hashlib.sha256(desktop.encode()).hexdigest(),
    }
    recipe = (ROOT / "packaging/linux/arch/PKGBUILD.in").read_text()
    for key, value in values.items():
        recipe = recipe.replace("@" + key + "@", shlex.quote(value))
    if release:
        recipe = recipe.replace("# Generated from a verified local Linux bundle; no AUR or remote release is assumed.",
                                "# Generated from a verified official release archive; SHA-256 pins its exact bytes.")
    output.mkdir(parents=True, exist_ok=True)
    target = output / "PKGBUILD"
    target.write_text(recipe)
    (output / (DESKTOP.APP_ID + ".desktop")).write_text(desktop)
    # Emit the same fixed package metadata from the generated, trusted template,
    # so the AUR index cannot drift from the recipe's dependency/source arrays.
    result = subprocess.run(["bash", "-e", "-c", '''
source "$1"
printf 'pkgbase = %s\\n' "$pkgname"
for field in pkgdesc pkgver pkgrel url arch license depends optdepends provides conflicts options source sha256sums; do
  reference="$field[@]"
  for value in "${!reference}"; do printf '\\t%s = %s\\n' "$field" "$value"; done
done
printf '\\npkgname = %s\\n' "$pkgname"
''', "srcinfo", str(target.resolve())], text=True, capture_output=True, check=True)
    (output / ".SRCINFO").write_text(result.stdout)
    return target


def generate(archive: Path, output: Path, *, experimental_aarch64: bool = False) -> Path:
    manifest = archive_metadata(archive)
    architecture = manifest["architecture"]
    if architecture == "aarch64" and not experimental_aarch64:
        raise ValueError("ARM64 requires --experimental-aarch64; Omarchy's supported target is x86_64")
    if architecture == "x86_64" and experimental_aarch64:
        raise ValueError("--experimental-aarch64 cannot label an x86_64 archive")
    digest = archive_sha256(archive)
    version = manifest["version"]
    name = f"LightTable-{version}-linux-{architecture}.tar.gz"
    target = write_recipe(output, manifest, digest, source=name)
    shutil.copy2(archive, output / name)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--experimental-aarch64", action="store_true")
    args = parser.parse_args()
    try:
        path = generate(args.archive.resolve(), args.output_dir.resolve(), experimental_aarch64=args.experimental_aarch64)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"LightTable: {error}\n")
    print(f"Generated {path}. On a matching Arch Linux system, run makepkg -s in that directory.")
    print("Install the resulting package with pacman -U. No files were published or installed.")


if __name__ == "__main__":
    main()
