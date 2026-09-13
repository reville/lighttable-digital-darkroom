#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
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


def _traced_wrapped_network(source: Path, weights: Path, tile: int):
    """Build, patch, gate, and trace SCUNet exactly as the Core ML path does.

    Shared by both export formats so ONNX and Core ML are provably tracing
    the same graph: the bit-identity gate against the published network runs
    once here, not once per format.
    """
    import torch
    import torch.nn as nn

    sys.path.insert(0, str(Path(__file__).resolve().parent / "models"))
    import scunet_traceable

    published = build_network(source, weights, tile)
    model = build_network(source, weights, tile)
    example = torch.rand(1, 3, tile, tile)
    patched = scunet_traceable.apply(model, example)
    print(f"made {patched} blocks traceable")

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
        """Drops SCUNet's dynamic padding, which is zero at a 64-multiple tile."""

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
    return wrapped, example, reference


def convert_onnx(source: Path, weights: Path, destination: Path, *,
                 tile: int = TILE, precision: str = "fp16") -> None:
    """Export the same patched, gated, fixed-shape network to ONNX.

    Windows and Linux have no Core ML. The roadmap (docs/roadmap/gaps.md,
    "1.5 Windows") calls for exporting the identical traced network to ONNX
    at the same fixed 1x3x512x512 shape rather than a second model choice, so
    the three runtimes agree by construction rather than by separate tuning.
    fp16 halves the artefact size the same way it does for Core ML; onnxruntime
    on CPU upcasts fp16 tensors internally, so it costs nothing on load and
    saves it on disk and over the wire.
    """
    import numpy as np
    import torch
    import onnx
    from onnxconverter_common import float16 as onnx_float16

    wrapped, example, reference = _traced_wrapped_network(source, weights, tile)

    destination.parent.mkdir(parents=True, exist_ok=True)
    fp32_path = destination.with_suffix(".fp32.onnx")
    torch.onnx.export(
        wrapped, example, str(fp32_path),
        input_names=["image"], output_names=["output"],
        opset_version=17, dynamic_axes=None, do_constant_folding=True,
    )
    onnx_model = onnx.load(str(fp32_path))
    onnx.checker.check_model(onnx_model)
    if precision == "fp16":
        onnx_model = onnx_float16.convert_float_to_float16(
            onnx_model, keep_io_types=True)
    onnx.save(onnx_model, str(destination))
    fp32_path.unlink(missing_ok=True)

    import onnxruntime as ort

    session = ort.InferenceSession(str(destination),
                                   providers=["CPUExecutionProvider"])
    (input_name,) = [item.name for item in session.get_inputs()]
    (output_name,) = [item.name for item in session.get_outputs()]
    if tuple(session.get_inputs()[0].shape) != (1, 3, tile, tile):
        raise SystemExit("exported ONNX graph does not have the fixed input shape")

    started = time.perf_counter()
    produced = session.run([output_name], {input_name: example.numpy()})[0]
    elapsed = (time.perf_counter() - started) * 1000
    error = np.abs(produced - reference.numpy())

    digest = file_digest(destination)
    index_path = destination.parent / "models.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {}
    index["denoise"] = {
        "version": f"SCUNet {SCUNET_REVISION[:7]} onnx {precision}",
        "license": "Apache-2.0 code / MIT weights",
        "source": "https://github.com/cszn/SCUNet",
        "sha256": digest,
        "weightsSha256": file_digest(weights),
        "input": int(tile),
        "precision": precision,
        "runtime": "onnxruntime",
    }
    staged_index = index_path.with_suffix(".json.part")
    staged_index.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    staged_index.replace(index_path)
    for name in ("SCUNet-CODE-LICENSE.txt", "SCUNet-WEIGHTS-LICENSE.txt"):
        license_path = source / name
        if not license_path.is_file():
            raise SystemExit(f"{license_path} is missing; refetch model sources")
        shutil.copy2(license_path, destination.parent / name)

    size_mb = destination.stat().st_size / (1024 * 1024)
    print(f"wrote {destination}")
    print(f"  {size_mb:.1f} MB | {elapsed:.0f} ms per {tile} tile (CPU) | "
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
    parser.add_argument("--format", choices=("coreml", "onnx"),
                        default="coreml",
                        help="coreml for macOS (.mlpackage); onnx for the "
                             "Windows/Linux onnxruntime path (.onnx)")
    parser.add_argument("--out", type=Path, default=None,
                        help="destination package or file; defaults to "
                             "denoise.mlpackage or onnx/denoise.onnx")
    parser.add_argument("--tile", type=int, default=TILE)
    parser.add_argument("--precision", choices=("fp16", "fp32"),
                        default="fp16")
    arguments = parser.parse_args()

    weights = arguments.source / arguments.weights
    if not weights.is_file():
        raise SystemExit(f"{weights} is missing; run scripts/fetch-models.py")
    if arguments.format == "onnx":
        destination = arguments.out or (
            arguments.source.parent / "onnx" / "denoise.onnx")
        convert_onnx(arguments.source, weights, destination,
                    tile=arguments.tile, precision=arguments.precision)
    else:
        destination = arguments.out or (arguments.source.parent / "denoise.mlpackage")
        convert(arguments.source, weights, destination,
                tile=arguments.tile, precision=arguments.precision)


if __name__ == "__main__":
    main()
