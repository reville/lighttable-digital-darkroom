#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Temporary release tester; the downloaded application bytes remain unchanged."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlencode


def load(path):
    spec = importlib.util.spec_from_file_location('acceptance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--backend', choices=['x11', 'wayland'], required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--film', action='store_true')
    args = parser.parse_args()
    bundle, output = args.bundle.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    a = load(Path(__file__).with_name('desktop-acceptance.py'))
    build = a.validate_bundle(bundle, args.source)
    environment = a.isolated_environment
    def env(root):
        values = environment(root)
        values['GDK_BACKEND'] = args.backend
        if args.backend == 'wayland':
            values.pop('DISPLAY', None)
        return values
    a.isolated_environment = env
    def screenshot(path):
        if args.backend == 'wayland':
            subprocess.run(['grim', str(path)], check=True, timeout=10)
        else:
            subprocess.run(['import', '-window', 'root', str(path)], check=True, timeout=10)
    if args.backend == 'wayland':
        def normal_close(process, server_pid, deadline):
            tree = json.loads(subprocess.check_output(['swaymsg', '-t', 'get_tree', '-r'], timeout=5))
            pending, matches = [tree], []
            while pending:
                node = pending.pop()
                if node.get('pid') == process.pid and node.get('name', '').startswith('LightTable'):
                    matches.append(node)
                pending.extend(node.get('nodes', []) + node.get('floating_nodes', []))
            a.require(len(matches) == 1, 'Expected one owned Wayland window')
            receipt = json.loads(subprocess.check_output(['swaymsg', '-r', f'[con_id={matches[0]["id"]}] kill'], timeout=5))
            a.require(all(r.get('success') for r in receipt), 'Compositor close failed')
            process.wait(timeout=min(25, deadline-time.monotonic()))
            a.require(process.returncode == 0, 'Wayland normal close failed')
            a.require(not Path(f'/proc/{server_pid}/exe').exists(), 'Wayland close left its server alive')
            return {'protocol': 'xdg_toplevel.close', 'pid': process.pid, 'window': matches[0]['id']}
        a.normal_close = normal_close
    # Retain the existing precision gate, including normal close and persistence,
    # and take an actual desktop screenshot before its first normal close.
    original_close = a.normal_close
    def capture_close(process, pid, deadline):
        screenshot(output/'precision.png')
        return original_close(process, pid, deadline)
    a.normal_close = capture_close
    precision = output/'precision'
    precision.mkdir()
    if not args.film:
        a.run(bundle, args.source, 240, precision)
        precision_report = json.loads((precision/'report.json').read_text())
        precision_report['backend'] = args.backend
        (precision/'report.json').write_text(json.dumps(precision_report,indent=2)+'\n')
    a.normal_close = original_close
    report = {'build': build, 'backend': args.backend, 'graphics': 'software VM', 'cases': []}
    for fixture in sorted(args.fixtures.glob('*')):
        if fixture.suffix.upper() not in ('.CR2', '.RAF'):
            continue
        case = {'fixture': fixture.name, 'ok': False}
        report['cases'].append(case)
        children, tokens = [], set()
        deadline = time.monotonic()+480
        with tempfile.TemporaryDirectory(prefix='LightTable release photos ') as temporary:
            root = Path(temporary)
            for name in ('runtime', 'photos', 'config/lighttable'):
                (root/name).mkdir(parents=True, mode=0o700)
            source = root/'photos'/fixture.name
            shutil.copy2(fixture, source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (root/'config/lighttable/prefs.json').write_text(json.dumps({
                'locale':'en','localeChosen':True,'allowAutomation':True,'viewMode':'detail',
                'firstRunSetup':{'version':1,'status':'completed','source':'folder'},
                'newPhotoDefaults':{'filmEnabled':False},'automaticUpdateChecks':False,
                'writeSidecars':False,'backupDirectory':str(root/'backups')}))
            subprocess.run([str(bundle/'install.sh'), '--bin-dir', str(root/'commands')], env=env(root), check=True, timeout=30)
            a.require((root/'commands/lighttable-desktop').resolve()==bundle/'bin/lighttable-desktop', 'Install did not register the owned launcher')
            def launch():
                with (root/'desktop.log').open('a') as log:
                    process = subprocess.Popen([str(root/'commands/lighttable-desktop')], env=env(root), stdout=log, stderr=log, start_new_session=True)
                children.append(process)
                api, health = a.connect(bundle, process, root, deadline, tokens)
                name = a.render_photo(process, api, deadline, source)
                return process, api, health, name
            try:
                process, api, health, name = launch()
                a.send_ui(api, 'slider', {'key':'exposure','value':0.5})
                a.send_ui(api, 'rating:4')
                route = '/api/state?'+urlencode({'name':name})
                a.wait_for(process, deadline, 'saved RAW edit', lambda: a.edits_saved(api.request(route)))
                if args.film:
                    a.send_ui(api, 'filmToggle')
                    a.wait_for(process, deadline, 'saved film enabled state', lambda: api.request(route).get('params',{}).get('profile_enabled') is True)
                    a.wait_for(process, deadline, 'film rendering in native window', lambda: api.request('/api/ui/state').get('render',{}).get('state')=='ready')
                screenshot(output/(fixture.stem+'-edited.png'))
                result = api.request('/api/export', {'names':[name],'format':'tif','outputSpace':'srgb','destination':str(root/'exports'),'metadata':'none','sidecar':False,'collision':'rename'})
                a.require(result.get('queued'), 'RAW export was not queued')
                def exported():
                    status = api.request('/api/export/status')
                    a.require(not status.get('error') and not status.get('errors'), 'RAW export failed')
                    return not status.get('running') and status.get('done') == 1
                a.wait_for(process, deadline, 'RAW RGB16 export', exported)
                import tifffile
                paths = list((root/'exports').rglob('*.tif'))
                a.require(len(paths)==1, 'Expected one RAW export')
                with tifffile.TiffFile(paths[0]) as image:
                    a.SHAPE = image.series[0].shape
                import rawpy
                with rawpy.imread(str(source)) as raw:
                    dimensions = sorted((raw.sizes.iheight, raw.sizes.iwidth))
                a.require(sorted(a.SHAPE[:2]) == dimensions and a.SHAPE[-1]==3,
                          'RAW export did not preserve full decoded dimensions')
                case['export'] = a.verify_export(paths[0])
                case['first_close'] = a.normal_close(process, health['pid'], deadline)
                a.cleanup(process); children.remove(process)
                process, api, second, reopened_name = launch()
                a.require(second['pid']!=health['pid'] and name==reopened_name, 'RAW reopen identity failed')
                def saved():
                    value = api.request(route)
                    return value.get('rating')==4 and value.get('grade',{}).get('exposure')==0.5 and value.get('params',{}).get('profile_enabled') is args.film
                a.wait_for(process, deadline, 'RAW edit persistence', saved)
                screenshot(output/(fixture.stem+'-reopened.png'))
                case['final_close'] = a.normal_close(process, second['pid'], deadline)
                a.require(hashlib.sha256(source.read_bytes()).hexdigest()==digest, 'Source RAW changed')
                a.cleanup(process); children.remove(process)
                catalog = root/'catalog/library.sqlite3'
                catalog_digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
                subprocess.run([str(bundle/'uninstall.sh'), '--bin-dir', str(root/'commands')], env=env(root), check=True, timeout=30)
                a.require(not (root/'commands/lighttable-desktop').is_symlink(), 'Uninstall left its launcher')
                a.require(hashlib.sha256(catalog.read_bytes()).hexdigest()==catalog_digest and hashlib.sha256(source.read_bytes()).hexdigest()==digest, 'Uninstall changed user data')
                case.update(ok=True, original_sha256=digest, saved_exposure=0.5, saved_rating=4, film_enabled=args.film, install_and_uninstall_preserved_data=True)
            finally:
                for process in reversed(children): a.cleanup(process)
                for path in (root/'desktop.log',root/'state/lighttable/logs/server.log'):
                    if path.exists():
                        (output/(fixture.stem+'-'+path.name)).write_text(a.redact(path.read_text(errors='replace')[-24000:],tokens))
                (output/'raw-report.json').write_text(json.dumps(report,indent=2)+'\n')
    a.require(len(report['cases'])==2 and all(c['ok'] for c in report['cases']), 'Both RAW fixtures must pass')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
