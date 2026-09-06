#!/usr/bin/env python3
"""Prove GPU packing, rotated viewport crops, and shared transport against float RGB."""
import argparse
import ctypes
import json
import mmap
import os
from pathlib import Path
import statistics
import struct
import subprocess
import tempfile

from bench_resident_cache import fixture, invoke
from bench_viewport import read_float_tiff


def read_surface(path):
    data = path.read_bytes()
    magic, width, height, row = struct.unpack('<4sIII', data[:16])
    assert magic == b'FLRA' and row == width * 4
    return width, height, data[16:]


def read_shared(surface):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
    libc.shm_open.restype = ctypes.c_int
    name = surface['name'].encode()
    fd = libc.shm_open(name, os.O_RDONLY, 0)
    assert fd >= 0, ctypes.get_errno()
    try:
        with mmap.mmap(fd, surface['length'], access=mmap.ACCESS_READ) as data:
            row = surface['rowBytes']
            return b''.join(data[y*row:y*row+surface['width']*4] for y in range(surface['height']))
    finally:
        os.close(fd)
        assert libc.shm_unlink(name) == 0


def expected_rgba(samples):
    # The worker quantizes f32, so preserve the f32 multiply before Python round.
    out = bytearray()
    for i, value in enumerate(samples):
        scaled = struct.unpack('f', struct.pack('f', min(1.0, max(0.0, value))*255.0))[0]
        out.append(round(scaled))
        if i % 3 == 2:
            out.append(255)
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path(__file__).parent/'target/release/lighttable-engine')
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--width', type=int, default=1100)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix='lighttable-native-pack-') as tmp:
        root = Path(tmp)
        source = root/'input.tiff'
        fixture(source, args.width, args.width*2//3)
        with (root/'engine.log').open('w') as log:
            process = subprocess.Popen([str(args.binary.resolve())], text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, env=dict(os.environ, SPEKTRAFILM_BACKEND='wgpu'))
            try:
                for rotation in range(4):
                    for viewport in [None, dict(x=71, y=37, width=180, height=120)]:
                        request = dict(id=len(results)+1, command='render', input=str(source),
                            input_cache_key='pack-fixture', data_dir=str(args.data.resolve()),
                            film='kodak_portra_400', paper='kodak_endura_premier',
                            params={'io': {'input_color_space':'ProPhoto RGB', 'input_cctf_decoding':False}},
                            bit_depth=32, rotate_quarters_ccw=rotation)
                        if viewport: request['viewport'] = viewport
                        rgb_path = root/'float.tiff'
                        baseline = invoke(process, dict(request, output=str(rgb_path)))
                        assert 'wgpu' in baseline['backend'].lower(), baseline
                        width, height, samples = read_float_tiff(rgb_path)
                        expected = expected_rgba(samples)
                        for shared in (False, True):
                            output = root/f'native-{rotation}-{bool(viewport)}-{shared}.rgba'
                            result = invoke(process, dict(request, native_output=str(output), native_shared=shared))
                            assert result['native_gpu_packed'], result
                            assert (result['width'],result['height']) == (width,height), result
                            if shared:
                                assert not output.exists(), 'shared transport still wrote the frame to disk'
                                actual = read_shared(result['native_shared'])
                            else:
                                w,h,actual = read_surface(output)
                                assert (w,h)==(width,height)
                            assert actual == expected, (rotation, viewport, shared,
                                sum(a!=b for a,b in zip(actual,expected)))
                            reference_mean = sum(samples)/len(samples)
                            assert abs(result['mean']-reference_mean) < 1e-6, (result['mean'],reference_mean)
                            result.update(rotation=rotation, viewport=viewport, shared=shared, byte_identical=True)
                            results.append(result)
            finally:
                process.stdin.close()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
    summary = dict(cases=len(results), byte_identical=True, shared_only_cases=sum(r['shared'] for r in results),
        native_encode_ms=statistics.median(r['encode_ms'] for r in results))
    print(json.dumps(summary, indent=2))
    if args.output: args.output.write_text(json.dumps(dict(summary=summary, results=results), indent=2)+'\n')


if __name__ == '__main__':
    main()
