// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
const moduleFor = async file => import(new URL(`../web/${file}`, import.meta.url));
const {createEditRecovery, recoveryPayloadMatches} = await moduleFor('edit-recovery.js');
const {createEditSaveQueue} = await moduleFor('edit-save-queue.js');
const settle = () => new Promise(resolve => setImmediate(resolve));
function memoryStorage() {
  const records = new Map();
  return {records, get length() { return records.size; }, key: i => [...records.keys()][i],
    getItem: key => records.get(key) ?? null, setItem: (key, value) => records.set(key, value),
    removeItem: key => records.delete(key)};
}
const payload = (value = 1) => ({state: {name: 'a.RAW', grade: {exposure: value}, crop: {x: .1},
  masks: [{id: 'mask', data: [1, 2]}], params: {profile_enabled: false}, heals: []},
  history: {label: 'Exposure', state: {grade: {exposure: value}}}});

test('a fresh recovery instance restores the complete draft and isolates catalogs', async () => {
  const storage = memoryStorage();
  const first = createEditRecovery({scope: '/catalog/one', storage});
  const state = payload(); await first.put('a.RAW', 'revision1', state);
  state.state.grade.exposure = 9;
  const reopened = createEditRecovery({scope: '/catalog/one', storage});
  assert.deepEqual((await reopened.list())[0].payload, payload());
  assert.deepEqual(await createEditRecovery({scope: '/catalog/two', storage}).list(), []);
});

test('late acknowledgements cannot delete newer changes and failed saves survive restart', {timeout: 5000}, async () => {
  const storage = memoryStorage();
  const journal = createEditRecovery({scope: '/catalog', storage});
  let complete; let calls = 0;
  let started;
  const firstSend = new Promise(resolve => { started = resolve; });
  const queue = createEditSaveQueue({journal, send: () => ++calls === 1
    ? new Promise(resolve => { complete = resolve; started(); }) : Promise.reject(new Error('server stopped'))});
  queue.enqueue('a.RAW', payload(1), {immediate: true});
  // WebCrypto may finish after many event-loop turns on a busy CI runner.
  // Wait for the observable send instead of assuming a fixed number of turns.
  await firstSend;
  queue.enqueue('a.RAW', payload(2)); await settle();
  complete();
  await assert.rejects(queue.flush(), /Could not save/);
  const reopened = createEditRecovery({scope: '/catalog', storage});
  assert.equal((await reopened.list())[0].payload.state.grade.exposure, 2);
  assert.equal(queue.getStatus().state, 'error');
});

test('journal commits before a send and retry clears only its acknowledged revision', async () => {
  const storage = memoryStorage(), events = [];
  const journal = createEditRecovery({scope: '/catalog', storage});
  let fail = true;
  const queue = createEditSaveQueue({journal, send: async () => {
    events.push((await journal.list())[0].payload.state.grade.exposure);
    if (fail) throw new Error('disk full');
  }});
  queue.enqueue('a.RAW', payload(3));
  await assert.rejects(queue.flush());
  assert.equal((await journal.list()).length, 1);
  fail = false; await queue.retry();
  assert.deepEqual(events, [3, 3]); assert.deepEqual(await journal.list(), []);
});

test('recovery storage failure remains visible while the primary catalog can save', async () => {
  const storage = memoryStorage(); storage.setItem = () => { throw new Error('Quota exceeded'); };
  const journal = createEditRecovery({scope: '/catalog', storage});
  let saved = false;
  const queue = createEditSaveQueue({journal, send: async () => { saved = true; }});
  queue.enqueue('a.RAW', payload());
  for (let i = 0; i < 50 && !queue.getStatus().recoveryError; i++)
    await new Promise(resolve => setTimeout(resolve, 2));
  assert.match(queue.getStatus().recoveryError.message, /Quota/);
  await queue.flush(); assert.equal(saved, true); assert.equal(queue.getStatus().state, 'saved');
});

test('cleanup failure keeps the durable record, and explicit retry clears it', async () => {
  const storage = memoryStorage();
  const journal = createEditRecovery({scope: '/catalog', storage});
  const remove = journal.remove; let fail = true;
  journal.remove = (...args) => fail ? Promise.reject(new Error('read only')) : remove(...args);
  const queue = createEditSaveQueue({journal, send: async () => {}});
  queue.enqueue('a.RAW', payload()); await queue.flush();
  assert.equal((await journal.list()).length, 1);
  assert.match(queue.getStatus().recoveryError.message, /read only/);
  fail = false; await queue.retry(); assert.equal((await journal.list()).length, 0);
});

test('corrupt browser journals stay intact without hiding other valid drafts', async () => {
  const storage = memoryStorage();
  const journal = createEditRecovery({scope: '/catalog', storage});
  await journal.put('a.RAW', 'one', payload());
  const key = storage.key(0); storage.setItem(key, '{broken');
  await journal.put('b.RAW', 'two', {state: {name: 'b.RAW', grade: {exposure: 2}}});
  let warning;
  const reopened = createEditRecovery({scope: '/catalog', storage, onWarning: value => {warning = value;}});
  const recovered = await reopened.list();
  assert.equal(recovered.length, 1); assert.equal(recovered[0].name, 'b.RAW');
  assert.match(warning.message, /1 damaged/);
  await assert.rejects(journal.put('a.RAW', 'three', payload()), SyntaxError);
  assert.equal(storage.getItem(key), '{broken');
});

test('saved-state matching checks masks, crop and film changes rather than only tone', () => {
  const value = payload(); const saved = structuredClone(value.state); saved.rating = 5;
  assert.equal(recoveryPayloadMatches(value, saved), true);
  saved.masks[0].data[0] = 9;
  assert.equal(recoveryPayloadMatches(value, saved), false);
});
