#!/usr/bin/env python3
"""Prepare a verified upstream Flatpak candidate; not a Flathub source build."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tarfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("lighttable_archive", ROOT / "scripts/linux/make-arch-package.py")
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)
APP_ID = "app.lighttable.LightTable"


def generate(archive: Path, output: Path, *, version: str, source_revision: str, sha256: str) -> Path:
    metadata = ARCHIVE.release_metadata(archive, version=version, source_revision=source_revision)
    actual = ARCHIVE.archive_sha256(archive)
    if actual != sha256:
        raise ValueError("Archive bytes do not match the separately supplied SHA256")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, output / "LightTable.tar.gz")
    files = ["install-candidate.py", "lighttable-desktop", "lighttable-cli", "sandbox-smoke.py"]
    for name in files:
        shutil.copy2(ROOT / "scripts/flatpak" / name, output / name)
    shutil.copy2(ROOT / "scripts/linux/desktop-smoke.py", output / "desktop-smoke.py")
    for name in (APP_ID + ".desktop", APP_ID + ".metainfo.xml"):
        shutil.copy2(ROOT / "packaging/flatpak" / name, output / name)
    metainfo = ET.parse(output / (APP_ID + ".metainfo.xml"))
    metainfo.find("./releases/release").set("version", version)
    metainfo.write(output / (APP_ID + ".metainfo.xml"), encoding="UTF-8", xml_declaration=True)
    provenance = {"format": 1, "distribution": "upstream-direct-flatpak-candidate",
                  "flathub_source_build": False, "app_id": APP_ID,
                  "bundle_sha256": actual, "bundle_manifest": metadata}
    (output / "candidate-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    manifest = json.loads((ROOT / "packaging/flatpak/direct-bundle.json.in").read_text())
    module = {"name": "lighttable-bundle", "buildsystem": "simple",
              "build-commands": ["python3 install-candidate.py"],
              "sources": [{"type": "archive", "path": "LightTable.tar.gz",
                           "sha256": actual, "strip-components": 0}]}
    for name in files + ["desktop-smoke.py", APP_ID + ".desktop", APP_ID + ".metainfo.xml", "candidate-provenance.json"]:
        module["sources"].append({"type": "file", "path": name,
            "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()})
    manifest["modules"].append(module)
    target = output / (APP_ID + ".json")
    target.write_text(json.dumps(manifest, indent=4) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        target = generate(args.bundle.resolve(), args.output_dir.resolve(), version=args.version,
                          source_revision=args.source_revision, sha256=args.sha256)
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        parser.exit(1, f"LightTable: {error}\n")
    print(f"Generated upstream-only candidate: {target}")
    print("This wraps a prebuilt runtime and is not eligible as a Flathub source-build submission.")


if __name__ == "__main__":
    main()
