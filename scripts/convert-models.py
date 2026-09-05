#!/usr/bin/env python3
"""Convert fetched SCUNet weights into a Core ML package for Enhance.

Run after `scripts/fetch-models.py`, using the pinned conversion environment in
`scripts/models/requirements-convert.txt`. Nothing from that environment enters
the app runtime; the resulting `.mlpackage`, provenance index, and licences are
copied into macOS release bundles.

The conversion refuses to write anything until it has proved the traceable
network is bit-identical to the published one, and it reports the error of the
converted model against the PyTorch reference so a precision choice is a
measurement rather than a guess.

fp16 is the default because the package is half the size of fp32. Conversion
prints the measured first-call time and numerical error for the current Core
ML backend; those values are deliberately not hard-coded here because Core ML
may choose a different device across machines and OS releases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

TILE = 512
CONFIG = [4] * 7          # the architecture these weights were trained with
DIM = 64
SCUNET_REVISION = "52e440a80a655b01e0b41e9dd9bfe599bc11625e"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_digest(path: Path) -> str:
    """Stable digest of every path and byte in a Core ML package."""
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*")
                       if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def build_network(source: Path, weights: Path, tile: int):
    """Load SCUNet with the published weights, verified key-for-key."""
    import torch

    sys.path.insert(0, str(source))
    from network_scunet import SCUNet  # noqa: E402  (path set above)

    model = SCUNet(in_nc=3, config=CONFIG, dim=DIM, input_resolution=tile)
    state = torch.load(weights, map_location="cpu")
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise SystemExit(
            f"{weights.name} does not match the expected architecture: "
            f"{len(missing)} missing and {len(unexpected)} unexpected tensors")
    return model.eval()


class FixedTile:
    """Marker for the wrapper defined inside `convert` (needs torch loaded)."""


def convert(source: Path, weights: Path, destination: Path, *,
            tile: int = TILE, precision: str = "fp16") -> None:
    import numpy as np
    import torch
    import torch.nn as nn
    import coremltools as ct

    sys.path.insert(0, str(Path(__file__).resolve().parent / "models"))
    import scunet_traceable

    published = build_network(source, weights, tile)
    model = build_network(source, weights, tile)
    example = torch.rand(1, 3, tile, tile)
    patched = scunet_traceable.apply(model, example)
    print(f"made {patched} blocks traceable")

    # The gate: prove nothing changed before converting anything.
    torch.manual_seed(0)
    for trial in range(3):
        probe = torch.rand(1, 3, tile, tile)
        with torch.no_grad():
            expected, actual = published(probe), model(probe)
        if not torch.equal(expected, actual):
            raise SystemExit(
                f"the traceable network diverges from the published one "
                f"(trial {trial}, max {float((expected-actual).abs().max())})")
    print("traceable network is bit-identical to the published one")

    class Wrapped(nn.Module):
        """Drops SCUNet's dynamic padding, which is zero at a 64-multiple tile.

        The pad size is computed with a Python int() that cannot be traced.
        Asserted equal below rather than assumed.
        """

        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, x0):
            m = self.inner
            x1 = m.m_head(x0)
            x2 = m.m_down1(x1)
            x3 = m.m_down2(x2)
            x4 = m.m_down3(x3)
            x = m.m_body(x4)
            x = m.m_up3(x + x4)
            x = m.m_up2(x + x3)
            x = m.m_up1(x + x2)
            return m.m_tail(x + x1)

    if tile % 64:
        raise SystemExit("the tile size must be a multiple of 64")
    wrapped = Wrapped(model).eval()
    with torch.no_grad():
        if not torch.equal(model(example), wrapped(example)):
            raise SystemExit("the tile wrapper changed the result")
        reference = wrapped(example)

    traced = torch.jit.trace(wrapped, example)
    mlmodel = ct.convert(
        traced,
        inputs=[ct.TensorType(name="image", shape=(1, 3, tile, tile),
                              dtype=np.float32)],
        outputs=[ct.TensorType(name="output", dtype=np.float32)],
        compute_precision=(ct.precision.FLOAT16 if precision == "fp16"
                           else ct.precision.FLOAT32),
        minimum_deployment_target=ct.target.macOS13,
    )
    mlmodel.short_description = (
        "SCUNet blind denoiser. Network Apache-2.0 (cszn/SCUNet), weights MIT "
        "(Kai Zhang, cszn/KAIR v1.0). Trained on synthesised degradations, not "
        "on SIDD or DND.")
    mlmodel.author = "Kai Zhang; converted for LightTable"
    mlmodel.license = "Apache-2.0 (code) / MIT (weights)"
    destination.parent.mkdir(parents=True, exist_ok=True)
    mlmodel.save(str(destination))
    digest = package_digest(destination)
    index_path = destination.parent / "models.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {}
    index["denoise"] = {
        "version": f"SCUNet {SCUNET_REVISION[:7]} {precision}",
        "license": "Apache-2.0 code / MIT weights",
        "source": "https://github.com/cszn/SCUNet",
        "sha256": digest,
        "weightsSha256": file_digest(weights),
        "input": int(tile),
        "precision": precision,
    }
    staged_index = index_path.with_suffix(".json.part")
    staged_index.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    staged_index.replace(index_path)
    for name in ("SCUNet-CODE-LICENSE.txt", "SCUNet-WEIGHTS-LICENSE.txt"):
        license_path = source / name
        if not license_path.is_file():
            raise SystemExit(f"{license_path} is missing; refetch model sources")
        shutil.copy2(license_path, destination.parent / name)

    started = time.perf_counter()
    produced = mlmodel.predict({"image": example.numpy()})["output"]
    elapsed = (time.perf_counter() - started) * 1000
    error = np.abs(produced - reference.numpy())
    size = subprocess.run(["du", "-sm", str(destination)],
                          capture_output=True, text=True).stdout.split()[0]
    print(f"wrote {destination}")
    print(f"  {size} MB | {elapsed:.0f} ms per {tile} tile | "
          f"max error {error.max()*255:.3f}/255 | "
          f"mean {error.mean()*255:.4f}/255")
    if error.max() > 0.01:
        print("  note: fp16 rounding; visually lossless but not bit-exact")


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=here / "models" / "download",
                        help="directory holding the fetched weights and "
                             "network_scunet.py")
    parser.add_argument("--weights", default="scunet_color_real_psnr.pth")
    parser.add_argument("--out", type=Path, default=None,
                        help="destination .mlpackage")
    parser.add_argument("--tile", type=int, default=TILE)
    parser.add_argument("--precision", choices=("fp16", "fp32"),
                        default="fp16")
    arguments = parser.parse_args()

    weights = arguments.source / arguments.weights
    if not weights.is_file():
        raise SystemExit(f"{weights} is missing; run scripts/fetch-models.py")
    destination = arguments.out or (arguments.source.parent / "denoise.mlpackage")
    convert(arguments.source, weights, destination,
            tile=arguments.tile, precision=arguments.precision)


if __name__ == "__main__":
    main()
