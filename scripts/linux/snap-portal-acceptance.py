#!/usr/bin/env python3
"""Exercise the real initial native folder portal in a strict Snap on private Xvfb."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).with_name('desktop-acceptance.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
common = Path(os.environ['SNAP_USER_COMMON'])
bundle = Path(os.environ['SNAP']) / 'LightTable'
root = common / 'portal-state'
output = common / 'portal-evidence'
output.mkdir(exist_ok=True)
assert not root.exists(), 'Use a fresh ephemeral test installation'
for name in ('runtime', 'config/lighttable'):
    (root / name).mkdir(parents=True, mode=0o700)
# This exercises the native initial folder chooser, independently of the web
# onboarding flow. No shell settings or remembered photo folder are seeded.
(root / 'config/lighttable/prefs.json').write_text(json.dumps({
    'locale': 'en', 'localeChosen': True, 'allowAutomation': True,
    'firstRunSetup': {'version': 1, 'status': 'completed', 'source': 'folder'},
    'viewMode': 'detail', 'newPhotoDefaults': {'filmEnabled': False},
    'automaticUpdateChecks': False, 'writeSidecars': False}))
(root / 'config/user-dirs.dirs').write_text('XDG_PICTURES_DIR="/nonexistent-lighttable-pictures"\n')
environment = a.isolated_environment(root)
environment.pop('LIGHTTABLE_DIR')
for key in ('XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE'):
    if key in os.environ:
        environment[key] = os.environ[key]
deadline, tokens, children = time.monotonic() + 210, set(), []
report = {'ok': False, 'confinement': 'strict', 'web_onboarding_tested': False}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

def launch():
    with (root / 'desktop.log').open('a') as log:
        process = subprocess.Popen([str(bundle / 'bin/lighttable-desktop')], env=environment,
                                   stdout=log, stderr=log, start_new_session=True)
    children.append(process)
    return process

def connect(process):
    def registration():
        for path in (root / 'instances').glob('[0-9]*.json'):
            value = json.loads(path.read_text())
            if a.owns_server(bundle, process, value.get('pid')):
                return value
    instance = a.wait_for(process, deadline, 'portal-selected folder registration', registration)
    tokens.add(instance['token'])
    api = a.API(instance, deadline)
    health = api.request('/api/health')
    folder = Path(health['folder'])
    a.require(health.get('ok') and not health.get('headless') and not health.get('safeMode'),
              'Native shell did not start a healthy desktop server')
    a.require('/doc/' in str(folder) and folder.name == 'photos', 'Expected a document-portal folder grant')
    a.require(a.same_path(health.get('catalog'), root / 'catalog/library.sqlite3'), 'Unexpected catalog')
    photo = folder / 'smoke.png'
    a.require(photo.is_file(), 'Selected portal directory did not expose its photo')
    a.render_photo(process, api, deadline, photo)
    return health, folder

def capture(name):
    (output / (name + '.request')).touch()
    limit = min(deadline, time.monotonic() + 12)
    while not (output / (name + '.done')).exists():
        a.require(time.monotonic() < limit, 'Host screenshot acknowledgment missing')
        time.sleep(.1)

try:
    report['build'] = a.validate_bundle(bundle, 'be537f2f3e2e431ae6b42af716c2a8b365f57bab')
    try:
        Path('/media/lighttable-portal/photos/smoke.png').read_bytes()
    except PermissionError:
        report['direct_removable_access_denied'] = True
    else:
        raise RuntimeError('Removable media was directly readable without connection')
    desktop = launch()
    health, folder = connect(desktop)
    report['selected_folder'] = str(folder)
    report['photo_sha256'] = hashlib.sha256((folder / 'smoke.png').read_bytes()).hexdigest()
    capture('portal-photo')
    report['first_close'] = a.normal_close(desktop, health['pid'], deadline)
    a.cleanup(desktop)
    children.remove(desktop)
    desktop = launch()
    reopened, new_folder = connect(desktop)
    a.require(new_folder == folder, 'Reopen lost the document-portal path')
    capture('portal-reopened')
    report['reopen_close'] = a.normal_close(desktop, reopened['pid'], deadline)
    report['grant_survived_reopen'] = True
    report['ok'] = True
except BaseException as error:
    report['error'] = a.redact(str(error), tokens)
    raise
finally:
    for process in reversed(children):
        a.cleanup(process)
    if (root / 'desktop.log').exists():
        (output / 'desktop.log').write_text(a.redact((root / 'desktop.log').read_text(errors='replace')[-24000:], tokens))
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
