import assert from 'node:assert/strict';
import test from 'node:test';
import {createEditSaveQueue} from '../web/edit-save-queue.js';
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

function harness(options = {}) {
  let clock = 0, nextId = 0;
  const timers = new Map(), requests = [], statuses = [];
  const queue = createEditSaveQueue({
    ...options,
    send(name, payload) {
      const result = deferred();
      requests.push({name, payload, ...result});
      return result.promise;
    },
    onStatus: status => statuses.push(status),
    setTimeout(fn, delay) {
      const id = ++nextId;
      timers.set(id, {at: clock + delay, fn});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  });
  async function advance(ms) {
    clock += ms;
    // Snapshot a bounded set of callbacks due at this time.
    for (const [id, timer] of [...timers]) if (timer.at <= clock) {
      timers.delete(id);
      timer.fn();
    }
    await settle();
  }
  return {queue, requests, statuses, timers, advance};
}

test('navigation cannot associate one photo with another photo\'s mutable edits', async () => {
  const {queue, requests, advance} = harness();
  const editor = {grade: {exposure: 1}, masks: [{opacity: .3}], crop: {x: .1}};
  queue.enqueue('A.raw', editor);
  editor.grade.exposure = 2;
  editor.masks[0].opacity = .8;
  editor.crop.x = .4;
  queue.enqueue('B.raw', editor);
  await advance(400);
  assert.deepEqual(requests.map(({name, payload}) => ({name, payload})), [
    {name: 'A.raw', payload: {grade: {exposure: 1}, masks: [{opacity: .3}], crop: {x: .1}}},
    {name: 'B.raw', payload: {grade: {exposure: 2}, masks: [{opacity: .8}], crop: {x: .4}}},
  ]);
  assert.ok(Object.isFrozen(requests[0].payload.grade));
  requests.forEach(request => request.resolve());
  await queue.flush();
  assert.equal(queue.getStatus().state, 'saved');
});

test('debounce coalesces each photo independently and keeps the latest snapshot', async () => {
  const {queue, requests, advance, timers} = harness();
  queue.enqueue('A', {exposure: 1});
  queue.enqueue('B', {exposure: 2});
  await advance(200);
  queue.enqueue('A', {exposure: 3});
  await advance(200);
  assert.deepEqual(requests.map(request => request.name), ['B']);
  await advance(200);
  assert.deepEqual(requests.map(request => [request.name, request.payload.exposure]), [['B', 2], ['A', 3]]);
  const retained = queue.getPending('A');
  retained.exposure = 100;
  assert.equal(queue.getPending('A').exposure, 3);
  requests.forEach(request => request.resolve());
  await queue.flush();
  assert.equal(timers.size, 0);
  assert.equal(queue.getPending('A'), null);
});

test('same-photo writes serialize while a different photo can save independently', async () => {
  const {queue, requests, advance} = harness();
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  await settle();
  queue.enqueue('A', {exposure: 2});
  queue.enqueue('B', {exposure: 3});
  await advance(400);
  assert.deepEqual(requests.map(request => request.name), ['A', 'B']);
  requests[1].resolve();
  await settle();
  assert.equal(requests.length, 2, 'A2 must not overtake A1');
  const done = queue.flush();
  requests[0].resolve();
  await settle();
  assert.equal(requests[2].name, 'A');
  assert.equal(requests[2].payload.exposure, 2);
  requests[2].resolve();
  await done;
  assert.equal(queue.getStatus().state, 'saved');
});

test('failures retain the newest edit and explicitly retry it without a background retry loop', async () => {
  const {queue, requests, advance, timers, statuses} = harness();
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  await settle();
  queue.enqueue('A', {exposure: 2});
  requests[0].reject(new Error('Disk full'));
  await settle();
  assert.equal(queue.getStatus().state, 'error');
  assert.equal(queue.getStatus().errors[0].error.message, 'Disk full');
  assert.deepEqual(queue.getStatus().pendingNames, ['A']);
  assert.deepEqual(queue.getPending('A'), {exposure: 2});
  queue.enqueue('A', {exposure: 3});
  await advance(10000);
  assert.equal(timers.size, 0);
  assert.equal(requests.length, 1);
  await assert.rejects(queue.flush(), error => {
    assert.deepEqual(error.failedNames, ['A']);
    assert.equal(error.errors[0].message, 'Disk full');
    return true;
  });
  const retry = queue.retry('A');
  await settle();
  assert.equal(requests[1].payload.exposure, 3);
  requests[1].resolve();
  await retry;
  assert.equal(queue.getStatus().state, 'saved');
  assert.ok(statuses.some(status => status.state === 'error'));
});

test('flush bypasses debounce and waits for every selected photo even when one fails', async () => {
  const {queue, requests, timers} = harness();
  queue.enqueue('A', {exposure: 1});
  queue.enqueue('B', {exposure: 2});
  let finished = false;
  const flush = queue.flush();
  const checked = assert.rejects(flush, /Could not save edits for A/).then(() => { finished = true; });
  await settle();
  assert.equal(timers.size, 0);
  requests[0].reject(new Error('Offline'));
  await settle();
  assert.equal(finished, false, 'B is still being saved');
  requests[1].resolve();
  await checked;
  assert.deepEqual(queue.getStatus().pendingNames, ['A']);
});

test('a flush barrier does not wait for edits created after that call', async () => {
  const {queue, requests} = harness();
  queue.enqueue('A', {exposure: 1});
  const flush = queue.flush('A');
  await settle();
  queue.enqueue('A', {exposure: 2}, {immediate: true});
  requests[0].resolve();
  await flush;
  await settle();
  assert.equal(requests.length, 2);
  assert.deepEqual(queue.getPending('A'), {exposure: 2});
  requests[1].resolve();
  await queue.flush();
});

test('flush behind an active write cannot be delayed by continuing slider input', async () => {
  const {queue, requests, timers} = harness();
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  await settle();
  queue.enqueue('A', {exposure: 2});
  const flush = queue.flush('A');
  queue.enqueue('A', {exposure: 3});
  assert.equal(timers.size, 1);
  requests[0].resolve();
  await settle();
  assert.equal(requests.length, 2, 'the captured flush must bypass the new debounce');
  assert.equal(requests[1].payload.exposure, 3);
  assert.equal(timers.size, 0);
  requests[1].resolve();
  await flush;
});

test('flushing one photo leaves another photo\'s debounce intact', async () => {
  const {queue, requests, advance, timers} = harness();
  queue.enqueue('A', {exposure: 1});
  queue.enqueue('B', {exposure: 2});
  const flush = queue.flush('A');
  await settle();
  assert.deepEqual(requests.map(request => request.name), ['A']);
  assert.equal(timers.size, 1);
  requests[0].resolve();
  await flush;
  await advance(400);
  assert.deepEqual(requests.map(request => request.name), ['A', 'B']);
  requests[1].resolve();
  await queue.flush();
});

test('synchronous send failures are retained and unrelated photos still save', async () => {
  let fail = true;
  const writes = [];
  const queue = createEditSaveQueue({send(name, payload) {
    if (name === 'A' && fail) throw new Error('Transport unavailable');
    writes.push({name, payload});
  }});
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  queue.enqueue('B', {exposure: 2}, {immediate: true});
  await settle();
  assert.deepEqual(writes.map(write => write.name), ['B']);
  assert.deepEqual(queue.getPending('A'), {exposure: 1});
  fail = false;
  await queue.retry();
  assert.deepEqual(writes.map(write => write.name), ['B', 'A']);
  assert.equal(queue.getStatus().state, 'saved');
});


test('bulk writes use at most four shared transport slots and every photo makes progress', async () => {
  const {queue, requests} = harness();
  for (let i = 0; i < 17; i++) queue.enqueue(`photo-${i}`, {exposure: i}, {immediate: true});
  const done = queue.flush();
  await settle();
  assert.equal(requests.length, 4);
  let peak = queue.getStatus().savingNames.length;
  for (let i = 0; i < 17; i++) {
    assert.ok(requests[i], `photo ${i} must acquire a released slot`);
    requests[i].resolve();
    await settle();
    peak = Math.max(peak, queue.getStatus().savingNames.length);
    assert.ok(queue.getStatus().savingNames.length <= 4);
  }
  await done;
  assert.equal(peak, 4);
  assert.deepEqual(requests.map(request => request.name), Array.from({length: 17}, (_, i) => `photo-${i}`));
  assert.equal(queue.getStatus().state, 'saved');
});

test('a failed transport frees its slot and retry joins the pool without overtaking waiting photos', async () => {
  const {queue, requests} = harness({maxConcurrent: 2});
  for (const name of ['A', 'B', 'C', 'D']) queue.enqueue(name, {exposure: 1}, {immediate: true});
  const failed = assert.rejects(queue.flush(), /Could not save edits for A/);
  await settle();
  requests[0].reject(new Error('Offline'));
  await settle();
  assert.deepEqual(requests.map(request => request.name), ['A', 'B', 'C']);
  queue.enqueue('A', {exposure: 2});
  const retry = queue.retry('A');
  await settle();
  assert.equal(requests.length, 3, 'retry must respect the occupied transport slots');
  requests[1].resolve();
  await settle();
  assert.equal(requests[3].name, 'D', 'previously waiting photo is not starved by retry');
  requests[2].resolve();
  await settle();
  assert.equal(requests[4].name, 'A');
  assert.equal(requests[4].payload.exposure, 2);
  assert.equal(queue.getStatus().savingNames.length, 2);
  requests[3].resolve();
  requests[4].resolve();
  await Promise.all([failed, retry]);
  assert.equal(queue.getStatus().state, 'saved');
});

test('new debounced input removes an unflushed photo from the waiting transport pool', async () => {
  const {queue, requests, advance} = harness({maxConcurrent: 1});
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  queue.enqueue('B', {exposure: 2}, {immediate: true});
  queue.enqueue('B', {exposure: 3});
  await settle();
  requests[0].resolve();
  await settle();
  assert.equal(requests.length, 1, 'B retains its new debounce after A releases a slot');
  await advance(400);
  assert.equal(requests[1].payload.exposure, 3);
  requests[1].resolve();
  await queue.flush();
});

test('later input cannot delay a flush already waiting for a transport slot', async () => {
  const {queue, requests, timers} = harness({maxConcurrent: 1});
  queue.enqueue('A', {exposure: 1}, {immediate: true});
  queue.enqueue('B', {exposure: 2});
  const flush = queue.flush('B');
  queue.enqueue('C', {exposure: 4}, {immediate: true});
  queue.enqueue('B', {exposure: 3});
  await settle();
  assert.equal(timers.size, 0);
  requests[0].resolve();
  await settle();
  assert.equal(requests[1].name, 'B');
  assert.equal(requests[1].payload.exposure, 3);
  requests[1].resolve();
  await flush;
  await settle();
  assert.equal(requests[2].name, 'C');
  requests[2].resolve();
  await queue.flush();
});

test('transport concurrency must be a positive integer', () => {
  for (const maxConcurrent of [0, -1, 1.5, Infinity, NaN]) {
    assert.throws(() => createEditSaveQueue({send() {}, maxConcurrent}), RangeError);
  }
});
