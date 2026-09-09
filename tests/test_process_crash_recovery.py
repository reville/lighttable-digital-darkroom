"""Kill a real state-API process at commit boundaries and reopen its catalog."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, signal
from http.server import ThreadingHTTPServer
from pathlib import Path
import catalog_scan, server
cat = server.open_catalog()
catalog_scan.scan_source(cat, server.PRIMARY_SOURCE_ID, read_metadata_for_new=False)
stage = os.environ.get('CRASH_STAGE')
save = server.save_image_states
def checkpoint():
    print('CHECKPOINT', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
def guarded(updates):
    if stage == 'before-commit': checkpoint()
    written = save(updates)
    if stage == 'after-commit': checkpoint()
    return written
server.save_image_states = guarded
http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
# The ephemeral test token stays inside these pipes and is never logged.
print(json.dumps({'port': http.server_port, 'token': server.INSTANCE_TOKEN}), flush=True)
http.serve_forever()
'''


class ProcessCrashRecoveryTests(unittest.TestCase):
    def start(self, root, stage=''):
        env = dict(os.environ, LIGHTTABLE_DIR=str(root/'photos'),
                   LIGHTTABLE_CATALOG_FILE=str(root/'catalog.sqlite3'),
                   LIGHTTABLE_PREFS_FILE=str(root/'prefs.json'),
                   LIGHTTABLE_CACHE_DIR=str(root/'cache'), LIGHTTABLE_CATALOG_MIRROR='0',
                   LIGHTTABLE_WATCH='0', CRASH_STAGE=stage, PYTHONUNBUFFERED='1')
        log = (root/'child.log').open('a')
        child = subprocess.Popen([sys.executable, '-u', '-c', CHILD], cwd=ROOT, env=env,
                                 stdout=subprocess.PIPE, stderr=log, text=True)
        self.addCleanup(log.close)
        self.addCleanup(self.stop, child)
        # Startup may write a non-sensitive catalog notice before READY.
        for _ in range(20):
            if not select.select([child.stdout], [], [], 10)[0]:
                self.fail('isolated server startup timed out; ' + (root/'child.log').read_text()[-1500:])
            line = child.stdout.readline()
            if line.startswith('{'):
                info = json.loads(line)
                return child, f"http://127.0.0.1:{info['port']}", info['token']
        self.fail('isolated server did not report ready')

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
        if child.stdout: child.stdout.close()

    @staticmethod
    def request(base, token, path, body=None):
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type':'application/json', 'X-LightTable-Token':token})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    @unittest.skipUnless(os.name == 'posix', 'deterministic process-stop boundary requires POSIX')
    def test_acknowledged_state_survives_and_interrupted_draft_replays(self):
        for stage in ('before-commit', 'after-commit', 'after-ack'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); (root/'photos').mkdir()
                photo = root/'photos/photo.png'
                Image.new('RGB', (32, 24), (30, 110, 190)).save(photo)
                original = hashlib.sha256(photo.read_bytes()).digest()
                child, base, token = self.start(root)
                self.request(base, token, '/api/state', {'name':'photo.png', 'grade':{'exposure':.2}})
                self.stop(child)
                child, base, token = self.start(root, stage if stage != 'after-ack' else '')
                pending = {'name':'photo.png', 'grade':{'exposure':.8}, 'rating':4}
                # A separately durable pending draft survives the server process.
                (root/'pending.json').write_text(json.dumps(pending))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    request = pool.submit(self.request, base, token, '/api/state', pending)
                    if stage == 'after-ack':
                        self.assertTrue(request.result(timeout=10)['ok'])
                    else:
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'commit checkpoint not reached')
                        self.assertEqual(child.stdout.readline().strip(), 'CHECKPOINT')
                    self.stop(child)
                    if stage != 'after-ack':
                        with self.assertRaises(Exception): request.result(timeout=10)
                child, base, token = self.start(root)
                saved = self.request(base, token, '/api/state?name=photo.png')
                self.assertEqual(saved['grade']['exposure'], .2 if stage == 'before-commit' else .8)
                self.assertTrue(self.request(base, token, '/api/state', json.loads((root/'pending.json').read_text()))['ok'])
                self.stop(child)
                child, base, token = self.start(root)
                saved = self.request(base, token, '/api/state?name=photo.png')
                self.assertEqual(saved['grade']['exposure'], .8)
                self.assertEqual(saved['rating'], 4)
                self.assertEqual(hashlib.sha256(photo.read_bytes()).digest(), original)
                self.stop(child)
