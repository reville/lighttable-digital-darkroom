"""Check the real application UI against its CPU CLI rendering endpoint."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np
from PIL import Image
from processing_support import compare_images, target_rgb8, run_browser

ROOT = Path(__file__).resolve().parents[1]


def run(output_dir):
    from bench.benchmark import server_environment, stop_process, find_playwright_module, find_playwright_browser
    import film_pipeline as fp
    from processing_export import ensure_resident
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    photos = output / 'photos'
    photos.mkdir(exist_ok=True)
    Image.fromarray(target_rgb8()).save(photos / 'a.png')
    Image.fromarray(target_rgb8(96, 128)).save(photos / 'b.png')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env = server_environment(photos, port, output / 'cache')
    env['SPEKTRAFILM_BACKEND'] = 'cpu'
    base = f'http://127.0.0.1:{port}'
    params = fp.clean_params({'grain_on':False, 'halation_on':False, 'glare_on':False,
        'auto_exposure':False, 'camera_diffusion_strength':0, 'scan_softness':0,
        'scan_sharpen':False, 'linear_input':False})
    cases = [
        {'name':'film-off-slider', 'params':{**params, 'profile_enabled':False}, 'exposure':0.45},
        {'name':'film-on-slider', 'params':params, 'exposure':-0.35},
        {'name':'print-exposure-slider', 'params':{**params, 'print_exposure':1.6}, 'exposure':0.2},
        {'name':'navigate-portrait', 'params':{**params, 'profile_enabled':False}, 'exposure':0.3, 'photo':1},
        {'name':'return-landscape', 'params':{**params, 'profile_enabled':False}, 'exposure':-0.2},
    ]
    for case in cases:
        case.update(raw=str(output / f"{case['name']}.rgba"),
                    reference=str(output / f"{case['name']}-cli.png"),
                    display=str(output / f"{case['name']}-display.png"),
                    screenshot=str(output / f"{case['name']}-app.png"))
    module = find_playwright_module()
    if not module or not shutil.which('node'):
        raise RuntimeError('Application gate requires Node and Playwright')
    browser = find_playwright_browser()
    config = output / 'config.json'
    config.write_text(json.dumps({'baseUrl':base, 'module':str(module),
        'browser':os.environ.get('LIGHTTABLE_BROWSER_EXECUTABLE') or (str(browser) if browser else None),
        'cases':cases, 'result':str(output / 'frames.json')}))
    binary, build_record = ensure_resident(output)
    if binary is None:
        return {'records':[build_record]}
    # A private runtime layout selects exactly the newly built engine through
    # normal production discovery, without overwriting any developer install.
    with tempfile.TemporaryDirectory(prefix='lighttable-processing-app-') as temporary, \
            (output / 'server.log').open('w') as log:
        runtime = Path(temporary)
        env = server_environment(photos, port, runtime / 'cache')
        env['SPEKTRAFILM_BACKEND'] = 'cpu'
        for source_file in ROOT.glob('*.py'):
            shutil.copy2(source_file, runtime / source_file.name)
        shutil.copy2(ROOT / 'media-formats.json', runtime / 'media-formats.json')
        for name in ('web', 'profiles', 'vendor', 'film_lab_ai', 'scripts', 'lighttable_cli'):
            (runtime / name).symlink_to(ROOT / name, target_is_directory=True)
        (runtime / 'engine').mkdir()
        (runtime / 'engine/data').symlink_to(ROOT / 'engine/data', target_is_directory=True)
        (runtime / 'engine/lighttable-engine').symlink_to(binary)
        server = subprocess.Popen([sys.executable, str(runtime / 'server.py')], cwd=runtime,
                                  env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError('Isolated application server exited; see server.log')
                try:
                    with urllib.request.urlopen(base + '/api/images', timeout=2) as response:
                        if len(json.load(response).get('images', [])) == 2:
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError('Isolated application server did not become ready within 90 seconds')
            result = run_browser([shutil.which('node'), str(ROOT / 'tests/processing_app.mjs'), str(config)],
                cwd=ROOT, timeout=600)
            (output / 'browser.log').write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError(f'Application pixel journey failed: {result.stderr[-1600:]}')
        finally:
            stop_process(server)
    frames = json.loads((output / 'frames.json').read_text())['records']
    records = [build_record]
    for case, frame in zip(cases, frames, strict=True):
        reference = np.asarray(Image.open(case['reference']).convert('RGB')).astype(float) / 255
        actual = np.fromfile(case['raw'], dtype=np.uint8).reshape(frame['height'], frame['width'], 4)
        record = compare_images(case['name'], reference, actual[::-1, :, :3] / 255, output / 'pixels')
        record.update(photo=frame['photo'], reference='Actual /api/render/file CLI route (Python CPU grade)',
                      screenshot=case['screenshot'], grade=frame['grade'])
        records.append(record)
        display = np.asarray(Image.open(case['display']).convert('RGB')).astype(float) / 255
        # CSS canvas presentation scales the rendered RGB8 image bilinearly.
        bounds = frame['bounds']
        width, height = round(bounds['width']), round(bounds['height'])
        # Element screenshots round their clip outward. A fractional canvas
        # origin can include a row/column of background outside the photo.
        # Compare the rasterized photo rectangle computed from DOM geometry;
        # no image registration or data-dependent border trimming is used.
        x = math.floor(bounds['x'] + 0.5) - math.floor(bounds['x'])
        y = math.floor(bounds['y'] + 0.5) - math.floor(bounds['y'])
        display = display[y:y+height, x:x+width]
        expected_display = np.asarray(Image.fromarray(actual[::-1, :, :3]).resize(
            (width, height), Image.Resampling.BILINEAR)).astype(float) / 255
        records.append(compare_images(case['name'] + '-visible-canvas', expected_display,
                                      display, output / 'display'))
    return {'records':records, 'coverage':'Full browser app, server, UI slider/save, navigation, CLI pixel parity'}
