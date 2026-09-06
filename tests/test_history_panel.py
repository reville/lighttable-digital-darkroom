"""Behavioral history checks with deferred transport and a deterministic clock."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = """
import {createHistoryPanel} from './web/history-panel.js';
let clock = 10000, nextTimer = 1;
const timers = new Map();
Date.now = () => clock;
globalThis.setTimeout = (run, delay) => {
  const id = nextTimer++; timers.set(id, {run, at: clock + delay}); return id;
};
globalThis.clearTimeout = id => timers.delete(id);
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
async function advance(ms) {
  clock += ms;
  for (const [id, timer] of [...timers]) {
    if (timer.at <= clock) { timers.delete(id); timer.run(); }
  }
  await settle();
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const nodes = Object.fromEntries(['historyList', 'historyClear', 'historyPane'].map(id => [id, {
  innerHTML: '', textContent: '', disabled: false, handlers: {},
  classList: {contains: () => false},
  addEventListener(type, callback) { this.handlers[type] = callback; },
}]));
const posts = [], notices = [], restored = [], statuses = [], captureRestores = [];
let get = async () => ({steps: []});
let post = async (path, body) => { posts.push({path, body}); return {ok: true}; };
let enabled = true;
const h = createHistoryPanel({
  el: id => nodes[id], post: (...args) => post(...args), get: (...args) => get(...args),
  toast: message => notices.push(message), enabled: () => enabled,
  onRestore: state => restored.push(state),
  onStatus: status => statuses.push(status),
  onRestoreCaptureTime: async (name, historyId) => captureRestores.push({name, historyId}),
});
"""


@unittest.skipUnless(shutil.which('node'), 'Node required')
class HistoryPanelTests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run(['node', '--input-type=module', '-e', HARNESS + body],
                                cwd=ROOT, text=True, capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_capture_history_uses_metadata_restore_without_replaying_pixel_state(self):
        result = self.run_js("""
await h.refresh('a', true);
get = async () => ({captureTimeOnly:true,captureTimeOverride:null});
await nodes.historyList.handlers.click({target:{closest:()=>({dataset:{id:'12'}})}});
console.log(JSON.stringify({restored,captureRestores}));
""")
        self.assertEqual(result['restored'], [])
        self.assertEqual(result['captureRestores'], [{'name': 'a', 'historyId': 12}])

    def test_final_slider_snapshot_flushes_without_another_interaction(self):
        result = self.run_js("""
h.record('a', 'Light', {exposure: 0.1}); await settle();
await advance(400); h.record('a', 'Light', {exposure: 0.2});
await advance(400); h.record('a', 'Light', {exposure: 0.3});
h.record('a', 'Light', {exposure: 0.3});
const before = posts.map(p => p.body.state.exposure);
await advance(1200);
console.log(JSON.stringify({before, after: posts.map(p => p.body.state.exposure), timers: timers.size}));
""")
        self.assertEqual(result['before'], [0.1])
        self.assertEqual(result['after'], [0.1, 0.3])
        self.assertEqual(result['timers'], 0)

    def test_each_photo_keeps_its_own_pending_snapshot(self):
        result = self.run_js("""
h.record('a', 'Light', {value: 'a1'}); h.record('a', 'Light', {value: 'a2'});
h.record('b', 'Light', {value: 'b1'}); h.record('b', 'Light', {value: 'b2'});
await h.flush();
const byPhoto = Object.fromEntries(['a', 'b'].map(name => [name,
  posts.filter(p => p.body.name === name).map(p => p.body.state.value)]));
console.log(JSON.stringify({byPhoto, timers: timers.size}));
""")
        self.assertEqual(result['byPhoto'], {'a': ['a1', 'a2'], 'b': ['b1', 'b2']})
        self.assertEqual(result['timers'], 0)

    def test_tool_change_preserves_pending_step_and_serializes_sends(self):
        result = self.run_js("""
const first = deferred();
post = async (path, body) => {
  posts.push({path, body});
  if (body.state.value === 'a1') await first.promise;
  return {ok: true};
};
h.record('a', 'Light', {value: 'a1'}); await settle();
h.record('a', 'Light', {value: 'a2'});
h.record('a', 'Crop', {value: 'a3'});
let done = false; const flush = h.flush().then(() => done = true);
await settle();
const blocked = {sent: posts.map(p => p.body.state.value), done};
first.resolve(); await flush;
console.log(JSON.stringify({blocked, sent: posts.map(p => p.body.state.value)}));
""")
        self.assertEqual(result['blocked'], {'sent': ['a1'], 'done': False})
        self.assertEqual(result['sent'], ['a1', 'a2', 'a3'])

    def test_record_captures_immutable_immediate_and_pending_states(self):
        result = self.run_js("""
const first = {masks: [{points: [1]}]};
h.record('a', 'Mask', first); first.masks[0].points.push(2);
const pending = {masks: [{points: [3]}]};
h.record('a', 'Mask', pending); pending.masks[0].points.push(4);
await h.flush();
console.log(JSON.stringify(posts.map(p => p.body.state.masks[0].points)));
""")
        self.assertEqual(result, [[1], [3]])

    def test_failed_write_survives_its_window_and_retries_before_later_steps(self):
        result = self.run_js("""
let online = false;
const persisted = [];
post = async (path, body) => {
  posts.push({path, body});
  if (body.name === 'a' && !online) throw new Error('offline');
  persisted.push(body.name + body.state.value);
  return {ok: true};
};
h.record('a', 'Light', {value: 1});
h.record('a', 'Crop', {value: 2});
h.record('a', 'Remove', {value: 3});
h.record('b', 'Light', {value: 1});
await settle(); await advance(2000);
const first = {pending: h.hasPending, persisted: [...persisted], status: statuses.at(-1)};
const failedFlush = await h.flush();
const stillPending = h.hasPending;
online = true;
const retry = await h.retry();
console.log(JSON.stringify({first, failedFlush, stillPending, retry, persisted,
  sent: posts.map(p => p.body.name + p.body.state.value), notices,
  finished: h.hasPending, status: statuses.at(-1)}));
""")
        self.assertEqual(result['first'], {
            'pending': True, 'persisted': ['b1'],
            'status': {'pendingNames': ['a'], 'failedNames': ['a'],
                       'error': 'Could not save photo history'},
        })
        self.assertFalse(result['failedFlush'])
        self.assertTrue(result['stillPending'])
        self.assertTrue(result['retry'])
        self.assertEqual(result['persisted'], ['b1', 'a1', 'a2', 'a3'])
        self.assertEqual(result['sent'], ['a1', 'b1', 'a1', 'a1', 'a2', 'a3'])
        self.assertEqual(result['notices'], ['Could not save photo history'] * 2)
        self.assertFalse(result['finished'])
        self.assertEqual(result['status'], {'pendingNames': [], 'failedNames': [], 'error': None})

    def test_flush_reports_a_failure_that_occurs_during_the_flush(self):
        result = self.run_js("""
const first = deferred();
post = async (path, body) => { posts.push({path, body}); return first.promise; };
h.record('a', 'Light', {value: 1});
h.record('a', 'Light', {value: 2});
const flushing = h.flush();
first.resolve({ok: false, error: 'disk full'});
const saved = await flushing;
console.log(JSON.stringify({saved, pending: h.hasPending,
  sent: posts.map(p => p.body.state.value), notices}));
""")
        self.assertEqual(result, {
            'saved': False, 'pending': True, 'sent': [1],
            'notices': ['Could not save photo history'],
        })

    def test_failed_clear_retries_before_subsequent_edits(self):
        result = self.run_js("""
let online = false;
post = async (path, body) => {
  posts.push({path, body});
  if (path.endsWith('/clear') && !online) return {error: 'disk full'};
  return {ok: true};
};
await h.refresh('a');
h.record('a', 'Light', {value: 1});
const clearing = nodes.historyClear.handlers.click();
h.record('a', 'Light', {value: 2});
await clearing; await settle();
const before = {actions: posts.map(p => p.body.state?.value ?? 'clear'),
  pending: h.hasPending, status: statuses.at(-1)};
online = true;
const saved = await h.retry();
console.log(JSON.stringify({before, saved, pending: h.hasPending,
  actions: posts.map(p => p.body.state?.value ?? 'clear'), notices}));
""")
        self.assertEqual(result['before'], {
            'actions': [1, 'clear'], 'pending': True,
            'status': {'pendingNames': ['a'], 'failedNames': ['a'],
                       'error': 'Could not clear photo history'},
        })
        self.assertTrue(result['saved'])
        self.assertFalse(result['pending'])
        self.assertEqual(result['actions'], [1, 'clear', 'clear', 2])
        self.assertEqual(result['notices'], ['Could not clear photo history'])

    def test_scoped_retry_keeps_other_photos_pending(self):
        result = self.run_js("""
let online = false;
post = async (path, body) => {
  posts.push({path, body});
  if (!online) return {error: 'offline'};
  return {ok: true};
};
h.record('a', 'Light', {value: 1}); h.record('b', 'Light', {value: 1});
await settle(); online = true;
const savedA = await h.retry('a');
const middle = {pending: h.hasPending, status: statuses.at(-1)};
const savedB = await h.flush('b');
console.log(JSON.stringify({savedA, middle, savedB, pending: h.hasPending,
  names: posts.map(p => p.body.name)}));
""")
        self.assertTrue(result['savedA'])
        self.assertEqual(result['middle'], {
            'pending': True, 'status': {'pendingNames': ['b'], 'failedNames': ['b'],
                                     'error': 'Could not save photo history'},
        })
        self.assertTrue(result['savedB'])
        self.assertFalse(result['pending'])
        self.assertEqual(result['names'], ['a', 'b', 'a', 'b'])

    def test_late_history_results_do_not_replace_selected_photo(self):
        result = self.run_js("""
const a = deferred(), b = deferred();
nodes.historyPane.classList.contains = () => true;
get = path => path.includes('name=a') ? a.promise : b.promise;
const loadA = h.refresh('a'), loadB = h.refresh('b');
b.resolve({steps: [{id: 2, label: 'B edits'}]}); await loadB;
a.resolve({steps: [{id: 1, label: 'A edits'}]}); await loadA;
console.log(JSON.stringify({html: nodes.historyList.innerHTML}));
""")
        self.assertIn('B edits', result['html'])
        self.assertNotIn('A edits', result['html'])

    def test_successful_record_invalidates_loaded_history(self):
        result = self.run_js("""
nodes.historyPane.classList.contains = () => true;
let count = 0;
get = async () => ({steps: [{id: ++count, label: 'Load ' + count}]});
await h.refresh('a');
h.record('a', 'Light', {exposure: 1}); await h.flush(); await settle();
console.log(JSON.stringify({count, html: nodes.historyList.innerHTML}));
""")
        self.assertEqual(result['count'], 2)
        self.assertIn('Load 2', result['html'])

    def test_clear_runs_after_pending_edits_and_before_new_edits(self):
        result = self.run_js("""
const first = deferred();
post = async (path, body) => {
  posts.push({path, body});
  if (body.state?.value === 1) await first.promise;
  return {ok: true};
};
await h.refresh('a');
h.record('a', 'Light', {value: 1}); h.record('a', 'Light', {value: 2});
const clearing = nodes.historyClear.handlers.click();
h.record('a', 'Light', {value: 3});
await h.refresh('b'); first.resolve(); await clearing; await h.flush();
console.log(JSON.stringify(posts.map(p => ({name: p.body.name,
  action: p.body.state?.value ?? 'clear'}))));
""")
        self.assertEqual(result, [
            {'name': 'a', 'action': 1}, {'name': 'a', 'action': 2},
            {'name': 'a', 'action': 'clear'}, {'name': 'a', 'action': 3},
        ])

    def test_late_restore_does_not_apply_to_a_different_selection(self):
        result = self.run_js("""
const state = deferred();
get = () => state.promise;
await h.refresh('a');
const restoring = nodes.historyList.handlers.click({target: {
  closest: () => ({dataset: {id: 1}}),
}});
await h.refresh('b'); await h.refresh('a');
state.resolve({exposure: 5}); await restoring;
console.log(JSON.stringify({restored}));
""")
        self.assertEqual(result['restored'], [])

    def test_folder_mode_does_not_start_history_writes(self):
        result = self.run_js("""
enabled = false;
h.record('a', 'Light', {exposure: 1}); await h.flush();
console.log(JSON.stringify({posts, timers: timers.size}));
""")
        self.assertEqual(result, {'posts': [], 'timers': 0})

    def test_pending_status_includes_queued_writes_but_not_an_empty_window(self):
        result = self.run_js("""
const first = deferred();
post = async (path, body) => {
  posts.push({path, body});
  if (body.state.value === 1) await first.promise;
  return {ok: true};
};
h.record('a', 'Light', {value: 1});
const queued = h.hasPending;
first.resolve(); await settle();
const emptyWindow = h.hasPending;
h.record('a', 'Light', {value: 2});
const coalesced = h.hasPending;
await h.flush();
console.log(JSON.stringify({queued,emptyWindow,coalesced,finished:h.hasPending}));
""")
        self.assertEqual(result, {'queued': True, 'emptyWindow': False, 'coalesced': True, 'finished': False})


if __name__ == '__main__':
    unittest.main()
