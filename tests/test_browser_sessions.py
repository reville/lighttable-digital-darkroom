# SPDX-License-Identifier: GPL-3.0-only
"""Browser cookies shared by localhost ports must keep both instances writable."""
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
import threading
import unittest
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json
import sys
import server
server.INSTANCE_TOKEN = 'synthetic-browser-test-' + sys.argv[1]
http = server.LightTableServer(('127.0.0.1', 0), server.Handler)
print('READY ' + str(http.server_port), flush=True)
http.serve_forever()
'''


class BrowserSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.instances = []
        for name in ('first', 'second'):
            root = Path(temporary.name) / name
            (root / 'photos').mkdir(parents=True)
            env = dict(os.environ, LIGHTTABLE_DIR=str(root / 'photos'),
                       LIGHTTABLE_PREFS_FILE=str(root / 'prefs.json'),
                       LIGHTTABLE_CATALOG_FILE=str(root / 'catalog.sqlite3'),
                       LIGHTTABLE_CACHE_DIR=str(root / 'cache'),
                       LIGHTTABLE_CATALOG_MIRROR='0', LIGHTTABLE_WATCH='0')
            log = (root / 'server.log').open('w')
            cls.addClassCleanup(log.close)
            child = subprocess.Popen([sys.executable, '-u', '-c', CHILD, name],
                                     cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                     stderr=log, text=True)
            cls.addClassCleanup(cls.stop, child)
            ready = Queue()

            def read_ready(process=child, result=ready):
                for _ in range(20):
                    line = process.stdout.readline()
                    if line.startswith('READY ') or not line:
                        result.put(line)
                        return

            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            try:
                line = ready.get(timeout=30)
            except Empty:
                raise AssertionError('isolated browser-session server did not start') from None
            if not line.startswith('READY '):
                raise AssertionError('isolated browser-session server exited: '
                                     + (root / 'server.log').read_text()[-1500:])
            reader.join(timeout=5)
            port = int(line.removeprefix('READY '))
            cls.instances.append((f'http://127.0.0.1:{port}', port, name))

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        child.stdout.close()

    def setUp(self):
        self.cookies = CookieJar()
        self.browser = build_opener(HTTPCookieProcessor(self.cookies))

    def request(self, base, path, body=None, headers=None):
        headers = {'Content-Type': 'application/json', **(headers or {})}
        if body is not None:
            headers.setdefault('Origin', base)
        request = Request(base + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            response = self.browser.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, json.loads(data) if path.startswith('/api/') else data

    def test_opening_and_reloading_another_instance_preserves_both_sessions(self):
        for base, _port, name in self.instances:
            self.assertEqual(self.request(base, '/')[0], 200)
            self.assertEqual(self.request(base, '/api/prefs', {'sessionTest': name}), (200, {'ok': True}))
        for reload_instance in [None, *self.instances, *reversed(self.instances)]:
            if reload_instance:
                self.assertEqual(self.request(reload_instance[0], '/')[0], 200)
            for base, _port, name in self.instances:
                self.assertEqual(self.request(base, '/api/prefs', {'sessionTest': name}), (200, {'ok': True}))
                self.assertEqual(self.request(base, '/api/prefs')[1]['sessionTest'], name)
                self.assertEqual(self.request(base, '/api/ui/state', {'client': name}), (200, {'ok': True}))
                self.assertEqual(self.request(base, '/api/ui/state')[1]['client'], name)
        expected = {f'lighttable_token_{port}' for _base, port, _name in self.instances}
        self.assertEqual({cookie.name for cookie in self.cookies}, expected)
        for cookie in self.cookies:
            self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
            self.assertEqual(cookie.get_nonstandard_attr('SameSite'), 'Strict')
            self.assertEqual(cookie.path, '/')

    def test_another_instances_cookie_or_token_cannot_authorize_a_change(self):
        first, second = self.instances
        self.assertEqual(self.request(first[0], '/')[0], 200)
        self.assertEqual(self.request(second[0], '/api/prefs', {'sessionTest': 'rejected'})[0], 401)
        self.assertEqual(self.request(second[0], '/')[0], 200)
        self.assertEqual(self.request(second[0], '/api/prefs', {'sessionTest': 'rejected'},
                                      {'X-LightTable-Token': 'synthetic-browser-test-' + first[2]})[0], 401)
        self.assertEqual(self.request(second[0], '/api/prefs', {'sessionTest': 'rejected'},
                                      {'Origin': first[0]})[0], 403)
        self.assertEqual(self.request(second[0], '/api/prefs', {'sessionTest': second[2]}), (200, {'ok': True}))

    def test_opening_an_instance_preserves_an_existing_legacy_cookie(self):
        first, second = self.instances
        legacy = Cookie(version=0, name='lighttable_token',
                        value='synthetic-browser-test-' + first[2],
                        port=None, port_specified=False,
                        domain='127.0.0.1', domain_specified=False,
                        domain_initial_dot=False, path='/', path_specified=True,
                        secure=False, expires=None, discard=True,
                        comment=None, comment_url=None,
                        rest={'HttpOnly': None, 'SameSite': 'Strict'})
        self.cookies.set_cookie(legacy)
        self.assertEqual(self.request(second[0], '/')[0], 200)
        self.assertIn(legacy, list(self.cookies))
        for base, _port, name in self.instances:
            self.assertEqual(self.request(base, '/api/prefs', {'sessionTest': name}), (200, {'ok': True}))


if __name__ == '__main__':
    unittest.main()
