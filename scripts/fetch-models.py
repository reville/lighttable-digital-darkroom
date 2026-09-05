#!/usr/bin/env python3
"""Fetch the pinned denoising weights LightTable's Enhance feature can use.

Nothing downloaded here is committed. The weights are fetched for release
preparation, verified against a pinned hash, and stored with their licence
text beside them, following the same pattern as `fetch-color-profiles.py`.

The chosen model is SCUNet, published by Kai Zhang. It is preferred over the
better-known real-noise denoisers for two reasons that matter to a photo
editor accepting files from more than a thousand camera models:

  * it is trained on a synthesised degradation pipeline rather than on the
    SIDD or DND smartphone pairs, so it generalises to sensor noise it has
    never seen; and
  * it is blind, needing no noise-level input, which is what a single Denoise
    control wants.

Take the PSNR weights rather than the GAN ones: a photograph wants fidelity,
not invented texture.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/cszn/KAIR/releases/download/v1.0/"

# sha256 of each file as published, verified on 2026-09-03.
WEIGHTS = {
    "scunet_color_real_psnr.pth":
        "fa78899ba2caec9d235a900e91d96c689da71c42029230c2028b00f09f809c2e",
    "scunet_color_real_gan.pth":
        "892c83f812c59173273b74f4f34a14ecaf57a2fdb68df056664589beb55c966e",
}

# The network definition, pinned to the revision the conversion was verified
# against so an upstream change cannot silently alter the architecture.
SCUNET_REVISION = "52e440a80a655b01e0b41e9dd9bfe599bc11625e"
SOURCE_FILES = {
    "network_scunet.py": (
        "https://raw.githubusercontent.com/cszn/SCUNet/"
        f"{SCUNET_REVISION}/models/network_scunet.py",
        "77aeefd31e37080db7f0bf46bca5efcecc800fcfddb502081340a10b2b949c60",
    ),
}

LICENCES = {
    "SCUNet-CODE-LICENSE.txt":
        f"https://raw.githubusercontent.com/cszn/SCUNet/{SCUNET_REVISION}/LICENSE",
    "SCUNet-WEIGHTS-LICENSE.txt":
        "https://raw.githubusercontent.com/cszn/KAIR/master/LICENSE",
}

PROVENANCE = """SCUNet denoising model
======================

Network code : Apache License 2.0, cszn/SCUNet, revision {revision}
Weights      : MIT License, Kai Zhang, released in cszn/KAIR v1.0

Both licences permit redistribution and are one-way compatible with
LightTable's GPL-3.0. The licence texts are in this directory.

The model was trained on a synthesised degradation pipeline. Its authors state
explicitly that the SIDD and DND paired noisy/clean datasets were NOT used in
training.

These source files were downloaded by `scripts/fetch-models.py` and verified
against pinned hashes. Release builds ship the converted fp16 Core ML package,
its provenance index, and both licence texts, not the PyTorch source weights.
"""


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def fetch(url: str, target: Path, expected: str | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and expected and digest(target) == expected:
        print(f"  have  {target.name}")
        return
    print(f"  get   {target.name}")
    staged = target.with_name(f".{target.name}.{os.getpid()}.part")
    try:
        with urllib.request.urlopen(url, timeout=300) as response, \
                staged.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if expected:
            actual = digest(staged)
            if actual != expected:
                raise SystemExit(
                    f"{target.name} does not match its pinned hash\n"
                    f"  expected {expected}\n  got      {actual}")
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--into", type=Path,
                        default=Path(__file__).resolve().parent / "models"
                        / "download",
                        help="where to place the downloaded files")
    parser.add_argument("--gan", action="store_true",
                        help="also fetch the GAN weights (not recommended for "
                             "photographs: it invents texture)")
    arguments = parser.parse_args()
    target = arguments.into
    target.mkdir(parents=True, exist_ok=True)

    print(f"Fetching into {target}")
    wanted = dict(WEIGHTS)
    if not arguments.gan:
        wanted.pop("scunet_color_real_gan.pth", None)
    for name, expected in wanted.items():
        fetch(RELEASE + name, target / name, expected)
    for name, (url, expected) in SOURCE_FILES.items():
        fetch(url, target / name, expected)
    for name, url in LICENCES.items():
        fetch(url, target / name)
    (target / "PROVENANCE.txt").write_text(
        PROVENANCE.format(revision=SCUNET_REVISION))
    print("Done. Convert with scripts/convert-models.py")


if __name__ == "__main__":
    main()
