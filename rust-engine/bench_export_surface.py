#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Verify full-precision shared exports and benchmark removal of float TIFF I/O.

Uses synthetic pixels, the actual resident engine, and the existing Python
finisher. Every delivered image must have identical pixels and ICC bytes to the
TIFF intermediate path. No GPU precision or finishing algorithm is changed.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image
import tifffile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import export_surface
import server
from bench_resident_cache import invoke


def fixture(path, width):
    height = width * 2 // 3
    y, x = np.mgrid[:height, :width]
    pixels = np.stack((.005 + 1.3 * x / width, .005 + .8 * y / height,
                       .02 + ((x // 37 + y // 43) % 8) / 5), axis=2).astype(np.float32)
    tifffile.imwrite(path, pixels, photometric="rgb")


def delivery(path):
    if path.suffix == ".tif":
        with tifffile.TiffFile(path) as image:
            return image.asarray(), image.pages[0].tags[34675].value
    with Image.open(path) as image:
        return np.array(image), image.info.get("icc_profile")


def recipes():
    base = dict(grade={}, masks=[], heals=[], optics={}, crop=None,
                watermark={"enabled": False}, format="tif", quality=92,
                outputSpace="srgb", bitDepth=16, metadata="none")
    edits = {
        "tiff16": {},
        "display_p3": {"outputSpace": "display_p3", "grade": {"exposure": .15}},
        "prophoto": {"outputSpace": "prophoto", "grade": {"saturation": .15}},
        "brush": {"format": "jpeg", "masks": [{"type": "brush", "strokes": [{
            "points": [[.2, .3], [.6, .5]], "size": .2, "feather": .7}],
            "grade": {"exposure": .6, "clarity": .2}}]},
        "heals": {"format": "jpeg", "heals": [{"enabled": True,
            "source": [.3, .4], "target": [.6, .6], "radius": .07}]},
        "optics": {"format": "jpeg", "optics": {"distortion": .2,
            "vignette": .1, "rotate": 2, "vertical": .1}},
        "watermark": {"watermark": {"enabled": True, "text": "Parity proof",
            "opacity": .7}, "crop": {"x": .1, "y": .1, "w": .8, "h": .7},
            "longEdge": 240},
    }
    return {name: dict(copy.deepcopy(base), **delta) for name, delta in edits.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "rust-engine/target/release/lighttable-engine")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--width", type=int, default=2200)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks, timings = [], []
    with tempfile.TemporaryDirectory(prefix="lighttable-export-surface-") as temporary:
        root = Path(temporary)
        source = root / "input.tif"
        fixture(source, args.width)
        request = dict(id=1, command="render", input=str(source), input_cache_key="export-proof",
            data_dir=str(args.data.resolve()), film="kodak_portra_400", paper="kodak_endura_premier",
            params={"io": {"input_color_space": "ProPhoto RGB", "input_cctf_decoding": False}},
            bit_depth=32)
        with (root / "engine.log").open("w") as log:
            process = subprocess.Popen([str(args.binary.resolve())], text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                env=dict(os.environ, SPEKTRAFILM_BACKEND="wgpu"))
            try:
                film = root / "film.tif"
                # Both transports derive from the very same render, including
                # orientation, and the descriptor carries no quantized pixels.
                for rotation in range(4):
                    result = invoke(process, dict(request, output=str(film), export_shared=True,
                                                  rotate_quarters_ccw=rotation))
                    pixels = export_surface.adopt_surface(result["export_shared"])
                    expected = tifffile.imread(film)
                    np.testing.assert_array_equal(pixels.view(np.uint32), expected.view(np.uint32))
                    checks.append(dict(rotation=rotation, float_bits_identical=True))
                    if rotation == 0:
                        # Keep finishing fixtures bounded even with a large render.
                        small = pixels[::max(1, args.width // 400), ::max(1, args.width // 400)]
                        small_film = root / "finish-source.tif"
                        tifffile.imwrite(small_film, small, photometric="rgb")
                        for name, job in recipes().items():
                            extension = ".tif" if job["format"] == "tif" else ".jpg"
                            old, new = root / (name + "-old" + extension), root / (name + "-new" + extension)
                            server.finish_export(small_film, old, copy.deepcopy(job))
                            server.finish_export(small, new, copy.deepcopy(job))
                            old_pixels, old_profile = delivery(old)
                            new_pixels, new_profile = delivery(new)
                            np.testing.assert_array_equal(new_pixels, old_pixels)
                            assert new_profile == old_profile, name
                            checks.append(dict(recipe=name, delivery_pixels_identical=True, icc_identical=True))
                    del pixels, expected
                for iteration in range(args.runs):
                    for shared in ([False, True] if iteration % 2 == 0 else [True, False]):
                        started = time.perf_counter()
                        result = invoke(process, dict(request, **({"export_shared": True}
                            if shared else {"output": str(film)})))
                        read_started = time.perf_counter()
                        if shared:
                            pixels = export_surface.adopt_surface(result["export_shared"])
                        else:
                            pixels = tifffile.imread(film)
                        assert pixels.shape == (args.width * 2 // 3, args.width, 3)
                        read_ms = (time.perf_counter() - read_started) * 1000
                        timings.append(dict(shared=shared, encode_ms=result["encode_ms"],
                            read_ms=read_ms, transport_ms=result["encode_ms"] + read_ms,
                            roundtrip_ms=(time.perf_counter() - started) * 1000))
                        del pixels
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    summary = {"width": args.width, "height": args.width * 2 // 3,
               "checks": len(checks), "all_exact": True}
    for label, shared in (("tiff", False), ("shared", True)):
        summary[label + "_transport_ms"] = statistics.median(
            row["transport_ms"] for row in timings if row["shared"] == shared)
    summary["transport_speedup"] = summary["tiff_transport_ms"] / summary["shared_transport_ms"]
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(summary=summary, checks=checks, timings=timings), indent=2) + "\n")


if __name__ == "__main__":
    main()
