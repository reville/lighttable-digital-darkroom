# SPDX-License-Identifier: GPL-3.0-only
"""Run production browser shaders against independently executed CPU grading."""
from __future__ import annotations

import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import numpy as np
from PIL import Image

import grade
from processing_support import compare_images, grade_cases, target_rgb8, run_browser

ROOT = Path(__file__).resolve().parents[1]


def run(output_dir):
    from bench.benchmark import find_playwright_browser, find_playwright_module
    module = find_playwright_module()
    node = shutil.which('node')
    if not module or not node:
        raise RuntimeError('WebGL gate requires Node and Playwright; set LIGHTTABLE_PLAYWRIGHT_MODULE')
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source = target_rgb8()
    reverse = np.ascontiguousarray(source[::-1, ::-1, ::-1])
    portrait = target_rgb8(96, 128)
    sources = {'target': source, 'reverse': reverse, 'portrait': portrait}
    # Alternating one-pixel stripes and isolated dust are erased by the old
    # 256px sampling-helper overlay, even though the main preview resolves them.
    spots = np.full((512, 1024, 3), 180, dtype=np.uint8)
    spots[:, ::2] = 90
    spots[240:244, 500:504] = 25
    sources['spots'] = spots
    for name, pixels in sources.items():
        Image.fromarray(pixels).save(output_dir / f'{name}.png')

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def translate_path(self, path):
            # Serve the renderer and its localization dependency, plus fixtures.
            if path in {'/web/gl.js', '/web/i18n.js'}:
                return str(ROOT / path.lstrip('/'))
            return super().translate_path(path)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
        functools.partial(Handler, directory=str(output_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    tests = grade_cases()
    tests.extend([
        {'name': 'navigate-b', 'grade': {}, 'fixture': 'reverse'},
        {'name': 'navigate-a', 'grade': {}},
        {'name': 'portrait', 'grade': {'exposure': 0.4}, 'fixture': 'portrait'},
        {'name': 'compare-half', 'grade': {'exposure': 0.4}, 'compare': 0.5},
        {'name': 'compare-off', 'grade': {'exposure': 0.4}},
    ])
    for threshold in [0, 0.55, 1]:
        tests.append({'name': f'spots-{threshold}', 'grade': {}, 'fixture': 'spots',
                      'spotVisualization': {'enabled': True, 'threshold': threshold}})
    tests.extend([
        {'name': 'spots-graded', 'grade': {'exposure': -0.5}, 'fixture': 'spots',
         'spotVisualization': {'enabled': True, 'threshold': 0.55}},
        {'name': 'spots-navigate-portrait', 'grade': {}, 'fixture': 'portrait',
         'spotVisualization': {'enabled': True, 'threshold': 0.55}},
        {'name': 'spots-off', 'grade': {}, 'fixture': 'spots'},
    ])
    for test in tests:
        test.update(source=f"{base}/{test.get('fixture', 'target')}.png",
                    screenshot=str(output_dir / f"{test['name']}-display.png"),
                    raw=str(output_dir / f"{test['name']}.rgba"))
        if test.get('compare'):
            test['original'] = f'{base}/reverse.png'
    result_path = output_dir / 'browser.json'
    config = output_dir / 'config.json'
    config.write_text(json.dumps({'baseUrl': base, 'module': str(module),
        'browser': os.environ.get('LIGHTTABLE_BROWSER_EXECUTABLE') or
                   (str(find_playwright_browser()) if find_playwright_browser() else None),
        'cases': tests, 'result': str(result_path)}))
    try:
        completed = run_browser([node, str(ROOT / 'tests/processing_webgl.mjs'), str(config)],
            cwd=ROOT, timeout=240)
        (output_dir / 'browser.log').write_text(completed.stdout + completed.stderr)
        if completed.returncode:
            raise RuntimeError(f'WebGL runner failed: {completed.stderr[-1800:]}')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    metadata = json.loads(result_path.read_text())
    records = []
    for test, result in zip(tests, metadata['records'], strict=True):
        pixels = sources[test.get('fixture', 'target')].astype(np.float32) / 255
        expected = grade.apply(pixels, test['grade'])
        if test.get('spotVisualization', {}).get('enabled'):
            threshold = test['spotVisualization']['threshold']
            luminance = expected @ np.array([0.2126, 0.7152, 0.0722])
            value = ((0.5 - luminance) * (2 + threshold * 7) + 0.5)
            expected = np.repeat(np.clip(np.clip(value, 0, 1) * (0.72 + threshold * 0.35),
                                         0, 1)[..., None], 3, axis=2)
        if test.get('compare'):
            expected[:, :64] = reverse[:, :64] / 255
        actual = np.fromfile(test['raw'], dtype=np.uint8).reshape(result['height'], result['width'], 4)
        actual = actual[::-1, :, :3].astype(np.float32) / 255
        record = compare_images(test['name'], expected, actual, output_dir / 'pixels')
        record['reference'] = 'Python grade.apply (separate CPU implementation)'
        records.append(record)
        display = np.asarray(Image.open(test['screenshot']).convert('RGB')).astype(np.float32) / 255
        records.append(compare_images(test['name'] + '-compositor', actual, display,
            output_dir / 'display', {'mean': 0.1, 'p95': 0.0, 'max': 1.0}))
    return {'records': records, 'browser': metadata['browserVersion'],
            'renderer': metadata['records'][0]['renderer'],
            'coverage': 'Production WebGL shader and composited canvas, isolated from full app UI'}
