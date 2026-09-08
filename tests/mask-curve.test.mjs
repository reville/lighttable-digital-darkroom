import test from 'node:test';
import assert from 'node:assert/strict';
import { installMaskCurve, localCurveLUT } from '../web/mask-curve.js';

const table = (fn = x => x) => Array.from({ length: 256 }, (_, index) => fn(index / 255));

test('collinear local curve points preserve identity and other straight lines', () => {
  for (const inputs of [[0, 1], [0, 0.5, 1], [0, 1 / 255, 0.2, 0.8, 254 / 255, 1]]) {
    assert.deepEqual(localCurveLUT(inputs.map(x => [x, x])), table());
    for (const line of [x => 0.2 + x * 0.6, x => 0.8 - x * 0.5, () => 0.35]) {
      const lut = localCurveLUT(inputs.map(x => [x, line(x)]));
      for (let index = 0; index < lut.length; index++) assert.ok(Math.abs(lut[index] - line(index / 255)) < 1e-12);
    }
  }
});

test('local cubic interpolation preserves monotonic segments without overshooting their handles', () => {
  for (const points of [
    [[0, 0], [0.01, 0.4], [0.6, 0.41], [0.99, 0.95], [1, 1]],
    [[0, 1], [0.2, 0.9], [0.8, 0.1], [1, 0]],
    [[0, 0.1], [0.2, 0.8], [0.4, 0.8], [0.6, 0.2], [0.8, 0.2], [1, 0.6]],
  ]) {
    const lut = localCurveLUT(points);
    assert.equal(lut[0], points[0][1]);
    assert.equal(lut.at(-1), points.at(-1)[1]);
    for (let segment = 0; segment < points.length - 1; segment++) {
      const [start, end] = points.slice(segment, segment + 2);
      const samples = lut.filter((_, index) => index / 255 >= start[0] && index / 255 <= end[0]);
      samples.forEach((value, index) => {
        assert.ok(value >= Math.min(start[1], end[1]) - 1e-12 && value <= Math.max(start[1], end[1]) + 1e-12);
        if (index) assert.ok((value - samples[index - 1]) * Math.sign(end[1] - start[1]) >= -1e-12);
      });
    }
  }
});

test('a lifted midpoint keeps a nonzero tangent and meets every sampled handle exactly', () => {
  const points = [[0, 0.1], [128 / 255, 0.7], [1, 0.95]];
  const lut = localCurveLUT(points);
  assert.equal(lut[0], 0.1);
  assert.equal(lut[128], 0.7);
  assert.equal(lut[255], 0.95);
  const slopeBefore = (lut[128] - lut[127]) * 255;
  const slopeAfter = (lut[129] - lut[128]) * 255;
  assert.ok(slopeBefore > 0.5 && slopeAfter > 0.5);
  assert.ok(Math.abs(slopeBefore - slopeAfter) < 0.03);
});

function node() {
  const listeners = new Map();
  return {
    attributes: {}, style: {},
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    setAttribute(name, value) { this.attributes[name] = value; },
    fire(type, detail = {}) {
      const event = { button: 0, isPrimary: true, pointerId: 1, preventDefault() {}, stopPropagation() {}, ...detail };
      for (const fn of listeners.get(type) || []) fn(event);
    },
  };
}

function fixture(initial = { id: 'first', grade: {} }) {
  let mask = initial;
  const canvas = node(), reset = node(), channel = node(), calls = { undo: 0, changed: [], save: [] };
  const context = {
    lines: [], points: [],
    clearRect() { this.lines = []; this.points = []; },
    beginPath() {}, stroke() {},
    moveTo(x, y) { this.lines.push([x, y]); },
    lineTo(x, y) { this.lines.push([x, y]); },
    fillRect(x, y, w, h) { this.points.push([(x + w / 2 - 8) / 240, 1 - (y + h / 2 - 8) / 240]); },
  };
  Object.assign(canvas, {
    width: 256, height: 256, capture: null,
    getContext: () => context,
    getBoundingClientRect: () => ({ left: 10, top: 20, width: 256, height: 256 }),
    focus() {},
    setPointerCapture(id) { this.capture = id; },
    hasPointerCapture(id) { return this.capture === id; },
    releasePointerCapture(id) { if (this.capture === id) this.capture = null; },
  });
  channel.value = 'L';
  const editor = installMaskCurve({ canvas, reset, channel, getMask: () => mask,
    pushUndo: () => { calls.undo++; }, changed: value => { calls.changed.push(value); }, save: value => { calls.save.push(value); } });
  return { editor, canvas, reset, channel, calls, context,
    select(value) { mask = value; editor.sync(); },
    pointer(type, x, y, extra = {}) { canvas.fire(type, { clientX: 18 + x * 240, clientY: 28 + (1 - y) * 240, ...extra }); },
    key(key, extra = {}) { canvas.fire('keydown', { key, ...extra }); },
    up(key) { canvas.fire('keyup', { key }); },
  };
}

test('opening, syncing and selecting a handle preserve an imported LUT exactly', () => {
  const original = table(x => x ** 0.73), mask = { grade: { curveL: original } };
  const f = fixture(mask);
  assert.strictEqual(mask.grade.curveL, original);
  assert.deepEqual(f.context.lines.slice(-256).map(([, y]) => (248 - y) / 240), original.map(value => (248 - (8 + (1 - value) * 240)) / 240));
  f.pointer('pointerdown', 128 / 255, original[128]);
  f.pointer('pointerup', 128 / 255, original[128]);
  f.editor.sync();
  assert.strictEqual(mask.grade.curveL, original);
  assert.equal(f.calls.undo, 0);
  assert.equal(f.calls.changed.length, 0);
  assert.equal(f.calls.save.length, 0);
});

test('the empty curve displays a straight identity and a drag creates one undo/save gesture', () => {
  const mask = { grade: { exposure: 0.4 } }, f = fixture(mask);
  assert.equal(mask.grade.curveL, undefined);
  const drawn = f.context.lines.slice(-256);
  assert.ok(Math.abs(drawn[64][1] - (248 - 64 / 255 * 240)) < 1e-12);
  f.pointer('pointerdown', 0.5, 0.65);
  f.pointer('pointermove', 0.6, 0.7);
  f.pointer('pointermove', 0.6, 0.75);
  f.pointer('pointerup', 0.6, 0.75);
  assert.equal(f.calls.undo, 1);
  assert.deepEqual(f.calls.save, [mask]);
  assert.equal(mask.grade.exposure, 0.4);
  assert.equal(mask.grade.curveL.length, 256);
  assert.ok(Math.abs(mask.grade.curveL[153] - 0.75) < 1e-12);
  assert.equal(f.canvas.capture, null);
});

test('adding an identity midpoint through the editor does not change any output tone', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.pointer('pointerdown', 0.5, 0.5); f.pointer('pointerup', 0.5, 0.5);
  assert.deepEqual(mask.grade.curveL, table());
  assert.equal(f.context.points.length, 3);
});

test('endpoints stay at input zero and one and cannot be removed', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.pointer('pointerdown', 0, 0);
  f.pointer('pointermove', 0.3, 0.2);
  f.pointer('pointerup', 0.3, 0.2);
  assert.equal(f.context.points[0][0], 0);
  assert.ok(Math.abs(f.context.points[0][1] - 0.2) < 1e-12);
  assert.ok(Math.abs(mask.grade.curveL[0] - 0.2) < 1e-12);
  f.pointer('dblclick', 0, 0.2);
  f.key('Delete'); f.up('Delete');
  assert.equal(f.context.points.length, 2);
  f.key('End');
  f.key('ArrowLeft'); f.up('ArrowLeft');
  assert.equal(f.context.points.at(-1)[0], 1);
  assert.equal(f.calls.undo, 1);
});

test('interior points cannot cross neighbors or occupy the same input value', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.pointer('pointerdown', 0.5, 0.7); f.pointer('pointerup', 0.5, 0.7);
  f.pointer('pointerdown', 0.75, 0.8); f.pointer('pointerup', 0.75, 0.8);
  f.pointer('pointerdown', 0.5, 0.7);
  f.pointer('pointermove', 0.99, 0.9);
  f.pointer('pointerup', 0.99, 0.9);
  const points = f.context.points;
  assert.ok(Math.abs(points[1][0] - (0.75 - 1 / 255)) < 1e-12);
  for (let index = 1; index < points.length; index++) assert.ok(points[index][0] - points[index - 1][0] >= 1 / 255 - 1e-12);
  assert.ok(mask.grade.curveL.every(value => Number.isFinite(value) && value >= 0 && value <= 1));
  const count = points.length;
  f.pointer('pointerdown', 0.75, 0.1); f.pointer('pointerup', 0.75, 0.1);
  assert.equal(f.context.points.length, count);
});

test('pointer cancellation restores the exact imported LUT without saving a partial edit', () => {
  const original = table(x => x ** 1.2), mask = { grade: { curveL: original, exposure: 0.5 } };
  const f = fixture(mask);
  f.pointer('pointerdown', 128 / 255, original[128]);
  f.pointer('pointermove', 0.5, 0.8);
  assert.notStrictEqual(mask.grade.curveL, original);
  f.canvas.fire('pointercancel');
  assert.strictEqual(mask.grade.curveL, original);
  assert.equal(mask.grade.exposure, 0.5);
  assert.equal(f.canvas.capture, null);
  assert.equal(f.calls.save.length, 0);
  f.pointer('pointermove', 0.8, 0.1);
  assert.strictEqual(mask.grade.curveL, original);
});

test('double-click removes only the targeted interior point', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.pointer('pointerdown', 0.4, 0.7); f.pointer('pointerup', 0.4, 0.7);
  f.pointer('pointerdown', 0.7, 0.9); f.pointer('pointerup', 0.7, 0.9);
  f.pointer('dblclick', 0.4, 0.7);
  assert.equal(f.context.points.length, 3);
  assert.ok(Math.abs(f.context.points[1][0] - 0.7) < 1e-12);
  assert.equal(f.calls.save.length, 3);
});

test('cancelling a newly added curve also restores an absent grade', () => {
  const mask = {}, f = fixture(mask);
  f.pointer('pointerdown', 0.5, 0.8);
  assert.ok(mask.grade.curveL);
  f.canvas.fire('lostpointercapture');
  assert.equal(Object.hasOwn(mask, 'grade'), false);
  assert.equal(f.context.points.length, 2);
  assert.equal(f.calls.save.length, 0);
});

test('switching masks cancels the old gesture and never changes the newly selected mask', () => {
  const first = { grade: {} }, second = { grade: { exposure: 0.2 } }, f = fixture(first);
  f.pointer('pointerdown', 0.5, 0.8);
  f.select(second);
  f.pointer('pointermove', 0.7, 0.9);
  f.pointer('pointerup', 0.7, 0.9);
  assert.deepEqual(first.grade, {});
  assert.deepEqual(second.grade, { exposure: 0.2 });
  assert.equal(f.calls.save.length, 0);
  assert.equal(f.context.points.length, 2);
});

test('a replaced or externally changed grade during a gesture remains authoritative', () => {
  for (const replace of [false, true]) {
    const mask = { grade: {} }, f = fixture(mask);
    f.pointer('pointerdown', 0.5, 0.8);
    const restored = table(x => x ** 2);
    if (replace) mask.grade = { curveL: restored };
    else mask.grade.curveL = restored;
    f.editor.sync();
    f.pointer('pointerup', 0.5, 0.8);
    assert.strictEqual(mask.grade.curveL, restored);
    assert.equal(f.calls.save.length, 0);
    assert.equal(f.context.points.length, 9);
  }
});

test('sync notices in-place LUT changes and keeps edited handles across channel and mask selection', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.pointer('pointerdown', 0.5, 0.8); f.pointer('pointerup', 0.5, 0.8);
  f.channel.value = 'R'; f.channel.fire('change');
  assert.equal(f.context.points.length, 2);
  f.channel.value = 'L'; f.channel.fire('change');
  assert.equal(f.context.points.length, 3);
  f.select({ grade: {} }); f.select(mask);
  assert.equal(f.context.points.length, 3);
  mask.grade.curveL[128] = 0.9;
  f.editor.sync();
  assert.equal(f.context.points.length, 9);
  assert.deepEqual(f.context.points[4], [128 / 255, 0.9]);
});

test('keyboard edits support add, selection, repeat grouping and interior deletion', () => {
  const mask = { grade: {} }, f = fixture(mask);
  f.key('Enter'); f.up('Enter');
  assert.equal(f.context.points.length, 3);
  assert.match(f.canvas.attributes['aria-valuetext'], /Point 2 of 3/);
  f.key('ArrowUp'); f.key('ArrowUp', { repeat: true }); f.key('ArrowUp', { repeat: true });
  f.up('ArrowUp');
  assert.equal(f.calls.undo, 2);
  assert.equal(f.calls.save.length, 2);
  assert.ok(Math.abs(f.context.points[1][1] - (0.5 + 3 / 255)) < 1e-12);
  f.key(']'); assert.match(f.canvas.attributes['aria-valuetext'], /Point 3 of 3/);
  f.key('['); f.key('Delete'); f.up('Delete');
  assert.equal(f.context.points.length, 2);
  assert.equal(mask.grade.curveL, undefined);
  assert.equal(f.reset.disabled, true);
});

test('Escape cancels a keyboard adjustment and reset affects only the active local channel', () => {
  const curveL = table(x => x ** 0.8), curveR = table(x => x ** 1.4);
  const mask = { grade: { curveL, curveR, exposure: 0.3 } }, f = fixture(mask);
  f.key('ArrowUp'); f.key('Escape'); f.up('ArrowUp');
  assert.strictEqual(mask.grade.curveL, curveL);
  assert.equal(f.calls.save.length, 0);
  f.channel.value = 'R'; f.channel.fire('change');
  f.reset.fire('click');
  assert.deepEqual(mask.grade, { curveL, exposure: 0.3 });
  assert.equal(f.calls.save.length, 1);
  assert.equal(f.reset.disabled, true);
});

test('no selected mask disables interaction and unrelated pointers cannot finish a gesture', () => {
  const f = fixture(null);
  assert.equal(f.canvas.attributes['aria-disabled'], 'true');
  assert.equal(f.canvas.tabIndex, -1);
  assert.equal(f.reset.disabled, true);
  f.pointer('pointerdown', 0.5, 0.8); f.key('Enter'); f.reset.fire('click');
  assert.equal(f.calls.undo, 0);
  const mask = { grade: {} }; f.select(mask);
  f.pointer('pointerdown', 0.5, 0.8);
  f.pointer('pointermove', 0.5, 0.1, { pointerId: 2 });
  f.canvas.fire('pointerup', { pointerId: 2 });
  assert.equal(f.calls.save.length, 0);
  assert.ok(Math.abs(f.context.points[1][1] - 0.8) < 1e-12);
  f.canvas.fire('pointerup');
  assert.equal(f.calls.save.length, 1);
});
