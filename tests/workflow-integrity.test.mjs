import test from 'node:test';
import assert from 'node:assert/strict';
import {createSelectionRequest} from '../web/selection-request.js';
import {mergeMaskDelta} from '../web/batch-masks.js';

test('Select All publishes the full filtered result after later pages arrive', async () => {
  let release;
  const rows = [{name: 'first'}], selected = new Set();
  const request = createSelectionRequest({
    load: () => new Promise(resolve => { release = resolve; }), scope: () => 'folder',
    visible: () => rows.filter(row => !row.hidden), selection: () => selected,
    changed() {}, onError(error) { throw error; },
  });
  const result = request.selectAll();
  assert.equal(request.pending, true);
  assert.equal(selected.size, 0);
  rows.push({name: 'later'}, {name: 'filtered', hidden: true});
  release();
  assert.equal(await result, true);
  assert.deepEqual([...selected], ['first', 'later']);
  assert.equal(request.pending, false);
});

test('new selection or changed folder cancels a pending Select All', async () => {
  for (const action of ['cancel', 'folder']) {
    let release, folder = 'before';
    const selected = new Set();
    const request = createSelectionRequest({load: () => new Promise(r => { release = r; }),
      scope: () => folder, visible: () => [{name: 'old'}], selection: () => selected,
      changed() {}, onError(error) { throw error; }});
    const result = request.selectAll();
    if (action === 'cancel') { request.cancel(); selected.add('manual'); }
    else folder = 'after';
    release();
    assert.equal(await result, false);
    assert.deepEqual([...selected], action === 'cancel' ? ['manual'] : []);
    assert.equal(request.pending, false);
  }
});

test('failed loading cannot turn Select All into a successful subset', async () => {
  const selected = new Set(['old']);
  let error;
  const request = createSelectionRequest({load: async () => { throw Error('Disconnected'); },
    scope: () => '', visible: () => [{name: 'first'}], selection: () => selected,
    changed() {}, onError(value) { error = value; }});
  assert.equal(await request.selectAll(), false);
  assert.equal(selected.size, 0);
  assert.equal(error.message, 'Disconnected');
});

test('batch mask deltas preserve pending manual edits and do not duplicate generated IDs', () => {
  const local = [{id: 'manual', opacity: .4}, {id: 'generated', opacity: .8}];
  const result = mergeMaskDelta(local, {added: [{id: 'generated', opacity: 1}, {id: 'new'}]});
  assert.deepEqual(result, [...local, {id: 'new'}]);
  assert.deepEqual(mergeMaskDelta(result, {removed: ['generated', 'new']}), [local[0]]);
  assert.equal(local.length, 2);
});
