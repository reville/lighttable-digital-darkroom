// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const handlers = source.slice(source.indexOf('  let wheelFrame = 0;'),
  source.indexOf('  let gStart = 1;'));
const zoom = ['zoomAt', 'zoomReset'].map(name =>
  source.match(new RegExp(`function ${name}\\(.*?\\n\\}`, 's'))[0]).join('\n');

function scene({sourceWidth = 6000} = {}) {
  const S = {viewMode: 'detail', zoom: 1, zoomMode: 'fit', panX: 0, panY: 0};
  const listeners = {}, frames = [], paints = [];
  const image = {name: 'photo'};
  let target = 'photo', modal = false, current = image, speedChanges = 0;
  const rect = () => ({left: 500 + S.panX - 400 * S.zoom,
    top: 400 + S.panY - 300 * S.zoom, width: 800 * S.zoom, height: 600 * S.zoom});
  const cv = {width: 800, height: 600, getBoundingClientRect: rect};
  const wrap = {contains: hit => hit === 'photo',
    addEventListener: (type, fn) => {listeners[type] = fn;}};
  const document = {querySelector: () => modal, elementFromPoint: () => target};
  const noop = () => {};
  new Function('S', '$', 'displaySourcePixelWidth', 'clamp', 'zoomMotion', 'zoomView',
    'presentZoomChange', 'rememberPhotoPan', 'wrap', 'window', 'document', 'cur',
    'requestAnimationFrame', 'stopZoomMotion', 'applyView', 'adjustSpeed', `${zoom}\n${handlers}`)(
      S, () => cv, () => sourceWidth, (v, a, b) => Math.min(b, Math.max(a, v)),
      {cancel: noop}, () => ({zoom: S.zoom, panX: S.panX, panY: S.panY}),
      () => paints.push(S.zoom), noop, wrap, {addEventListener: wrap.addEventListener},
      document, () => current, fn => {frames.push(fn); return frames.length;},
      noop, noop, () => {speedChanges++;});
  return {S, paints, frames, rect,
    pinch(factor, x = 650, y = 450) {listeners['lighttable-magnify']({detail: {factor, x, y}});},
    wheel(event) {listeners.wheel({preventDefault: noop, clientX: 650, clientY: 450, ...event});},
    flush() {for (const fn of frames.splice(0)) fn();},
    target(value) {target = value;}, modal(value) {modal = value;},
    current(value) {current = value;}, speedChanges: () => speedChanges,
  };
}

test('native pinch coalesces incremental magnification and preserves the photo under the pointer', () => {
  const view = scene();
  const before = view.rect();
  const anchor = [(650 - before.left) / before.width, (450 - before.top) / before.height];
  view.pinch(1.1); view.pinch(1.2); view.pinch(1.25);
  assert.equal(view.frames.length, 1);
  assert.equal(view.S.zoom, 1);
  view.flush();
  assert.ok(Math.abs(view.S.zoom - 1.65) < 1e-10);
  assert.equal(view.paints.length, 1);
  const after = view.rect();
  assert.ok(Math.abs(after.left + after.width * anchor[0] - 650) < 1e-10);
  assert.ok(Math.abs(after.top + after.height * anchor[1] - 450) < 1e-10);
  view.pinch(1 / 1.65); view.flush();
  assert.equal(view.S.zoomMode, 'fit');
  assert.deepEqual([view.S.zoom, view.S.panX, view.S.panY], [1, 0, 0]);
});

test('native pinch uses the existing zoom limits, including actual pixels below Fit', () => {
  const view = scene();
  view.pinch(100); view.flush();
  assert.equal(view.S.zoom, 32);
  view.pinch(0.001); view.flush();
  assert.equal(view.S.zoom, 1);
  const small = scene({sourceWidth: 200});
  small.pinch(0.25); small.flush();
  assert.equal(small.S.zoom, 0.25);
  assert.equal(small.S.zoomMode, '100');
  small.pinch(0.5); small.flush();
  assert.equal(small.S.zoom, 0.25);
});

test('pinch cannot zoom through dialogs, sidebars, a grid, or an in-progress edit', () => {
  const view = scene();
  view.target('sidebar'); view.pinch(2);
  view.target('photo'); view.modal(true); view.pinch(2);
  view.modal(false); view.S.viewMode = 'square'; view.pinch(2);
  view.S.viewMode = 'detail'; view.current(null); view.pinch(2);
  view.current({name: 'photo'}); view.S.editGesture = {}; view.pinch(2);
  view.S.editGesture = null; view.S.cropTransition = {}; view.pinch(2);
  assert.equal(view.frames.length, 0);
  assert.equal(view.S.zoom, 1);
});

test('invalid or empty native magnification is ignored', () => {
  const view = scene();
  for (const factor of [NaN, Infinity, -1, 0, 1, undefined]) view.pinch(factor);
  view.pinch(2, NaN, 450); view.pinch(2, 650, Infinity);
  assert.equal(view.frames.length, 0);
});

test('native pinch stays a zoom gesture when a Speed Key is held', () => {
  const view = scene();
  view.S.speed = {key: 'e'};
  view.pinch(2); view.flush();
  assert.equal(view.S.zoom, 2);
  assert.equal(view.speedChanges(), 0);
  view.wheel({deltaY: 1});
  assert.equal(view.speedChanges(), 1);
});

test('browser trackpad wheel zoom and ordinary two-finger panning still work', () => {
  const view = scene();
  view.wheel({ctrlKey: true, deltaY: -Math.log(2) * 100}); view.flush();
  assert.equal(view.S.zoom, 2);
  const pan = [view.S.panX, view.S.panY];
  view.wheel({deltaX: 15, deltaY: -10}); view.flush();
  assert.deepEqual([view.S.panX, view.S.panY], [pan[0] - 15, pan[1] + 10]);
});
