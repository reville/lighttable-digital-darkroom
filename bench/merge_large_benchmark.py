#!/usr/bin/env python3
"""One-pass camera-size merge checks with reloadable, disk-backed fixtures.

Only one fixture family and one output is held at a time. This measures the
numeric merge path, including cached NPY reloads, not RAW decode or TIFF export.
"""
from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gpu_compute
import merge_acceleration
import merge_workflow


def fixtures(photo: Path, width: int, mode: str, directory: Path):
    with Image.open(photo) as original:
        height = round(width * original.height / original.width)
        if mode == "panorama":
            wide = original.convert("RGB").resize((width * 2, height), Image.Resampling.LANCZOS)
            for i in range(3):
                frame = np.asarray(wide.crop((i*width//2, 0, i*width//2+width, height)), dtype=np.float32)/255
                np.save(directory / f"frame-{i}.npy", frame)
                del frame
            return sorted(directory.glob("frame-*.npy")), (height, width, 3)
        base = np.asarray(original.convert("RGB").resize((width, height), Image.Resampling.LANCZOS), dtype=np.float32)/255
    if mode == "hdr":
        for i, (exposure, dy, dx) in enumerate(((.55, 1, -1), (1, 0, 0), (1.6, -1, 1))):
            frame = np.zeros_like(base)
            ys, ye = max(0, dy), min(height, height+dy)
            xs, xe = max(0, dx), min(width, width+dx)
            np.multiply(base[ys-dy:ye-dy, xs-dx:xe-dx], exposure,
                        out=frame[ys:ye, xs:xe])
            np.clip(frame, 0, 1, out=frame)
            np.save(directory / f"frame-{i}.npy", frame)
            del frame
    else:
        blurred = ndimage.gaussian_filter(base, (3, 3, 0))
        for i in range(4):
            frame = blurred.copy()
            start, stop = max(0, i*height//4-height//16), min(height, i*height//4+height//3)
            frame[start:stop] = base[start:stop]
            np.save(directory / f"frame-{i}.npy", frame)
            del frame
    return sorted(directory.glob("frame-*.npy")), (height, width, 3)


def quality(first: Path, second: Path):
    a, b = np.load(first, mmap_mode="r"), np.load(second, mmap_mode="r")
    if a.shape != b.shape:
        return {"sameShape": False, "baselineShape": a.shape, "gpuShape": b.shape}
    maximum, square_sum, count = 0.0, 0.0, 0
    for top in range(0, a.shape[0], 64):
        delta = a[top:top+64].astype(np.float64) - b[top:top+64]
        maximum = max(maximum, float(np.max(np.abs(delta))))
        square_sum += float(np.sum(delta*delta))
        count += delta.size
    return {"sameShape": True, "maxAbsError": maximum,
            "psnrDb": float(-10*np.log10(max(square_sum/count, 1e-30)))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--width", type=int, default=6000)
    parser.add_argument("--modes", default="hdr,panorama,focus")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 128 <= args.width <= 6000:
        parser.error("width must be128–6000")
    modes = args.modes.split(",")
    if any(mode not in {"hdr", "panorama", "focus"} for mode in modes):
        parser.error("unknown merge mode")
    spec = importlib.util.spec_from_file_location("large_merge_reference", args.baseline_source)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    for module in (baseline, merge_workflow):
        original = module.ransac
        module.ransac = lambda *a, _ransac=original, **kw: _ransac(*a, **kw, rng=42)
    results = {"fixture": str(args.image), "width": args.width,
               "samples": 1, "includes": "numeric merge with disk-backed reloadable float32 fixtures",
               "excludes": "RAW decode, TIFF write and native UI", "modes": {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for mode in modes:
            with tempfile.TemporaryDirectory(prefix="lighttable-merge-large-") as temporary:
                directory = Path(temporary)
                paths, input_shape = fixtures(args.image, args.width, mode, directory)
                gc.collect()
                item = {"inputShape": input_shape, "frames": len(paths)}
                for backend, module in (("baseline", baseline), ("gpu", merge_workflow)):
                    os.environ["LIGHTTABLE_MERGE_ACCELERATION"] = "cpu" if backend == "baseline" else "gpu"
                    merge_acceleration._disabled = False
                    # skimage's Cython warp requires a writable buffer even though
                    # it only reads pixels. Copy-on-write maps match app arrays
                    # without keeping every camera frame in heap memory.
                    loaders = [lambda path=path: np.load(path, mmap_mode="c") for path in paths]
                    stage_times = {}
                    started = last = time.perf_counter()
                    def progress(phase, done, total):
                        nonlocal last
                        now = time.perf_counter()
                        stage_times[phase] = stage_times.get(phase, 0) + (now-last)*1000
                        last = now
                    image = getattr(module, mode+"_merge")(loaders, progress=progress)
                    elapsed = (time.perf_counter()-started)*1000
                    stage_times["finalize"] = (time.perf_counter()-last)*1000
                    item[backend] = {"timeMs": elapsed, "phasesMs": stage_times,
                        "shape": image.shape, "finite": bool(np.isfinite(image).all()),
                        "peakProcessRssMiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == "darwin" else 1024)}
                    if backend == "gpu" and merge_acceleration._disabled:
                        raise RuntimeError("GPU unavailable; large-size benchmark invalid")
                    np.save(directory/f"{backend}.npy", image)
                    del image
                    gc.collect()
                item["comparison"] = quality(directory/"baseline.npy", directory/"gpu.npy")
                results["modes"][mode] = item
                args.output.write_text(json.dumps(results, indent=2)+"\n")
                print(json.dumps({mode: item}), flush=True)
    finally:
        gpu_compute.close()
    if any(not item["comparison"]["sameShape"] for item in results["modes"].values()):
        raise SystemExit("GPU output shape mismatch")


if __name__ == "__main__":
    main()
