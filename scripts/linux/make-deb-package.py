#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate a Debian/Ubuntu (.deb) package from a verified Linux bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile


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


def write_ar_archive(output_path: Path, members: list[tuple[str, bytes]]) -> None:
    """Write an ar archive format compliant with Debian (.deb) specifications."""
    with output_path.open("wb") as out:
        out.write(b"!<arch>\n")
        for name, data in members:
            header_name = f"{name}".ljust(16)
            timestamp = "0".ljust(12)
            owner = "0".ljust(6)
            group = "0".ljust(6)
            mode = "100644".ljust(8)
            size = str(len(data)).ljust(10)
            trailer = "`\n"
            header = f"{header_name}{timestamp}{owner}{group}{mode}{size}{trailer}".encode("ascii")
            out.write(header)
            out.write(data)
            if len(data) % 2 != 0:
                out.write(b"\n")


def stage_debian_tree(archive: Path, stage_dir: Path, manifest: dict, pkgrel: int = 1) -> None:
    """Stage filesystem hierarchy and DEBIAN control files."""
    architecture = manifest["architecture"]
    deb_arch = {"x86_64": "amd64", "aarch64": "arm64"}[architecture]
    version = manifest["version"].replace("-", ".")

    opt_dir = stage_dir / "opt/lighttable"
    bin_dir = stage_dir / "usr/bin"
    apps_dir = stage_dir / "usr/share/applications"
    icons_dir = stage_dir / "usr/share/icons/hicolor/1024x1024/apps"
    doc_dir = stage_dir / "usr/share/doc/lighttable"
    debian_dir = stage_dir / "DEBIAN"

    for d in (opt_dir, bin_dir, apps_dir, icons_dir, doc_dir, debian_dir):
        d.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.name.startswith("LightTable/"):
                rel = member.name[len("LightTable/"):]
                if not rel:
                    continue
                target = opt_dir / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(member.mode)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)

    (opt_dir / "installation-owner.json").write_text('{"owner":"deb"}\n')
    for script in ("install.sh", "uninstall.sh", "desktop-integration.py"):
        (opt_dir / script).unlink(missing_ok=True)

    # Symlinks
    (bin_dir / "lighttable").symlink_to("/opt/lighttable/bin/lighttable")
    (bin_dir / "lighttable-desktop").symlink_to("/opt/lighttable/bin/lighttable-desktop")

    # Desktop entry and icon
    desktop = DESKTOP.desktop_entry(Path("/opt/lighttable"))
    (apps_dir / (DESKTOP.APP_ID + ".desktop")).write_text(desktop)
    if (opt_dir / "share/icons/lighttable.png").is_file():
        shutil.copy2(opt_dir / "share/icons/lighttable.png",
                     icons_dir / (DESKTOP.APP_ID + ".png"))

    if (opt_dir / "LICENSE").is_file():
        shutil.copy2(opt_dir / "LICENSE", doc_dir / "copyright")

    # Render control and maintainer scripts
    control_template = (ROOT / "packaging/linux/deb/control.in").read_text()
    control_content = (control_template
                       .replace("@PKGVER@", version)
                       .replace("@PKGREL@", str(pkgrel))
                       .replace("@DEB_ARCH@", deb_arch))
    (debian_dir / "control").write_text(control_content)

    for script_name in ("postinst", "prerm"):
        src = ROOT / f"packaging/linux/deb/{script_name}.in"
        if src.is_file():
            dst = debian_dir / script_name
            dst.write_text(src.read_text())
            dst.chmod(0o755)


def generate(archive: Path, output_dir: Path, *, pkgrel: int = 1,
             experimental_aarch64: bool = False) -> Path:
    manifest = archive_metadata(archive)
    architecture = manifest["architecture"]
    if architecture == "aarch64" and not experimental_aarch64:
        raise ValueError("ARM64 requires --experimental-aarch64; standard DEB release target is x86_64")
    if architecture == "x86_64" and experimental_aarch64:
        raise ValueError("--experimental-aarch64 cannot label an x86_64 archive")
    archive_sha256(archive)

    version = manifest["version"].replace("-", ".")
    deb_arch = {"x86_64": "amd64", "aarch64": "arm64"}[architecture]
    deb_name = f"lighttable_{version}-{pkgrel}_{deb_arch}.deb"
    output_dir.mkdir(parents=True, exist_ok=True)
    target_deb = output_dir / deb_name

    with tempfile.TemporaryDirectory(prefix="lighttable-deb-build-") as temp:
        stage_dir = Path(temp) / "stage"
        stage_debian_tree(archive, stage_dir, manifest, pkgrel=pkgrel)

        if shutil.which("dpkg-deb"):
            subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(stage_dir), str(target_deb)],
                           check=True)
        else:
            # Pure Python fallback for environments without dpkg-deb (e.g. macOS)
            control_buf = io.BytesIO()
            with tarfile.open(fileobj=control_buf, mode="w:gz") as tar:
                for item in (stage_dir / "DEBIAN").iterdir():
                    tar.add(item, arcname="./" + item.name)

            data_buf = io.BytesIO()
            with tarfile.open(fileobj=data_buf, mode="w:gz") as tar:
                for item in stage_dir.iterdir():
                    if item.name != "DEBIAN":
                        tar.add(item, arcname="./" + item.name)

            members = [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", control_buf.getvalue()),
                ("data.tar.gz", data_buf.getvalue()),
            ]
            write_ar_archive(target_deb, members)

    return target_deb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Path to verified Linux bundle (.tar.gz)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/deb",
                        help="Directory to place generated .deb package")
    parser.add_argument("--pkgrel", type=int, default=1, help="Package release number")
    parser.add_argument("--experimental-aarch64", action="store_true",
                        help="Allow experimental aarch64 packaging")
    args = parser.parse_args()

    try:
        result = generate(args.archive.resolve(), args.output_dir.resolve(),
                          pkgrel=args.pkgrel,
                          experimental_aarch64=args.experimental_aarch64)
        print(f"Successfully generated: {result}")
    except Exception as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
