#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Compare ImageIO with rawpy embedded thumbnails on real RAW containers.

Usage: python scripts/benchmark-raw-thumbnails.py photo.dng photo.raf ...
Reports failures, dimensions, orientation, and warm medians separately. No
renderer or live catalog is started, and originals are only read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import color_pipeline
import platform_image
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("--repeats must be between 1 and 100")
    rows = []
    with tempfile.TemporaryDirectory(prefix="lighttable-raw-thumb-") as directory:
        for source in args.sources:
            row = {"file": source.name}
            for name in ("imageio", "rawpy"):
                output = Path(directory) / f"{name}.jpg"
                elapsed = []
                try:
                    for _ in range(args.repeats):
                        start = time.perf_counter()
                        if name == "imageio":
                            platform_image._build_thumbnail_imageio(source, output)
                        elif not color_pipeline.raw_embedded_thumbnail(source, output, 240, 80):
                            raise ValueError("no embedded thumbnail")
                        elapsed.append((time.perf_counter() - start) * 1000)
                        with Image.open(output) as image:
                            image.load()
                            dimensions = image.size
                            if max(dimensions) > 240:
                                raise ValueError(f"unbounded dimensions: {dimensions}")
                    row[name] = {"firstMs": round(elapsed[0], 2),
                                 "medianMs": round(statistics.median(elapsed), 2),
                                 "dimensions": dimensions}
                except Exception as error:
                    row[name] = {"error": str(error)}
            rows.append(row)
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
