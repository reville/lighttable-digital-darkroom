#!/usr/bin/env python3
"""Exercise real RAW/film catalogs before and after a host-owned Snap refresh.

Run with the installed Snap's Python, private Xvfb, and its systemd user bus. Helpers
and exact public fixtures live in SNAP_USER_COMMON/acceptance. No package-manager
operation runs here. The host acknowledges each raw-*-PHASE.request only after
capturing its private display; screenshot files are not fabricated by this test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode

# Exact records from demo-assets/cc0-raw/manifest.tsv; both are public CC0 samples.
FIXTURES = {
    '01-canon-eos-80d-city-tree.CR2': (20220625, 'f9c5404b57248c21e9b269721aa0d721c3b1281c94ebdee3c50bf32db42613a8'),
    '03-fujifilm-xq2-harbor-ferry.RAF': (19430400, '4bce88593f5ae8fc45aeeec8a6dee9a84c773493cc6db9dabdc28744c8ac4b4d'),
}
DEFAULT_SOURCE = 'be537f2f3e2e431ae6b42af716c2a8b365f57bab'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def saved_film(value):
    return (value.get('rating') == 4 and value.get('grade', {}).get('exposure') == 0.5
            and value.get('params', {}).get('profile_enabled') is True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('before', 'after'), default=os.environ.get('LIGHTTABLE_ACCEPTANCE_PHASE'))
    parser.add_argument('--expected-source', default=DEFAULT_SOURCE)
    parser.add_argument('--timeout', type=int, default=480, help='Per-case total budget, including cleanup (60–480 seconds)')
    args = parser.parse_args()
    if args.phase is None:
        parser.error('--phase=before or --phase=after is required')
    if not 60 <= args.timeout <= 480:
        parser.error('--timeout must be between 60 and 480 seconds')
    if sys.platform != 'linux' or not os.environ.get('DISPLAY') or not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        parser.error('Run inside Snap confinement with private Xvfb and the systemd user bus')
    if os.environ.get('SNAP_NAME') != 'lighttable' or not os.environ.get('SNAP_USER_COMMON'):
        parser.error('This helper must run inside the lighttable Snap')

    spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).with_name('desktop-acceptance.py'))
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    bundle = (Path(os.environ['SNAP']) / 'LightTable').resolve()
    common = Path(os.environ['SNAP_USER_COMMON']).resolve()
    evidence = common / 'acceptance-evidence'
    evidence.mkdir(mode=0o700, exist_ok=True)
    states = common / 'acceptance/raw-state'
    states.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixtures = common / 'acceptance/fixtures'
    report = {'ok': False, 'phase': args.phase, 'backend': 'x11', 'graphics': 'software VM',
              'snap_revision': os.environ.get('SNAP_REVISION'), 'cases': []}
    tokens = set()
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))

    def expire(_number, _frame):
        raise TimeoutError('RAW acceptance case exceeded its work budget')
    signal.signal(signal.SIGALRM, expire)

    def screenshot(fixture, process, deadline):
        checkpoint = evidence / f'raw-{fixture.stem}-{args.phase}'
        request, done = checkpoint.with_suffix('.request'), checkpoint.with_suffix('.done')
        # Never accept a stale acknowledgment from an earlier run.
        done.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        request.touch(mode=0o600)
        capture_deadline = min(deadline, time.monotonic() + 12)
        while not done.exists():
            a.require(process.poll() is None, 'Native desktop exited before screenshot acknowledgment')
            a.require(time.monotonic() < capture_deadline, 'Host did not capture the native RAW window')
            time.sleep(.1)
        return checkpoint.name

    def updates(api):
        # Managed installs intentionally include an error message in HTTP 200.
        remaining = api.deadline - time.monotonic()
        a.require(remaining > 0, 'No time remains for managed-update check')
        request = a.Request(f'http://127.0.0.1:{api.port}/api/updates',
                            headers={'X-LightTable-Token': api.token})
        with api.opener.open(request, timeout=min(5, remaining)) as response:
            a.require(response.status == 200, 'Snap updater status did not return HTTP 200')
            status = json.load(response)
        a.require(status.get('owner') == 'snap' and status.get('supported') is False,
                  'Snap install enabled the portable updater')
        return {'owner': 'snap', 'supported': False}

    try:
        report['build'] = a.validate_bundle(bundle, args.expected_source)
        a.require(json.loads((bundle / 'installation-owner.json').read_text()).get('owner') == 'snap',
                  'Bundle ownership is not Snap')
        for filename, (size, digest) in FIXTURES.items():
            fixture = fixtures / filename
            root = states / fixture.stem
            source = root / 'photos' / filename
            catalog = root / 'catalog/library.sqlite3'
            checkpoint = root / 'checkpoint.json'
            case = {'fixture': filename, 'ok': False}
            report['cases'].append(case)
            children = []
            # Reserve thirty seconds for the bounded owned-process cleanup.
            deadline = time.monotonic() + args.timeout - 30
            signal.setitimer(signal.ITIMER_REAL, args.timeout - 30)
            try:
                a.require(fixture.is_file() and not fixture.is_symlink() and fixture.stat().st_size == size
                          and sha256(fixture) == digest, 'RAW fixture differs from the recorded public sample')
                if args.phase == 'before':
                    a.require(not root.exists(), 'Before phase requires a fresh private RAW state directory')
                    for name in ('runtime', 'photos', 'config/lighttable'):
                        (root / name).mkdir(mode=0o700, parents=True)
                    shutil.copy2(fixture, source)
                    (root / 'config/lighttable/prefs.json').write_text(json.dumps({
                        'locale': 'en', 'localeChosen': True, 'allowAutomation': True, 'viewMode': 'detail',
                        'firstRunSetup': {'version': 1, 'status': 'completed', 'source': 'folder'},
                        'newPhotoDefaults': {'filmEnabled': False}, 'automaticUpdateChecks': False,
                        'writeSidecars': False, 'backupDirectory': str(root / 'backups')}))
                    prior = None
                else:
                    a.require(checkpoint.is_file(), 'Missing successful before-phase checkpoint')
                    prior = json.loads(checkpoint.read_text())
                    a.require(prior.get('fixture') == filename and prior.get('source_sha256') == digest
                              and prior.get('ok') is True, 'Invalid before-phase checkpoint')
                    a.require(prior.get('snap_revision') and report['snap_revision']
                              and prior['snap_revision'] != report['snap_revision'],
                              'After phase requires a different installed Snap revision')
                    a.require(catalog.is_file() and sha256(catalog) == prior['catalog_sha256'],
                              'Host refresh changed the closed saved catalog')
                    a.require(source.is_file() and sha256(source) == digest, 'Host refresh changed the source RAW')
                    case['refresh_preserved_catalog_bytes'] = True
                    case['previous_snap_revision'] = prior['snap_revision']

                def launch():
                    environment = a.isolated_environment(root)
                    for key in ('XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE'):
                        if key in os.environ:
                            environment[key] = os.environ[key]
                    with (root / 'desktop.log').open('a') as log:
                        process = subprocess.Popen([str(bundle / 'bin/lighttable-desktop')], env=environment,
                                                   stdout=log, stderr=log, start_new_session=True)
                    children.append(process)
                    api, health = a.connect(bundle, process, root, deadline, tokens)
                    name = a.render_photo(process, api, deadline, source)
                    return process, api, health, name

                def close(process, server_pid):
                    receipt = a.normal_close(process, server_pid, deadline)
                    a.cleanup(process)
                    children.remove(process)
                    return receipt

                process, api, health, name = launch()
                case['updater'] = updates(api)
                route = '/api/state?' + urlencode({'name': name})
                if args.phase == 'before':
                    a.send_ui(api, 'slider', {'key': 'exposure', 'value': .5})
                    a.send_ui(api, 'rating:4')
                    a.wait_for(process, deadline, 'saved exposure/rating', lambda: a.edits_saved(api.request(route)))
                    a.send_ui(api, 'filmToggle')
                else:
                    a.require(name == prior['photo_name'], 'Snap refresh changed the saved photo identity')
                a.wait_for(process, deadline, 'saved film/exposure/rating', lambda: saved_film(api.request(route)))
                a.wait_for(process, deadline, 'native film rendering',
                           lambda: api.request('/api/ui/state').get('render', {}).get('state') == 'ready')

                destination = root / 'exports' / args.phase
                a.require(not destination.exists(), 'Export destination must be fresh for this phase')
                result = api.request('/api/export', {'names': [name], 'format': 'tif', 'outputSpace': 'srgb',
                    'destination': str(destination), 'metadata': 'none', 'sidecar': False, 'collision': 'rename'})
                a.require(result.get('queued'), 'Film RAW export was not queued')
                def exported():
                    status = api.request('/api/export/status')
                    a.require(not status.get('error') and not status.get('errors'), 'Film RAW export reported an error')
                    return not status.get('running') and status.get('done') == 1
                a.wait_for(process, deadline, 'full-size film RGB16 export', exported)
                paths = list(destination.rglob('*.tif'))
                a.require(len(paths) == 1, 'Expected exactly one film TIFF export')
                import rawpy
                import tifffile
                with rawpy.imread(str(source)) as raw:
                    dimensions = sorted((raw.sizes.iheight, raw.sizes.iwidth))
                with tifffile.TiffFile(paths[0]) as image:
                    a.SHAPE = image.series[0].shape
                a.require(len(a.SHAPE) == 3 and sorted(a.SHAPE[:2]) == dimensions and a.SHAPE[-1] == 3,
                          'Film export did not preserve full decoded RAW dimensions')
                case['export'] = a.verify_export(paths[0])
                case['first_close'] = close(process, health['pid'])
                process, api, reopened, reopened_name = launch()
                a.require(reopened['pid'] != health['pid'] and reopened_name == name,
                          'Normal reopen failed to preserve the photo in a new server')
                a.wait_for(process, deadline, 'film/edit persistence after normal reopen',
                           lambda: saved_film(api.request(route)))
                updates(api)
                case['screenshot_checkpoint'] = screenshot(fixture, process, deadline)
                case['final_close'] = close(process, reopened['pid'])
                a.require(sha256(source) == digest and sha256(fixture) == digest, 'Source RAW bytes changed')
                case.update(ok=True, source_sha256=digest, photo_name=name, saved_exposure=.5,
                            saved_rating=4, film_enabled=True, source_preserved=True,
                            catalog_sha256=sha256(catalog), snap_revision=report['snap_revision'])
                if args.phase == 'before':
                    checkpoint.write_text(json.dumps(case, indent=2) + '\n')
                    checkpoint.chmod(0o600)
            except BaseException as error:
                case['error'] = a.redact(str(error), tokens)
                case['startup_records'] = a.startup_snapshot(root)
                raise
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                try:
                    for process in reversed(children):
                        a.cleanup(process)
                finally:
                    # A startup failure may precede connect(); redact any token
                    # already written to this private case's registrations too.
                    for registration in (root / 'instances').glob('[0-9]*.json'):
                        try:
                            token = json.loads(registration.read_text()).get('token')
                            if isinstance(token, str) and token:
                                tokens.add(token)
                        except (OSError, ValueError, TypeError):
                            pass
                    for path in (root / 'desktop.log', root / 'state/lighttable/logs/server.log'):
                        if path.exists():
                            (evidence / f'raw-{fixture.stem}-{args.phase}-{path.name}').write_text(
                                a.redact(path.read_text(errors='replace')[-24000:], tokens))
                    (evidence / f'raw-{args.phase}-report.json').write_text(
                        a.redact(json.dumps(report, indent=2), tokens) + '\n')
        a.require(len(report['cases']) == 2 and all(case['ok'] for case in report['cases']),
                  'Both exact RAW fixtures must pass')
        report['ok'] = True
    except BaseException as error:
        report['error'] = a.redact(str(error), tokens)
        raise
    finally:
        text = a.redact(json.dumps(report, indent=2), tokens) + '\n'
        (evidence / f'raw-{args.phase}-report.json').write_text(text)
        print(text)


if __name__ == '__main__':
    main()
