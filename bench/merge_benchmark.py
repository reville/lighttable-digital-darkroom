#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Repeatable CPU/GPU benchmark for HDR, panorama, and focus merging."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
    parser.add_argument("--backends", default="gpu", help="cpu,gpu for an interleaved comparison")
    parser.add_argument("--warmup", type=int, default=1, help="untimed iterations per backend/mode")
    parser.add_argument("--image", type=Path, help="derive synthetic brackets from an RGB photograph")
    parser.add_argument("--baseline-source", type=Path, help="original merge_workflow.py for before/after checks")
    args = parser.parse_args()
    widths = [int(value) for value in args.widths.split(",") if value.strip()]
    backends = [value.strip() for value in args.backends.split(",")]
    if any(value not in {"baseline", "cpu", "gpu"} for value in backends):
        parser.error("--backends must contain baseline, cpu and/or gpu")
    baseline = None
    if "baseline" in backends:
        if not args.baseline_source:
            parser.error("baseline backend requires --baseline-source")
        spec = importlib.util.spec_from_file_location("merge_benchmark_reference", args.baseline_source)
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline
        spec.loader.exec_module(baseline)
        old_ransac = baseline.ransac
        baseline.ransac = lambda *a, **kw: old_ransac(*a, **kw, rng=42)
    if args.image:
        from PIL import Image
        photo = np.asarray(Image.open(args.image).convert("RGB"), dtype=np.float32) / 255.0
    else:
        photo = None
    results = {"settings": {
        "fixture": str(args.image) if args.image else "seeded synthetic texture",
        "warmup": args.warmup,
        "iterations": args.iterations,
        "alignmentMaxEdge": merge_workflow.ALIGNMENT_MAX_EDGE,
        "maxFeatures": merge_workflow.MAX_ALIGNMENT_FEATURES,
        "descriptorBlock": merge_workflow.DESCRIPTOR_MATCH_BLOCK,
        "panoramaTileEdge": merge_workflow.PANORAMA_TILE_EDGE,
    }, "widths": {}}
    from skimage import transform
    import merge_acceleration
    import gpu_compute
    # RANSAC receives the same seed for each run: compare processing paths,
    # without introducing unrelated stochastic registration changes.
    original_ransac = merge_workflow.ransac
    merge_workflow.ransac = lambda *a, **kw: original_ransac(*a, **kw, rng=42)
    try:
        for width in widths:
            values = fixtures(width)
            if photo is not None:
                scene = transform.resize(photo, (width, width * 2, 3),
                                         preserve_range=True).astype(np.float32)
                base = scene[:, :width].copy()
                values["hdr"] = [np.clip(ndimage.shift(base * e, (y, x, 0), order=1), 0, 1)
                                 for e, y, x in ((.55, 1, -1), (1, 0, 0), (1.6, -1, 1))]
                values["panorama"] = [scene[:, i*width//2:i*width//2+width].copy() for i in range(3)]
                blurred = ndimage.gaussian_filter(base, (3, 3, 0))
                values["focus"] = []
                for i in range(4):
                    frame = blurred.copy()
                    start, stop = max(0, i*width//4-width//16), min(width, i*width//4+width//3)
                    frame[start:stop] = base[start:stop]
                    values["focus"].append(frame)
            result = {}
            for mode, function in (("hdr", merge_workflow.hdr_merge),
                                   ("panorama", merge_workflow.panorama_merge),
                                   ("focus", merge_workflow.focus_merge)):
                reference = None
                result[mode] = {}
                for backend in backends:
                    os.environ["LIGHTTABLE_MERGE_ACCELERATION"] = "cpu" if backend == "baseline" else backend
                    active_function = getattr(baseline, mode + "_merge") if backend == "baseline" else function
                    merge_acceleration._disabled = False
                    samples, phases, warmup_samples = [], [], []
                    for iteration in range(-max(0, args.warmup), max(1, args.iterations)):
                        started = time.perf_counter()
                        last = started
                        stage_times = {}
                        def progress(phase, done, total):
                            nonlocal last
                            now = time.perf_counter()
                            stage_times[phase] = stage_times.get(phase, 0) + (now-last)*1000
                            last = now
                        merged = active_function(values[mode], progress=progress)
                        elapsed = (time.perf_counter() - started) * 1000
                        stage_times["finalize"] = (time.perf_counter()-last)*1000
                        if iteration >= 0:
                            samples.append(elapsed)
                            phases.append(stage_times)
                        else:
                            warmup_samples.append(elapsed)
                    if backend == "gpu" and merge_acceleration._disabled:
                        raise RuntimeError("GPU merge fell back to CPU; benchmark is not valid")
                    item = {"medianMs": round(statistics.median(samples), 2),
                            "samplesMs": [round(v, 2) for v in samples],
                            "warmupSamplesMs": [round(v, 2) for v in warmup_samples],
                            "shape": list(merged.shape),
                            "phasesMs": {key: round(statistics.median(p.get(key, 0) for p in phases), 2)
                                         for key in set().union(*phases)}}
                    if reference is None:
                        reference = merged
                    else:
                        item["sameShape"] = merged.shape == reference.shape
                        if item["sameShape"]:
                            difference = merged.astype(np.float64)-reference
                            item["maxAbsError"] = float(np.max(np.abs(difference)))
                            mse = float(np.mean(difference*difference))
                            item["psnrDb"] = round(-10*np.log10(max(mse, 1e-30)), 2)
                    result[mode][backend] = item
            results["widths"][str(width)] = result
    finally:
        merge_workflow.ransac = original_ransac
        gpu_compute.close()
    payload = json.dumps(results, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
