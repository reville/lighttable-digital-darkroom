#!/usr/bin/env python3
"""Repeatable CPU benchmark for HDR, panorama, and focus merging."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import merge_workflow  # noqa: E402


def fixtures(side: int) -> dict[str, list[np.ndarray]]:
    rng = np.random.default_rng(42)
    scene = ndimage.gaussian_filter(
        rng.random((side, side * 2, 3), dtype=np.float32), (1.0, 1.0, 0))
    base = scene[:, :side]
    hdr = [
        np.clip(ndimage.shift(base * exposure, (y, x, 0), order=1), 0, 1)
        .astype(np.float32)
        for exposure, y, x in ((0.55, 1, -1), (1.0, 0, 0), (1.6, -1, 1))
    ]
    panorama = [
        scene[:, index * side // 2:index * side // 2 + side].copy()
        for index in range(3)
    ]
    blurred = ndimage.gaussian_filter(base, (3.0, 3.0, 0))
    focus = []
    for index in range(4):
        frame = blurred.copy()
        start = max(0, index * side // 4 - side // 16)
        stop = min(side, index * side // 4 + side // 3)
        frame[start:stop] = base[start:stop]
        focus.append(frame)
    return {"hdr": hdr, "panorama": panorama, "focus": focus}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--widths", default="512,1024")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    widths = [int(value) for value in args.widths.split(",") if value.strip()]
    results = {"settings": {
        "alignmentMaxEdge": merge_workflow.ALIGNMENT_MAX_EDGE,
        "maxFeatures": merge_workflow.MAX_ALIGNMENT_FEATURES,
        "descriptorBlock": merge_workflow.DESCRIPTOR_MATCH_BLOCK,
        "panoramaTileEdge": merge_workflow.PANORAMA_TILE_EDGE,
    }, "widths": {}}
    for width in widths:
        values = fixtures(width)
        result = {}
        for mode, function in (
                ("hdr", merge_workflow.hdr_merge),
                ("panorama", merge_workflow.panorama_merge),
                ("focus", merge_workflow.focus_merge)):
            samples = []
            shape = None
            for _ in range(max(1, args.iterations)):
                started = time.perf_counter()
                merged = function(values[mode])
                samples.append((time.perf_counter() - started) * 1000)
                shape = list(merged.shape)
            result[mode] = {
                "medianMs": round(statistics.median(samples), 2),
                "samplesMs": [round(value, 2) for value in samples],
                "shape": shape,
            }
        results["widths"][str(width)] = result
    payload = json.dumps(results, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
