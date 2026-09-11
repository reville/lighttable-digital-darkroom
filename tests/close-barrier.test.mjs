// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const source = readFileSync(new URL('../web/close-barrier.js', import.meta.url));
const {createCloseBarrier} = await import(`data:text/javascript;base64,${source.toString('base64')}`);
const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
const settle = () => new Promise(r => setImmediate(r));
test('close freezes input before capture and keeps it frozen through the native reply', async () => {
  const events = [], pending = deferred();
  const close = createCloseBarrier({capture: () => events.push('capture'),
    flush: () => pending.promise, setBlocked: value => events.push(value)});
  const done = close.prepare(); await settle();
  assert.deepEqual(events, [true, 'capture']);
  pending.resolve(true); assert.equal(await done, true);
  assert.deepEqual(events, [true, 'capture']);
  close.cancel(); assert.equal(events.at(-1), false);
});
test('failed saves unfreeze input, and a cancelled old attempt cannot approve a new one', async () => {
  const first = deferred(), second = deferred(), events = []; let attempts = 0;
  const close = createCloseBarrier({capture: () => {},
    flush: () => ++attempts === 1 ? first.promise : second.promise,
    setBlocked: value => events.push(value)});
  const old = close.prepare(); await settle(); close.cancel();
  const current = close.prepare(); await settle(); first.resolve(true);
  assert.equal(await old, false); assert.equal(events.at(-1), true);
  second.resolve(false); assert.equal(await current, false); assert.equal(events.at(-1), false);
});
