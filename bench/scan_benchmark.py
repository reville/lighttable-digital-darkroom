#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Time ``catalog_scan.scan_source`` against real files on disk.

The fixture is a folder of distinct noise JPEGs: every file has its own
pixels, so header and content hashes differ per file the way a real shoot
does (hard links to one seed would measure duplicate handling instead).
Each repeat scans a fresh catalog, so the number reported is the cold path a
new source pays: walk, header hash, full-content hash and, unless disabled,
the metadata read. The page cache is warm after generation, so the hashing
cost measured here is CPU bound rather than disk bound.

``LIGHTTABLE_SCAN_HASH_WORKERS`` passes through to the scanner, so a
before/after pair is two invocations against the same ``--fixtures`` folder.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import catalog  # noqa: E402
import catalog_scan  # noqa: E402


def build_fixtures(folder: Path, count: int, width: int, height: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob("*/frame-*.jpg"))
    if len(existing) == count:
        return
    for path in existing:
        path.unlink()
    rng = np.random.default_rng(20260913)
    for index in range(count):
        subfolder = folder / f"roll-{index % 8:02d}"
        subfolder.mkdir(exist_ok=True)
        pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
        Image.fromarray(pixels, "RGB").save(
            subfolder / f"frame-{index:05d}.jpg", "JPEG", quality=95)


def scan_once(fixtures: Path, read_metadata: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="lighttable-scan-bench-") as temp:
        cat = catalog.Catalog(Path(temp) / "library.sqlite3")
        try:
            source = cat.add_source(fixtures)
            started = time.perf_counter()
            result = catalog_scan.scan_source(
                cat, source, read_metadata_for_new=read_metadata)
            elapsed = time.perf_counter() - started
        finally:
            cat.close()
    if not result.get("complete") or result.get("error"):
        raise RuntimeError(f"scan incomplete: {result}")
    return {"seconds": round(elapsed, 3), "added": result["added"],
            "photos_per_second": round(result["added"] / elapsed, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fixtures", type=Path,
                        help="reuse this folder of generated JPEGs across runs")
    parser.add_argument("--no-metadata", action="store_true",
                        help="skip the per-file metadata read to isolate hashing")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.count <= 20000 or not 1 <= args.repeats <= 20:
        raise SystemExit("--count must be 1..20000 and --repeats 1..20")
    owns_fixtures = args.fixtures is None
    fixtures = (Path(tempfile.mkdtemp(prefix="lighttable-scan-fixtures-"))
                if owns_fixtures else args.fixtures.resolve())
    try:
        build_fixtures(fixtures, args.count, args.width, args.height)
        total_bytes = sum(path.stat().st_size for path in fixtures.glob("*/frame-*.jpg"))
        runs = [scan_once(fixtures, not args.no_metadata) for _ in range(args.repeats)]
        rates = [run["photos_per_second"] for run in runs]
        output = {
            "schema": 1,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "hash_workers": os.environ.get("LIGHTTABLE_SCAN_HASH_WORKERS", "default"),
            "count": args.count,
            "fixture_bytes": total_bytes,
            "mean_file_bytes": total_bytes // max(1, args.count),
            "read_metadata": not args.no_metadata,
            "runs": runs,
            "median_seconds": round(statistics.median(run["seconds"] for run in runs), 3),
            "median_photos_per_second": round(statistics.median(rates), 1),
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps(output, indent=2))
        return 0
    finally:
        if owns_fixtures:
            shutil.rmtree(fixtures, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
