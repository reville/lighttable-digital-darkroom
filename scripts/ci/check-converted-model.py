#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check cached Core ML bytes and conversion identity without loading ML libraries."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONVERTER = load_script('convert_models', 'convert-models.py')
FETCHER = load_script('fetch_models', 'fetch-models.py')


def verify(directory):
    package = directory / 'denoise.mlpackage'
    required = [directory / name for name in (
        'models.json', 'SCUNet-CODE-LICENSE.txt', 'SCUNet-WEIGHTS-LICENSE.txt')]
    if not package.is_dir() or package.is_symlink():
        raise ValueError('Converted model package is missing or is a symlink')
    files = list(package.rglob('*'))
    if any(item.is_symlink() for item in files + required):
        raise ValueError('Converted model cache contains a symlink')
    if not any(item.is_file() for item in files):
        raise ValueError('Converted model package is empty')
    if any(not item.is_file() or not item.stat().st_size for item in required):
        raise ValueError('Converted model metadata or license is missing/empty')
    entry = json.loads(required[0].read_text())['denoise']
    expected = {
        'version': f'SCUNet {CONVERTER.SCUNET_REVISION[:7]} fp16',
        'source': 'https://github.com/cszn/SCUNet',
        'weightsSha256': FETCHER.WEIGHTS['scunet_color_real_psnr.pth'],
        'input': CONVERTER.TILE,
        'precision': 'fp16',
        'sha256': CONVERTER.package_digest(package),
    }
    for key, value in expected.items():
        if entry.get(key) != value:
            raise ValueError(f'Converted model cache has mismatched {key}')
    return expected['sha256']


def verify_onnx(directory):
    """Check the exported ONNX bytes the Windows and Linux packages bundle."""
    model = directory / 'denoise.onnx'
    required = [directory / name for name in (
        'models.json', 'SCUNet-CODE-LICENSE.txt', 'SCUNet-WEIGHTS-LICENSE.txt')]
    if any(item.is_symlink() for item in [model] + required):
        raise ValueError('Exported model directory contains a symlink')
    if not model.is_file() or not model.stat().st_size:
        raise ValueError('Exported ONNX model is missing or empty')
    if any(not item.is_file() or not item.stat().st_size for item in required):
        raise ValueError('Exported model metadata or license is missing/empty')
    entry = json.loads(required[0].read_text())['denoise']
    expected = {
        'version': f'SCUNet {CONVERTER.SCUNET_REVISION[:7]} onnx fp16',
        'source': 'https://github.com/cszn/SCUNet',
        'weightsSha256': FETCHER.WEIGHTS['scunet_color_real_psnr.pth'],
        'input': CONVERTER.TILE,
        'precision': 'fp16',
        'runtime': 'onnxruntime',
        'sha256': CONVERTER.file_digest(model),
    }
    for key, value in expected.items():
        if entry.get(key) != value:
            raise ValueError(f'Exported model index has mismatched {key}')
    return expected['sha256']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=None)
    parser.add_argument('--onnx', action='store_true',
                        help='verify the exported ONNX model under scripts/models/onnx instead')
    args = parser.parse_args()
    directory = args.directory or (ROOT / 'scripts/models/onnx' if args.onnx else ROOT / 'scripts/models')
    try:
        digest = verify_onnx(directory) if args.onnx else verify(directory)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Model cache verification failed: {error}\n')
    print(f'Converted model identity and SHA-256 verified: {digest}')


if __name__ == '__main__':
    main()
