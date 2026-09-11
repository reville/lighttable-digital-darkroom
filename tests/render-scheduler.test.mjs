// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {createFrameScheduler} from '../web/render-scheduler.js';

function harness(callback) {
  let id = 0;
  const frames = new Map(), timers = new Map(), output = [];
  const scheduler = createFrameScheduler(work => { output.push(work); callback?.(work); }, {
    requestFrame: fn => { const key = ++id; frames.set(key, () => { frames.delete(key); fn(); }); return key; },
    cancelFrame: id => frames.delete(id),
    setTimer: fn => { timers.set(++id, fn); return id; }, clearTimer: id => timers.delete(id),
  });
  return {scheduler, frames, timers, output};
}

test('suspended frames deliver latest coalesced work once via deadline', () => {
  const h = harness();
  for (let i=0;i<100;i++) h.scheduler.request({grade:true, revision:i});
  const stale = [...h.frames.values()][0];
  assert.equal(h.frames.size,1); assert.equal(h.timers.size,1);
  [...h.timers.values()][0]();
  assert.deepEqual(h.output,[{grade:true,revision:99}]);
  h.scheduler.request({view:true});
  stale();
  assert.equal(h.output.length,1,'late RAF cannot consume newer work');
  [...h.frames.values()][0]();
  assert.deepEqual(h.output[1],{view:true});
  assert.equal(h.timers.size,0);
});

test('cancel and explicit flush invalidate both callbacks', () => {
  const h = harness(); h.scheduler.request({view:true});
  const stale = [...h.timers.values()][0];
  h.scheduler.cancel(); stale();
  assert.equal(h.output.length,0);
  h.scheduler.request({overlay:true}); h.scheduler.flush(); h.scheduler.flush();
  assert.deepEqual(h.output,[{overlay:true}]);
  assert.equal(h.frames.size,0); assert.equal(h.timers.size,0);
});

test('a callback can schedule the following frame without losing it', () => {
  const h = harness(work => { if(work.first) h.scheduler.request({second:true}); });
  h.scheduler.request({first:true});
  [...h.frames.values()][0]();
  assert.equal(h.frames.size,1);
  [...h.timers.values()][0]();
  assert.deepEqual(h.output,[{first:true},{second:true}]);
});
