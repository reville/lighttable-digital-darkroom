#!/usr/bin/env python3
"""Assemble the standalone Flathub submission package for app.lighttable.LightTable.

This script copies all local files into the submission package directory,
rewrites the manifest file source paths to be local, recomputes SHA-256 digests,
and creates flathub.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / 'packaging/flatpak'
SCRIPTS = ROOT / 'scripts/flatpak'
DEFAULT_OUTPUT = ROOT / 'packaging/flathub/app.lighttable.LightTable'

COMPANION_FILES = [
    (SCRIPTS / 'source-preflight.py', 'source-preflight.py'),
    (PACKAGING / 'source-status.json', 'source-status.json'),
    (SCRIPTS / 'source-wheel.py', 'source-wheel.py'),
    (PACKAGING / 'source-pcodec-Cargo.lock', 'source-pcodec-Cargo.lock'),
    (PACKAGING / 'source-cargo-c-Cargo.lock', 'source-cargo-c-Cargo.lock'),
    (PACKAGING / 'source-rav1e-Cargo.lock', 'source-rav1e-Cargo.lock'),
    (SCRIPTS / 'source-imagecodecs.py', 'source-imagecodecs.py'),
    (SCRIPTS / 'source-codec-check.py', 'source-codec-check.py'),
    (SCRIPTS / 'source-install.py', 'source-install.py'),
    (SCRIPTS / 'lighttable-desktop', 'lighttable-desktop'),
    (SCRIPTS / 'lighttable-cli', 'lighttable-cli'),
    (PACKAGING / 'app.lighttable.LightTable.desktop', 'app.lighttable.LightTable.desktop'),
    (PACKAGING / 'app.lighttable.LightTable.metainfo.xml', 'app.lighttable.LightTable.metainfo.xml'),
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assemble(manifest_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text())

    # Copy companion files into output directory
    copied = {}
    for src, name in COMPANION_FILES:
        dst = output_dir / name
        shutil.copy2(src, dst)
        copied[name] = sha256_file(dst)

    # Localize file sources in manifest
    def localize_sources(modules):
        for module in modules:
            localize_sources(module.get('modules', []))
            for source in module.get('sources', []):
                if source.get('type') == 'file' and 'path' in source:
                    filename = Path(source['path']).name
                    if filename not in copied:
                        raise ValueError(f"Unmapped local file source: {source['path']} (name={filename})")
                    source['path'] = filename
                    source['sha256'] = copied[filename]

    localize_sources(manifest.get('modules', []))

    # Write localized manifest as app.lighttable.LightTable.json
    out_manifest = output_dir / 'app.lighttable.LightTable.json'
    out_manifest.write_text(json.dumps(manifest, indent=2) + '\n')

    # Create flathub.json
    flathub_cfg = {
        'only-arches': ['x86_64']
    }
    (output_dir / 'flathub.json').write_text(json.dumps(flathub_cfg, indent=2) + '\n')

    return out_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=PACKAGING / 'source-candidate.json')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    out_manifest = assemble(args.manifest.resolve(), args.output.resolve())
    print(f"Flathub submission package assembled in: {args.output}")
    print(f"Manifest: {out_manifest}")


if __name__ == '__main__':
    main()
