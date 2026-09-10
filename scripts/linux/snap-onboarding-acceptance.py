#!/usr/bin/env python3
"""Fresh native onboarding and backup restoration in an ephemeral strict Snap."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode

spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).with_name('desktop-acceptance.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
assert os.environ['SNAP_NAME'] == 'lighttable'
common = Path(os.environ['SNAP_USER_COMMON'])
bundle = (Path(os.environ['SNAP']) / 'LightTable').resolve()
root = common / 'onboarding-state'
output = common / 'onboarding-evidence'
output.mkdir(exist_ok=True)
assert not root.exists(), 'Use a fresh ephemeral installation'
for name in ('runtime', 'photos', 'import/photos', 'config/lighttable'):
    (root / name).mkdir(parents=True, mode=0o700)
# Select a language but never seed firstRunSetup or a catalog source.
(root / 'config/lighttable/prefs.json').write_text(json.dumps({
    'locale': 'en', 'localeChosen': True, 'allowAutomation': True,
    'viewMode': 'detail', 'newPhotoDefaults': {'filmEnabled': False},
    'automaticUpdateChecks': False, 'writeSidecars': False,
    'backupDirectory': str(root / 'backups')}))
from PIL import Image
photo = root / 'import/photos/smoke.png'
Image.new('RGB', (128, 128), (50, 150, 210)).save(photo)
digest = hashlib.sha256(photo.read_bytes()).hexdigest()
environment = a.isolated_environment(root)
for key in ('XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE'):
    if key in os.environ:
        environment[key] = os.environ[key]
deadline, tokens, children = time.monotonic() + 300, set(), []
report = {'ok': False, 'confinement': 'strict', 'graphics': 'software VM'}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

def launch():
    with (root / 'desktop.log').open('a') as log:
        process = subprocess.Popen([str(bundle / 'bin/lighttable-desktop')], env=environment,
                                   stdout=log, stderr=log, start_new_session=True)
    children.append(process)
    api, health = a.connect(bundle, process, root, deadline, tokens)
    a.wait_for(process, deadline, 'native client', lambda: api.request('/api/ui/state').get('client'))
    return process, api, health

def checkpoint(name):
    (output / (name + '.request')).touch()
    limit = min(deadline, time.monotonic() + 25)
    while not (output / (name + '.done')).exists():
        a.require(time.monotonic() < limit, 'Host interaction acknowledgment missing: ' + name)
        time.sleep(.1)

def close(process, health):
    receipt = a.normal_close(process, health['pid'], deadline)
    a.cleanup(process)
    children.remove(process)
    return receipt

try:
    report['build'] = a.validate_bundle(bundle, 'be537f2f3e2e431ae6b42af716c2a8b365f57bab')
    process, api, health = launch()
    a.require(not api.request('/api/prefs').get('firstRunSetup'), 'Onboarding was pre-completed')
    checkpoint('onboarding-choices')
    checkpoint('onboarding-folder')
    a.wait_for(process, deadline, 'onboarding-added source',
               lambda: api.request('/api/catalog/query', {'limit': 10}).get('items'))
    checkpoint('onboarding-result')
    a.wait_for(process, deadline, 'durable onboarding completion',
        lambda: api.request('/api/prefs').get('firstRunSetup', {}).get('status') == 'completed')
    report['setup'] = api.request('/api/prefs')['firstRunSetup']
    name = a.render_photo(process, api, deadline, photo)
    route = '/api/state?' + urlencode({'name': name})
    a.send_ui(api, 'slider', {'key': 'exposure', 'value': .5})
    a.send_ui(api, 'rating:4')
    a.wait_for(process, deadline, 'saved baseline edits', lambda: a.edits_saved(api.request(route)))
    backup = api.request('/api/catalog/backup', {})
    archive = Path(backup['archive'])
    a.require(archive.is_file(), 'Backup archive missing')
    report['backup_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    a.send_ui(api, 'rating:1')
    a.wait_for(process, deadline, 'post-backup edit', lambda: api.request(route).get('rating') == 1)
    checkpoint('before-restore')
    restored = api.request('/api/recovery', {'action': 'restore', 'archive': str(archive)})
    a.require(restored.get('restart') is True, 'Restore did not request a clean restart')
    # The API deliberately restarts the server; terminate only owned test
    # processes, then prove restoration in a new native desktop process.
    a.cleanup(process)
    children.remove(process)
    process, api, health = launch()
    reopened_name = a.render_photo(process, api, deadline, photo)
    a.require(reopened_name == name, 'Restore changed photo identity')
    a.wait_for(process, deadline, 'restored rating and exposure', lambda: a.edits_saved(api.request(route)))
    a.require(api.request('/api/prefs')['firstRunSetup']['status'] == 'completed', 'Reopen lost onboarding')
    checkpoint('restored-photo')
    report['restored_rating'] = api.request(route)['rating']
    report['restored_exposure'] = api.request(route)['grade']['exposure']
    report['restore'] = restored
    report['final_close'] = close(process, health)
    a.require(hashlib.sha256(photo.read_bytes()).hexdigest() == digest, 'Original photo changed')
    report.update(ok=True, onboarding_completed=True, backup_restored_in_new_process=True,
                  source_preserved=True)
except BaseException as error:
    report['error'] = a.redact(str(error), tokens)
    report['startup_records'] = a.startup_snapshot(root)
    raise
finally:
    for process in reversed(children):
        a.cleanup(process)
    for path in (root / 'desktop.log', root / 'state/lighttable/logs/server.log'):
        if path.exists():
            (output / path.name).write_text(a.redact(path.read_text(errors='replace')[-24000:], tokens))
    (output / 'report.json').write_text(a.redact(json.dumps(report, indent=2), tokens) + '\n')
