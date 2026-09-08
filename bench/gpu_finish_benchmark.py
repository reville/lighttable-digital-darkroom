#!/usr/bin/env python3
"""Compare float32 GPU export finishing with the Python reference, including IPC.

Run with the bundled Python and a built engine/lighttable-engine. Writes only
the requested report and temporary output files; it never edits a catalog.
"""
import argparse
import gc
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import rawpy
import grade
import gpu_compute
import edits
from tests.test_gpu_grade import DETAIL, COLOR, TONES, WHEELS


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--export-check', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.repeat <= 5:
        parser.error('--repeat must be 1..5')
    with rawpy.imread(args.source) as raw:
        image = raw.postprocess(use_camera_wb=True, output_bps=16).astype(np.float32) / 65535.0
    rows = []
    results = dict(source=args.source, shape=list(image.shape), dtype=str(image.dtype),
        scope='Wall time includes float input/output transport and GPU work; excludes RAW decode and final encode except export_check.',
        runs=rows)
    cases = [('basic', TONES), ('detail', DETAIL), ('color', COLOR),
             ('combined', dict(DETAIL, **COLOR, **TONES, **WHEELS))]
    small = image[:64, :64].copy()
    started = time.perf_counter()
    gpu_compute.compute('grade', small, [64, 64], small.shape, grade=grade.clean(DETAIL))
    results['cold_worker_and_shader_ms'] = (time.perf_counter() - started) * 1000
    for name, settings in cases:
        grade.apply(small, settings)
        timing = {'python_ms': [], 'gpu_ms': []}
        for _ in range(args.repeat):
            gc.collect()
            start = time.perf_counter()
            expected = grade.apply(image, settings).astype(np.float32)
            timing['python_ms'].append((time.perf_counter() - start) * 1000)
            gc.collect()
            start = time.perf_counter()
            # Basic-only tone uses Numba in production after measurement
            # showed that this transport was slower for that small workload.
            # Still record the actual GPU comparison to justify that decision.
            actual = gpu_compute.compute('grade', image, [image.shape[1], image.shape[0]],
                                         image.shape, grade=grade.clean(settings))
            timing['gpu_ms'].append((time.perf_counter() - start) * 1000)
            error = np.abs(actual - expected)
            maximum = float(error.max()); mean = float(error.mean())
            quantized = np.abs((actual * 65535 + .5).astype(np.int32) -
                              (expected * 65535 + .5).astype(np.int32))
            quantized_max = int(quantized.max())
            if not np.isfinite(actual).all() or maximum > 2e-5:
                raise AssertionError(f'{name} exceeded float precision tolerance: {maximum}')
            del expected, actual, error, quantized
        row = dict(case=name, settings=settings, **timing, max_error=maximum,
                   mean_error=mean, max_16bit_code_difference=quantized_max,
                   production_route='cpu-numba' if name == 'basic' and grade._HAS_NUMBA else 'gpu')
        row['speedup'] = statistics.median(timing['python_ms']) / statistics.median(timing['gpu_ms'])
        rows.append(row)
        Path(args.output).write_text(json.dumps(results, indent=2))
        print(json.dumps({k: v for k, v in row.items() if k != 'settings'}), flush=True)
    if args.export_check:
        with tempfile.TemporaryDirectory(prefix='lighttable-export-parity-') as temp:
            os.environ['LIGHTTABLE_CACHE_DIR'] = temp + '/cache'
            os.environ['LIGHTTABLE_PREFS_FILE'] = temp + '/prefs.json'
            os.environ['LIGHTTABLE_CATALOG_MIRROR'] = '0'
            import server
            import tifffile
            from PIL import Image
            job = dict(grade=dict(DETAIL, **COLOR), format='tif', outputSpace='prophoto',
                metadata='none', watermark={'enabled': False}, optics={}, heals=[],
                masks=[dict(type='radial', center=[.4, .4], radius=.3,
                            grade={'exposure': .2})])
            outputs = []
            export_times = []
            for mode in ['python', 'gpu']:
                path = Path(temp) / (mode + '.tif')
                start = time.perf_counter()
                with mock.patch.dict(os.environ, {'LIGHTTABLE_GPU_COMPUTE': '0' if mode == 'python' else '1'}):
                    server.finish_export(image, path, job)
                export_times.append((time.perf_counter() - start) * 1000)
                pixels = tifffile.imread(path)
                with Image.open(path) as encoded:
                    profile = encoded.info.get('icc_profile')
                outputs.append((pixels, profile))
            delta = np.abs(outputs[0][0].astype(np.int32) - outputs[1][0].astype(np.int32))
            results['export_check'] = dict(format='16-bit ProPhoto TIFF', shape=list(outputs[0][0].shape),
                dtype=str(outputs[0][0].dtype), python_ms=export_times[0], gpu_ms=export_times[1],
                max_code_difference=int(delta.max()), mean_code_difference=float(delta.mean()),
                equal_icc=bool(outputs[0][1]) and outputs[0][1] == outputs[1][1])
            if int(delta.max()) > 2 or not results['export_check']['equal_icc']:
                raise AssertionError('Export exceeded RGB16 tolerance or changed ICC profile')
            print(json.dumps(results['export_check']), flush=True)
    Path(args.output).write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    try:
        run()
    finally:
        gpu_compute.close()
