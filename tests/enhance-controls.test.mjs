// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { createEnhancePanel } from '../web/enhance-panel.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture({ capabilities, run } = {}) {
  const handlers = {};
  const nodes = {
    enhanceRun: {}, enhanceNote: {},
    enhanceMode: { value: 'denoise', options: [{ value: 'denoise' }, { value: 'upscale' }],
      addEventListener: (event, handler) => { handlers[event] = handler; } },
  };
  let photo = { name: 'first.jpg' };
  const calls = [], completed = [];
  const controller = createEnhancePanel({
    el: id => nodes[id], getPhoto: () => photo,
    getCapabilities: capabilities || (async () => ({ available: true, modes: { denoise: true, upscale: false } })),
    run: async request => { calls.push(request); return run ? run(request) : { ok: true }; },
    onComplete: result => { completed.push(result); },
  });
  return { controller, nodes, handlers, calls, completed, setPhoto: value => { photo = value; controller.sync(); } };
}

test('pending capabilities and a failed capability check keep Enhance unavailable', async () => {
  const pending = deferred();
  const f = fixture({ capabilities: () => pending.promise });
  const checking = f.controller.refresh();
  assert.equal(f.nodes.enhanceRun.disabled, true);
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.calls.length, 0);
  pending.reject(new Error('offline'));
  await checking;
  assert.equal(f.nodes.enhanceRun.disabled, true);
  assert.match(f.nodes.enhanceNote.textContent, /Could not check/);
});

test('mode availability disables Super resolution and rejects an unsupported synthetic click', async () => {
  const f = fixture();
  await f.controller.refresh();
  assert.equal(f.nodes.enhanceMode.options[0].disabled, false);
  assert.equal(f.nodes.enhanceMode.options[1].disabled, true);
  assert.equal(f.nodes.enhanceRun.disabled, false);
  f.nodes.enhanceMode.value = 'upscale';
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.calls.length, 0);
  assert.match(f.nodes.enhanceNote.textContent, /Super resolution needs an installed model/);
});

test('missing photo disables Enhance and a loaded photo enables it again', async () => {
  const f = fixture();
  await f.controller.refresh();
  f.setPhoto(null);
  assert.equal(f.nodes.enhanceRun.disabled, true);
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.calls.length, 0);
  f.setPhoto({ name: 'second.jpg' });
  assert.equal(f.nodes.enhanceRun.disabled, false);
  await f.nodes.enhanceRun.onclick();
  assert.deepEqual(f.calls, [{ name: 'second.jpg', mode: 'denoise' }]);
});

test('an active Enhance request cannot be submitted twice', async () => {
  const pending = deferred();
  const f = fixture({ run: () => pending.promise });
  await f.controller.refresh();
  const first = f.nodes.enhanceRun.onclick();
  assert.equal(f.nodes.enhanceRun.disabled, true);
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.calls.length, 1);
  pending.resolve({ ok: true });
  await first;
  assert.equal(f.completed.length, 1);
  assert.equal(f.nodes.enhanceRun.disabled, false);
});

test('failed requests show their error and allow a retry', async () => {
  const f = fixture({ run: async () => { throw new Error('Connection lost'); } });
  await f.controller.refresh();
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.nodes.enhanceRun.disabled, false);
  assert.equal(f.nodes.enhanceNote.textContent, 'Connection lost');
  assert.equal(f.completed.length, 0);
});

test('refresh picks a supported mode when the prior mode is unavailable', async () => {
  const f = fixture({ capabilities: async () => ({ available: true, modes: { denoise: false, upscale: true } }) });
  await f.controller.refresh();
  assert.equal(f.nodes.enhanceMode.value, 'upscale');
  await f.nodes.enhanceRun.onclick();
  assert.equal(f.calls[0].mode, 'upscale');
});

test('an older capability response cannot override the latest refresh', async () => {
  const old = deferred(), current = deferred();
  let count = 0;
  const f = fixture({ capabilities: () => (++count === 1 ? old : current).promise });
  const before = f.controller.refresh(), after = f.controller.refresh();
  current.resolve({ available: true, modes: { denoise: true, upscale: false } });
  await after;
  old.resolve({ available: false, reason: 'stale' });
  await before;
  assert.equal(f.nodes.enhanceRun.disabled, false);
});
