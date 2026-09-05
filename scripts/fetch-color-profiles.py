#!/usr/bin/env python3
"""Fetch the pinned, redistributable ICC profiles used outside macOS."""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path


REVISION = "bdd84663061bc4ae95ca70decff54f581e27f702"
BASE_URL = (
    "https://raw.githubusercontent.com/saucecontrol/Compact-ICC-Profiles/"
    f"{REVISION}/"
)
ASSETS = {
    "profiles/sRGB-v4.icc": (
        "sRGB-v4.icc",
        "c56e1685d888f5edb92fe07f2750f387f8fe8e91b32ff8fb0b56bfbbb9458353",
    ),
    "profiles/DisplayP3-v4.icc": (
        "DisplayP3-v4.icc",
        "cb51de38e482ee974c0c76b9689e16aad04bad16e226fed2f30c842d15ff3a3d",
    ),
    "profiles/ProPhoto-v4.icc": (
        "ProPhoto-v4.icc",
        "090daf740c136b4a63bf979d64f034b4a65aa5abbb04a0917729222afe2bb5c2",
    ),
    "license": (
        "color-profiles-CC0-1.0.txt",
        "469d87b5fa48f8aaf69e25f4fcac3a309c97dbb7d561b917638abdb640e004d9",
    ),
}


def fetch(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source, (filename, expected) in ASSETS.items():
        output = destination / filename
        if output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest() == expected:
            continue
        with urllib.request.urlopen(BASE_URL + source, timeout=60) as response:
            content = response.read()
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"hash mismatch for {filename}: expected {expected}, received {actual}")
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    fetch(args.destination.resolve())


if __name__ == "__main__":
    main()
