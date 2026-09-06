#!/usr/bin/env python3
"""Verify RAW output identity across processes with 1 and 8 OpenMP threads.

Pass real X-Trans and Bayer files. An optional stock-wheel interpreter adds
baseline identity/timing. Each decode gets a fresh process, so OpenMP reads
its thread limit before initialization. No original or catalog is modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


DECODE = r'''
import hashlib, json, sys, time, rawpy
from contextlib import ExitStack
sys.path.insert(0, sys.argv[3])
import raw_decode_runtime
try:
    import rawpy_openmp
    available = rawpy_openmp.flags['OPENMP']
except ImportError:
    available = False
t = time.perf_counter()
with ExitStack() as stack:
    if sys.argv[2] == 'stock':
        decoder = rawpy
        raw = stack.enter_context(rawpy.imread(sys.argv[1]))
    else:
        raw, decoder = stack.enter_context(raw_decode_runtime.open_raw(sys.argv[1]))
    opened = time.perf_counter()
    rgb = raw.postprocess(use_camera_wb=True, gamma=(1,1), no_auto_bright=True,
        output_bps=16, output_color=decoder.ColorSpace.ProPhoto,
        highlight_mode=decoder.HighlightMode.ReconstructDefault)
done = time.perf_counter()
print(json.dumps({'openmp':decoder.flags['OPENMP'], 'available':available,
    'decoder':decoder.__name__, 'shape':rgb.shape,
    'openMs':round((opened-t)*1000,2), 'processMs':round((done-opened)*1000,2),
    'sha256':hashlib.sha256(rgb).hexdigest()}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--baseline-python")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be 1 to 10")
    rows = []
    for source in args.sources:
        row = {"file": source.name, "runs": []}
        configurations = [("openmp", args.python, t) for t in (1, 8)]
        if args.baseline_python:
            configurations.insert(0, ("stock", args.baseline_python, 1))
        for label, python, threads in configurations:
            for _ in range(args.repeats):
                env = dict(os.environ, OMP_NUM_THREADS=str(threads), OMP_DYNAMIC="FALSE")
                # Explicit interpreters must not inherit a site-packages overlay.
                env.pop("PYTHONPATH", None)
                result = subprocess.run([python, "-c", DECODE, str(source.resolve()),
                                         label, str(Path(__file__).resolve().parents[1])],
                                        env=env, check=True, text=True,
                                        capture_output=True, timeout=180)
                run = json.loads(result.stdout)
                if label == "openmp" and not run["available"]:
                    raise SystemExit("Candidate rawpy does not support OpenMP")
                row["runs"].append(dict(run, runtime=label, threads=threads))
        row["byteIdentical"] = len({(tuple(r["shape"]), r["sha256"])
                                      for r in row["runs"]}) == 1
        rows.append(row)
    print(json.dumps(rows, indent=2), flush=True)
    if not all(row["byteIdentical"] for row in rows):
        raise SystemExit("RAW output changed across thread counts or runtimes")


if __name__ == "__main__":
    main()
