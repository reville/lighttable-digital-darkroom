"""Run the actual recovery journey against the real state API and catalog."""
import json
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from unittest import mock

import server
from tests.test_server_catalog import CatalogServerTestCase


ROOT = Path(__file__).resolve().parents[1]
JOURNEY = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const [root, url, name] = process.argv.slice(1);
const read = file => readFileSync(`${root}/web/${file}`, 'utf8');
const moduleFor = file => import(`data:text/javascript;base64,${Buffer.from(read(file)).toString('base64')}`);
const {createEditSaveQueue} = await moduleFor('edit-save-queue.js');
const {createEditRecovery, recoveryPayloadMatches} = await moduleFor('edit-recovery.js');
const {createCloseBarrier} = await moduleFor('close-barrier.js');
const {GRADE_DEFAULTS} = await moduleFor('gl.js');
const records = new Map();
const storage = {get length() {return records.size;}, key: i => [...records.keys()][i],
  getItem: key => records.get(key) ?? null, setItem: (key, value) => records.set(key, value),
  removeItem: key => records.delete(key)};
const window = {fetch: (path, options) => fetch(url + path, options)};
const getJSON = async path => {
  const response = await window.fetch(path);
  assert.equal(response.status, 200);
  return response.json();
};
const data = await getJSON('/api/images');
const createJournal = options => createEditRecovery({...options, storage});
const editRecovery = createJournal({scope: data.catalog?.path || `folder:${data.folder}`});
const writes = [];
const editSaveQueue = createEditSaveQueue({journal: editRecovery, send: async (_, payload) => {
  const response = await window.fetch('/api/state', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload.state)});
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || 'Save failed');
  writes.push(await getJSON(`/api/state?name=${encodeURIComponent(name)}`));
}});
const closeBarrier = createCloseBarrier({capture: () => {}, setBlocked: () => {},
  flush: async () => {try {await editSaveQueue.flush(); return true;} catch {return false;}}});
window.lightTablePrepareToClose = () => closeBarrier.prepare();
window.lightTableCancelClose = () => closeBarrier.cancel();
const source = read('app.js').match(/^async function runEditRecoveryJourney\([^]*?^}/m)[0];
const context = {window, cur: () => ({name}), getJSON, GRADE_DEFAULTS, editSaveQueue,
  editRecovery, createEditRecovery: createJournal, nativeJournalRequest: undefined,
  recoveryPayloadMatches};
vm.runInNewContext(source + '\nglobalThis.run = runEditRecoveryJourney;', context);
try {
  const result = await context.run();
  console.log(JSON.stringify({result, writes, remainingDrafts: (await editRecovery.list()).length}));
} catch (error) {
  console.log(JSON.stringify({error: error.message, writes}));
  process.exitCode = 1;
}
"""


class NativeRecoveryJourneyTests(CatalogServerTestCase):
    def run_journey(self):
        if not shutil.which('node'):
            self.skipTest('requires Node.js')

        class Handler(server.Handler):
            def _enforce_security(self, **kwargs):
                pass

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                ['node', '--input-type=module', '--eval', JOURNEY, str(ROOT),
                 f'http://127.0.0.1:{http.server_port}', self.qualified('a.jpg')],
                capture_output=True, text=True, timeout=30,
            )
            self.assertTrue(result.stdout.strip(), result.stderr)
            return result.returncode, json.loads(result.stdout)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)

    def test_unedited_photo_recovery_saves_and_restores_only_the_grade(self):
        name = self.qualified('a.jpg')
        original = server.catalog_entry_for(name)
        code, result = self.run_journey()
        self.assertEqual(code, 0, result)
        self.assertTrue(all(result['result'].values()))
        self.assertEqual(result['remainingDrafts'], 0)
        self.assertEqual([state['grade']['exposure'] for state in result['writes']], [.321, 0])
        for state in result['writes']:
            self.assertEqual({key: value for key, value in state.items() if key != 'grade'},
                             {key: value for key, value in original.items() if key != 'grade'})

    def test_existing_film_edits_and_metadata_survive_the_recovery_journey(self):
        name = self.qualified('a.jpg')
        state, _ = server.cleaned_state_request({
            'name': name, 'params': {'profile_enabled': False},
            'grade': {'exposure': 1.25, 'contrast': .2},
            'crop': {'x': .1, 'y': .2, 'w': .7, 'h': .6},
            'rating': 4, 'keywords': ['Keep'],
        }, strict=False)
        server.save_image_state(name, state)
        original = server.catalog_entry_for(name)
        code, result = self.run_journey()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['writes'][-1], original)
        self.assertEqual(result['writes'][0]['grade']['exposure'], .321)

    def test_recovery_still_rejects_a_success_response_without_a_saved_edit(self):
        with mock.patch.object(server, 'save_image_states'):
            code, result = self.run_journey()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['error'], 'Recovered edit did not reach the catalog')
