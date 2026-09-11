// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {test} from 'node:test';
import {createEditSaveQueue} from '../web/edit-save-queue.js';

const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function actions(seed, count) {
  let state = seed >>> 0;
  const rand = () => (state = (Math.imul(state, 1664525) + 1013904223) >>> 0);
  return Array.from({length: count}, (_, revision) => ({
    type: rand() % 10, photo: `photo-${rand() % 7}`, revision: revision + 1,
    pick: rand(), fail: rand() % 5 === 0, immediate: rand() % 2 === 0,
  }));
}

async function replay(sequence) {
  const pending = [], timers = new Map(), expected = new Map(), stored = new Map();
  const journal = new Map(), active = new Set();
  let timerId = 0;
  const queue = createEditSaveQueue({
    maxConcurrent: 3,
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    journal: {
      async put(name, token, payload) { journal.set(name, {token, payload}); },
      async remove(name, token) { if (journal.get(name)?.token === token) journal.delete(name); },
    },
    send(name, payload) {
      assert.ok(!active.has(name), 'concurrent writes to the same photo');
      active.add(name);
      assert.ok(active.size <= 3, 'transport budget exceeded');
      return new Promise((resolve, reject) => pending.push({name, payload, resolve, reject}));
    },
  });
  async function complete(pick, fail) {
    if (!pending.length) return;
    const request = pending.splice(pick % pending.length, 1)[0];
    active.delete(request.name);
    // A lost response may follow a successful write. Retrying must remain safe.
    if (!fail || pick % 2 === 0) {
      assert.ok(request.payload.revision >= (stored.get(request.name) || 0), 'an older edit overwrote a newer one');
      stored.set(request.name, request.payload.revision);
    }
    if (fail) request.reject(new Error('injected lost response'));
    else request.resolve();
    await settle();
  }
  function fireTimer(pick) {
    if (!timers.size) return;
    const [id, fn] = [...timers][pick % timers.size];
    timers.delete(id); fn();
  }
  for (const action of sequence) {
    if (action.type < 6) {
      const payload = {revision: action.revision, state: {name: action.photo, grade: {exposure: action.revision / 1000}}};
      expected.set(action.photo, action.revision);
      queue.enqueue(action.photo, payload, {immediate: action.immediate});
      payload.revision = -1; // Caller mutation must never affect a queued snapshot.
    } else if (action.type < 8) await complete(action.pick, action.fail);
    else if (action.type === 8) fireTimer(action.pick);
    else void queue.retry(action.photo).catch(() => {});
    await settle();
  }
  for (let i = 0; i < 1000 && queue.getStatus().state !== 'saved'; i++) {
    void queue.retry().catch(() => {});
    await settle();
    while (timers.size) fireTimer(0);
    await complete(0, false);
  }
  assert.equal(queue.getStatus().state, 'saved', 'queue did not settle within the bound');
  await queue.flush();
  assert.deepEqual([...stored].sort(), [...expected].sort(), 'final edits differ from the independent model');
  assert.equal(journal.size, 0, 'acknowledged recovery drafts were retained');
  assert.equal(active.size, 0);
}

// Keep a reproducible, reduced failure instead of reporting a random seed alone.
async function reduceFailure(sequence) {
  let result = sequence, chunk = Math.ceil(sequence.length / 2), attempts = 0;
  while (chunk && attempts < 100) {
    let reduced = false;
    for (let start = 0; start < result.length && attempts++ < 100; start += chunk) {
      const candidate = result.slice(0, start).concat(result.slice(start + chunk));
      try { await replay(candidate); }
      catch { result = candidate; reduced = true; break; }
    }
    if (!reduced) chunk = Math.floor(chunk / 2);
  }
  return result;
}

test('40 seeded save sequences survive delayed, reordered and lost responses', async () => {
  for (let seed = 1; seed <= 40; seed++) {
    const sequence = actions(seed, 250);
    try { await replay(sequence); }
    catch (error) {
      const reduced = await reduceFailure(sequence);
      if (process.env.LIGHTTABLE_SEQUENCE_FAILURE) {
        writeFileSync(process.env.LIGHTTABLE_SEQUENCE_FAILURE, JSON.stringify({seed, sequence, reduced}, null, 2));
      }
      throw new Error(`seed ${seed}, reduced actions ${JSON.stringify(reduced)}: ${error.message}`, {cause: error});
    }
  }
});
