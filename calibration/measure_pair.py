#!/usr/bin/env python
"""Measure a rendered candidate against an aligned reference scan."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from skimage.transform import resize

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import color_pipeline  # noqa: E402


def _to_srgb(image: np.ndarray, source_space: str) -> np.ndarray:
    if source_space == "srgb":
        return image
    import colour

    converted = colour.RGB_to_RGB(
        image.astype(np.float64),
        color_pipeline.COLOUR_SPACE_NAMES[source_space],
        color_pipeline.COLOUR_SPACE_NAMES["srgb"],
        apply_cctf_decoding=True,
        apply_cctf_encoding=True,
    )
    return np.clip(converted, 0.0, 1.0).astype(np.float32)


def metrics(candidate_path: Path, reference_path: Path,
            candidate_space: str = "srgb",
            reference_space: str = "srgb") -> dict:
    import colour

    candidate = _to_srgb(
        color_pipeline.load_float_rgb(candidate_path), candidate_space)
    reference = _to_srgb(
        color_pipeline.load_float_rgb(reference_path), reference_space)
    if candidate.shape != reference.shape:
        reference = resize(reference, candidate.shape[:2], order=3,
                           anti_aliasing=True, preserve_range=True)
    candidate_xyz = colour.sRGB_to_XYZ(candidate.astype(np.float64))
    reference_xyz = colour.sRGB_to_XYZ(reference.astype(np.float64))
    candidate_lab = colour.XYZ_to_Lab(candidate_xyz)
    reference_lab = colour.XYZ_to_Lab(reference_xyz)
    delta = colour.delta_E(candidate_lab, reference_lab, method="CIE 2000")
    absolute = np.abs(candidate - reference) * 255.0
    candidate_luma = candidate @ np.array([0.2126, 0.7152, 0.0722])
    reference_luma = reference @ np.array([0.2126, 0.7152, 0.0722])
    return {
        "candidate": str(candidate_path),
        "reference": str(reference_path),
        "candidateSpace": color_pipeline.COLOUR_SPACE_NAMES[candidate_space],
        "referenceSpace": color_pipeline.COLOUR_SPACE_NAMES[reference_space],
        "shape": list(candidate.shape),
        "rgbMae255": round(float(absolute.mean()), 4),
        "rgbP95Error255": round(float(np.percentile(absolute, 95)), 4),
        "deltaE2000Mean": round(float(delta.mean()), 4),
        "deltaE2000Median": round(float(np.median(delta)), 4),
        "deltaE2000P95": round(float(np.percentile(delta, 95)), 4),
        "luminanceMae": round(float(np.abs(candidate_luma - reference_luma).mean()), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--output", type=Path)
    spaces = tuple(color_pipeline.COLOUR_SPACE_NAMES)
    parser.add_argument("--candidate-space", choices=spaces, default="srgb")
    parser.add_argument("--reference-space", choices=spaces, default="srgb")
    args = parser.parse_args()
    report = metrics(args.candidate, args.reference,
                     args.candidate_space, args.reference_space)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
