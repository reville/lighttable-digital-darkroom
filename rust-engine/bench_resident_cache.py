#!/usr/bin/env python3
"""Compare uncached and cached resident film outputs and downstream-slider timings.

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


def run(binary, data, directory, enabled, requests, input_cache_bytes, fresh_pipeline=False):
    env = dict(os.environ, LIGHTTABLE_RESIDENT_STAGE_CACHE=str(int(enabled)),
               LIGHTTABLE_RESIDENT_PIPELINE_CACHE_ENTRIES='0' if fresh_pipeline else '1',
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
    parser.add_argument('--fresh-pipeline-check', action='store_true', help='also compare against independently rebuilt spectral pipelines')
    parser.add_argument('--budget-check', action='store_true', help='four-case large-image cache bypass check')
    parser.add_argument('--require-hardware', action='store_true',
                        help='reject CPU/software adapters when measuring hardware performance')
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
        downstream_labels = []
        for label, group, field, value in [
            ('yellow_filter', 'enlarger', 'y_filter_shift', 3.0),
            ('magenta_filter', 'enlarger', 'm_filter_shift', -3.0),
            ('preflash', 'enlarger', 'preflash_exposure', 0.05),
            ('preflash_yellow', 'enlarger', 'preflash_y_filter_shift', 7.0),
            ('illuminant', 'enlarger', 'illuminant', 'D50'),
            ('paper_gamma', 'print_render', 'density_curve_gamma', 1.1),
            ('paper_glare', 'print_render', 'glare', {'active': True, 'percent': 3.0, 'roughness': 0.0}),
            ('scanner_blur', 'scanner', 'lens_blur', 1.2),
            ('scanner_sharpen', 'scanner', 'unsharp_mask', [1.2, 0.8]),
            ('print_reference', 'scanner', 'white_correction', True),
            ('output_space', 'io', 'output_color_space', 'ProPhoto RGB'),
            ('output_encoding', 'io', 'output_cctf_encoding', False),
        ]:
            params = copy.deepcopy(base)
            params.setdefault(group, {})[field] = value
            label = 'downstream_' + label
            add(label, params)
            downstream_labels.append(label)
        add('paper_switch', base, paper='fujifilm_crystal_archive_typeii')
        downstream_labels.append('paper_switch')
        add('paper_return', base)
        downstream_labels.append('paper_return')
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
        scan_params = copy.deepcopy(base)
        scan_params['scanner'] = {'lens_blur': 1.2}
        add('slide_scanner_blur', scan_params, a, 'kodak_ektachrome_100', scan=True)
        scan_params['scanner']['white_correction'] = True
        add('slide_reference', scan_params, a, 'kodak_ektachrome_100', scan=True)
        add('return_from_scan', base, a)
        if args.budget_check:
            requests = [next(r for r in requests if r[0] == label) for label in
                        ('warmup', 'print_drag', 'different_dimensions', 'return_dimensions')]
        baseline = run((args.baseline_binary or args.binary).resolve(), args.data.resolve(), directory, False, requests, args.input_cache_bytes)
        cached = run(args.binary.resolve(), args.data.resolve(), directory, True, requests, args.input_cache_bytes)
        assert 0.001 < cached[0]['mean'] < 0.999, 'fixture must produce nonblank pixels'
        assert len({r['sha256'] for r in cached if r['case'] == 'print_drag'}) == sum(r['case'] == 'print_drag' for r in cached), 'print exposure must alter output'
        mismatches = [a['case'] for a, b in zip(baseline, cached) if a['sha256'] != b['sha256']]
        if args.fresh_pipeline_check:
            fresh = run(args.binary.resolve(), args.data.resolve(), directory, False,
                        requests, args.input_cache_bytes, fresh_pipeline=True)
            mismatches.extend('fresh:' + a['case'] for a, b in zip(fresh, cached)
                              if a['sha256'] != b['sha256'])
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
                       ('camera_ev', 'different_input', 'different_dimensions', 'rebuilt_pipeline', 'slide_scan', 'slide_reference'))
            unexpected_misses = [r['case'] for r in cached if
                (r['case'] in downstream_labels or r['case'] == 'slide_scanner_blur')
                and (r['film_stage_cache_hit'] != expected_checkpoint or not r['pipeline_cache_hit'])]
            assert not unexpected_misses, unexpected_misses
        summary = {'width': args.width, 'cases': len(requests), 'backend': cached[0]['backend'],
                   'adapter': cached[0].get('adapter'),
                   'bit_identical': not mismatches, 'mismatches': mismatches,
                   'fresh_pipeline_checked': args.fresh_pipeline_check}
        software = bool((summary['adapter'] or {}).get('software'))
        summary['performance_scope'] = ('software GPU execution only' if software
                                        else 'reported local adapter')
        transfers = [r['gpu_timings'] for r in cached if r['case'] == 'print_drag'
                     and r.get('gpu_timings')]
        if transfers:
            summary['gpu_timings'] = {key: statistics.median(row[key] for row in transfers)
                                     for key in ('cpu_setup_ms', 'submit_to_map_ms',
                                                 'cpu_readback_ms', 'upload_bytes', 'readback_bytes')}
            summary['readback_reuse_fraction'] = sum(row['readback_buffer_reused'] for row in transfers) / len(transfers)
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
        if args.require_hardware and (software or not summary['adapter']):
            raise SystemExit('Hardware performance NOT DONE: no identified hardware adapter')


if __name__ == '__main__':
    main()
