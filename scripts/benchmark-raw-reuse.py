#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Compare uncached and reused capture pixels through the app's actual decoder."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import color_pipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be between 1 and 10")
    variants = [
        ("first_full", {}), ("reuse_full", {}),
        ("first_custom", {"wb_mode": "custom", "wb_temperature": 5000}),
        ("adjust_custom", {"wb_mode": "custom", "wb_temperature": 6500, "wb_tint": 0.2}),
    ]
    result = {"file": args.raw.name, "runs": []}
    cache = color_pipeline.RAW_DEMOSAIC_CACHE
    budget = cache.max_bytes
    expected = {}
    try:
        for repeat in range(args.repeats):
            for enabled in (False, True):
                cache.clear()
                cache.max_bytes = budget if enabled else 0
                for name, params in variants:
                    started = time.perf_counter()
                    pixels = color_pipeline.decode_raw(args.raw, params)
                    elapsed = (time.perf_counter() - started) * 1000
                    digest = hashlib.sha256(memoryview(pixels).cast("B")).hexdigest()
                    if name in expected:
                        assert digest == expected[name], f"pixels changed: {name}"
                    expected[name] = digest
                    result["runs"].append(dict(repeat=repeat, cache=enabled, case=name,
                        ms=round(elapsed, 3), sha256=digest, cacheStats=cache.stats()))
                    del pixels
        result["medians"] = {name: {
            ("cached" if enabled else "uncached"): round(statistics.median(
                row["ms"] for row in result["runs"]
                if row["case"] == name and row["cache"] == enabled), 3)
            for enabled in (False, True)} for name, _ in variants}
        result["byteIdentical"] = True
    finally:
        cache.clear()
        cache.max_bytes = budget
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(json.dumps(result["medians"], indent=2))


if __name__ == "__main__":
    main()
