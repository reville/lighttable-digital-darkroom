#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Release smoke: load the bundled model and denoise real tiles for real.

Runs the platform's real inference backend end to end: the Swift/Core ML
helper on macOS, onnxruntime on Windows and Linux, through the same
``run_model()`` entry point the app uses. There is no identity fallback
anywhere in this codebase, so a missing model or backend is a failure here,
not a skip.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

# Packagers supply their staged runtime through PYTHONPATH. Never accidentally
# replace that with source imports when checking a relocated application.
if not os.environ.get("PYTHONPATH"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import enhance_workflow


def _flat_field_tile(tile: int) -> np.ndarray:
    rng = np.random.default_rng(20260903)
    return np.clip(
        np.full((tile, tile, 3), 0.45, dtype=np.float32)
        + rng.normal(0.0, 0.08, (tile, tile, 3)).astype(np.float32),
        0.0, 1.0)


def _photo_tile(image_path: Path, tile: int) -> np.ndarray:
    from PIL import Image, ImageOps

    with Image.open(image_path) as source:
        rgb = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    height, width = rgb.shape[:2]
    y0 = max(0, (height - tile) // 2)
    x0 = max(0, (width - tile) // 2)
    crop = rgb[y0:y0 + tile, x0:x0 + tile]
    return crop.astype(np.float32) / 255.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path,
                        help="override LIGHTTABLE_MODEL_DIR for this process")
    parser.add_argument("--image", type=Path,
                        help="optional real photo; a synthetic flat noisy "
                             "field is used when omitted")
    parser.add_argument("--tile", type=int, default=enhance_workflow.DEFAULT_TILE)
    args = parser.parse_args()

    if args.model_dir:
        os.environ[enhance_workflow.MODEL_DIR_ENV] = str(args.model_dir)

    report = enhance_workflow.capabilities()
    if not report["modes"]["denoise"]:
        raise SystemExit(f"denoise is unavailable: {report['reason']}")

    source = (_photo_tile(args.image, args.tile) if args.image
              else _flat_field_tile(args.tile))
    result = enhance_workflow.run_model(
        source, "denoise", strength=1.0, tile=args.tile, overlap=64)

    if result.shape != source.shape or not np.isfinite(result).all():
        raise SystemExit("bundled denoise returned an invalid tile")
    if result.min() < 0 or result.max() > 1:
        raise SystemExit("bundled denoise returned out-of-range values")
    before = float(np.std(source))
    after = float(np.std(result))
    if args.image is None and after >= before * 0.5:
        raise SystemExit(
            f"bundled denoise did not halve flat-field noise ({before} -> {after})")
    residual = source - result
    mse = float(np.mean(residual ** 2))
    psnr_vs_input = float("inf") if mse == 0 else 10 * np.log10(1.0 / mse)

    print(json.dumps({
        "ok": True,
        "platform": sys.platform,
        "backend": report["helper"]["path"],
        "model": report["models"]["denoise"]["identity"],
        "shape": list(result.shape),
        "noiseStdBefore": round(before, 6),
        "noiseStdAfter": round(after, 6),
        "psnrVsInput": round(psnr_vs_input, 2),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
