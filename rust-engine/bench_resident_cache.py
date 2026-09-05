#!/usr/bin/env python3
"""Compare uncached and cached resident film outputs and print-exposure timings.

Uses only Python's standard library. Build release first; provide the installed
engine/data directory. Every output is a float32 TIFF and must match byte for byte.
A generated linear RGB fixture covers gradients, edges, saturated patches, and highlights.
"""
import argparse
import array
import copy
import hashlib
import json
import os
from pathlib import Path
import selectors
import statistics
import struct
import subprocess
import tempfile


def fixture(path, width, height, phase=0):
    pixels = array.array('f')
    for y in range(height):
        for x in range(width):
            pixels.extend((0.005 + 1.3 * x / width,
                           0.005 + 0.8 * y / height,
                           0.02 + ((x // 37 + y // 43 + phase) % 8) / 5))
    if struct.pack('=I', 1) != struct.pack('<I', 1):
        pixels.byteswap()
    entries = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 3, 146),
               (259, 3, 1, 1), (262, 3, 1, 2), (273, 4, 1, 158),
               (277, 3, 1, 3), (278, 4, 1, height),
               (279, 4, 1, len(pixels) * 4), (284, 3, 1, 1), (339, 3, 3, 152)]
    with path.open('wb') as out:
        out.write(b'II' + struct.pack('<HIH', 42, 8, len(entries)))
        for entry in entries:
            out.write(struct.pack('<HHII', *entry))
        out.write(struct.pack('<I6H', 0, 32, 32, 32, 3, 3, 3))
        pixels.tofile(out)


def invoke(proc, request):
    proc.stdin.write(json.dumps(request) + '\n')
    proc.stdin.flush()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        if not selector.select(120):
            raise TimeoutError('resident request exceeded 120 seconds')
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError('resident process exited')
    result = json.loads(line)
    if not result['ok']:
        raise RuntimeError(result)
    return result


def run(binary, data, directory, enabled, requests, input_cache_bytes):
    env = dict(os.environ, LIGHTTABLE_RESIDENT_STAGE_CACHE=str(int(enabled)),
               LIGHTTABLE_RESIDENT_PIPELINE_CACHE_ENTRIES='1',
               LIGHTTABLE_RESIDENT_INPUT_CACHE_BYTES=str(input_cache_bytes))
    results = []
    with (directory / f'engine-{enabled}.log').open('w') as log:
        proc = subprocess.Popen([str(binary)], env=env, text=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log)
        try:
            for index, (label, params, source, film, paper, scan) in enumerate(requests):
                output = directory / f'{enabled}-{index}.tiff'
                result = invoke(proc, dict(id=index, command='render', input=str(source),
                    output=str(output), data_dir=str(data), film=film, paper=paper,
                    scan_film=scan, bit_depth=32, params=params))
                result['case'] = label
                result['sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
                results.append(result)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path(__file__).parent / 'target/release/lighttable-engine')
    parser.add_argument('--baseline-binary', type=Path)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--width', type=int, default=1100)
    parser.add_argument('--input-cache-bytes', type=int, default=256 * 1024 * 1024)
    parser.add_argument('--budget-check', action='store_true', help='four-case large-image cache bypass check')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='lighttable-film-cache-') as temp:
        directory = Path(temp)
        a, b, small = (directory / name for name in ('a.tiff', 'b.tiff', 'small.tiff'))
        fixture(a, args.width, args.width * 2 // 3)
        fixture(b, args.width, args.width * 2 // 3, 1)
        fixture(small, 251, 173)
        base = {'io': {'input_color_space': 'ProPhoto RGB', 'input_cctf_decoding': False},
                'film_render': {'grain': {'active': True}, 'halation': {'active': True}}}
        requests = []
        def add(label, params, source=a, film='kodak_portra_400', paper='kodak_endura_premier', scan=False):
            requests.append((label, copy.deepcopy(params), source, film, paper, scan))
        add('warmup', base)
        for index in range(12):
            params = copy.deepcopy(base)
            params['enlarger'] = {'print_exposure': 0.65 + index * 0.075}
            add('print_drag', params)
        for label, group, field, value in [
            ('camera_ev', 'camera', 'exposure_compensation_ev', 0.3),
            ('meter_method', 'camera', 'auto_exposure_method', 'average'),
            ('no_meter', 'camera', 'auto_exposure', False),
            ('camera_blur', 'camera', 'lens_blur_um', 3.0),
            ('film_gamma', 'film_render', 'density_curve_gamma', 1.1),
            ('paper_gamma', 'print_render', 'density_curve_gamma', 1.1),
            ('preflash', 'enlarger', 'preflash_exposure', 0.05),
            ('filter', 'enlarger', 'y_filter_shift', 3.0),
        ]:
            params = copy.deepcopy(base)
            params.setdefault(group, {})[field] = value
            add(label, params)
            params.setdefault('enlarger', {})['print_exposure'] = 1.15
            add(label + '_print', params)
        add('different_input', base, b)
        add('return_input', base, a)
        add('different_dimensions', base, small)
        add('return_dimensions', base, a)
        add('other_stock', base, a, 'kodak_gold_200')
        add('rebuilt_pipeline', base, a)
        add('slide_scan', base, a, 'kodak_ektachrome_100', scan=True)
        add('return_from_scan', base, a)
        if args.budget_check:
            requests = [requests[0], requests[1], requests[-6], requests[-5]]
        baseline = run((args.baseline_binary or args.binary).resolve(), args.data.resolve(), directory, False, requests, args.input_cache_bytes)
        cached = run(args.binary.resolve(), args.data.resolve(), directory, True, requests, args.input_cache_bytes)
        assert 0.001 < cached[0]['mean'] < 0.999, 'fixture must produce nonblank pixels'
        assert len({r['sha256'] for r in cached if r['case'] == 'print_drag'}) == sum(r['case'] == 'print_drag' for r in cached), 'print exposure must alter output'
        mismatches = [a['case'] for a, b in zip(baseline, cached) if a['sha256'] != b['sha256']]
        if 'wgpu' in cached[0]['backend'].lower():
            expected_checkpoint = args.width * (args.width * 2 // 3) * 12 <= 96 * 1024 * 1024
            assert all(r['film_stage_cache_hit'] == expected_checkpoint for r in cached if r['case'] == 'print_drag')
            if args.input_cache_bytes == 1:
                assert all(not r['input_cache_hit'] for r in cached if r['case'] in ('return_input', 'return_dimensions'))
            assert all(r['resident_cache_bytes'] <= 320 * 1024 * 1024 for r in cached)
            if not expected_checkpoint and args.width * (args.width * 2 // 3) * 24 > 192 * 1024 * 1024:
                assert all(r['resident_cache_bytes'] <= 32 * 1024 * 1024 and not r['gpu_buffers_reused']
                           for r in cached if r['width'] == args.width)
            assert all(not r['film_stage_cache_hit'] for r in cached if r['case'] in
                       ('camera_ev', 'different_input', 'different_dimensions', 'rebuilt_pipeline', 'slide_scan'))
        summary = {'width': args.width, 'cases': len(requests), 'backend': cached[0]['backend'],
                   'bit_identical': not mismatches, 'mismatches': mismatches}
        for metric in ('render_ms', 'total_ms'):
            before = [r[metric] for r in baseline if r['case'] == 'print_drag']
            after = [r[metric] for r in cached if r['case'] == 'print_drag']
            summary[metric] = {'before_median': statistics.median(before),
                               'after_median': statistics.median(after),
                               'speedup': statistics.median(before) / statistics.median(after)}
        report = {'summary': summary, 'baseline': baseline, 'cached': cached}
        print(json.dumps(summary, indent=2))
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + '\n')
        if mismatches:
            raise SystemExit('Float32 parity failed')
        if 'wgpu' not in summary['backend'].lower():
            raise SystemExit('GPU path NOT DONE: backend was ' + summary['backend'])


if __name__ == '__main__':
    main()
