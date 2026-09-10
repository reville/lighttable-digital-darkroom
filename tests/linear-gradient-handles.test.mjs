import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { editOverlayCursor } from '../web/edit-cursor.js';
import { linearHandleAt, editLinear } from '../web/mask-shape.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const rect = { left: -200, top: -100, width: 1000, height: 600 };
const initial = () => ({ type: 'linear', start: [0.25, 0.5], end: [0.75, 0.5],
  grade: { exposure: 0.7 }, addStrokes: [], subtractStrokes: [], intersectStrokes: [] });
const near = (actual, expected) => actual.forEach((value, i) =>
  assert.ok(Math.abs(value - expected[i]) < 1e-10, `${actual} != ${expected}`));

// Exercise the real overlay listeners, including pointer capture, Undo and save.
function editor() {
  let mask = initial();
  const S = { activePane: 'maskPane', localPinsVisible: true, masks: [mask],
    maskRefineMode: null, brushSize: 0.1, brushFeather: 0.5, brushFlow: 1, brushDensity: 1 };
  const handlers = {}, undo = [], saves = [], samples = [];
  let captured = null;
  const overlay = {
    addEventListener: (type, handler) => { handlers[type] = handler; },
    setPointerCapture: id => { captured = id; },
    releasePointerCapture: () => { captured = null; },
    hasPointerCapture: id => captured === id,
  };
  const deps = { S, $: id => id === 'cv' ? {getBoundingClientRect: () => rect} : overlay,
    cur: () => ({}), selectedMask: () => mask, linearHandleAt, editLinear,
    overlayPoint: (event, r) => [(event.clientX - r.left) / r.width, (event.clientY - r.top) / r.height],
    sampleMaskColorArea: (...args) => samples.push(args),
    pushUndo: () => undo.push(structuredClone(mask)), saveState: () => saves.push(structuredClone(mask)),
    drawGrade: () => {}, drawEditOverlay: () => {}, syncMaskPanel: () => {},
    maskPointCount: () => 0, MAX_TOTAL_MASK_POINTS: 20000, document: {addEventListener() {}}, window: {addEventListener() {}} };
  new Function(...Object.keys(deps), source.slice(
    source.indexOf("$('editOverlay').addEventListener('pointerdown'"),
    source.indexOf('/* ------------------------------------------------------------ film render */')))(...Object.values(deps));
  const fire = (type, point, extra = {}) => handlers[type]({ type, button: 0, pointerId: 1,
    clientX: rect.left + point[0] * rect.width, clientY: rect.top + point[1] * rect.height,
    stopPropagation() {}, preventDefault() {}, ...extra });
  return { S, mask, undo, saves, samples, fire, captured: () => captured };
}

for (const handle of ['start', 'end']) {
  test(`re-grabbing the ${handle} dot moves only that endpoint without jumping`, () => {
    const e = editor(), other = handle === 'start' ? 'end' : 'start';
    const before = structuredClone(e.mask);
    const hit = [before[handle][0] + 0.008, before[handle][1]];
    e.fire('pointerdown', hit);
    assert.equal(e.S.editGesture.handle, handle);
    assert.deepEqual(e.mask, before);
    assert.equal(e.captured(), 1);
    e.fire('pointermove', [hit[0] + 0.03, hit[1] + 0.1]);
    near(e.mask[handle], [before[handle][0] + 0.03, before[handle][1] + 0.1]);
    assert.deepEqual(e.mask[other], before[other]);
    e.fire('pointerup', hit);
    assert.equal(e.captured(), null);
    assert.equal(e.S.editGesture, null);
    assert.deepEqual(e.undo, [before]);
    assert.deepEqual(e.saves, [e.mask]);
    assert.deepEqual(e.mask.grade, before.grade);
  });
}

test('dragging the line moves both endpoints and preserves its shape at photo edges', () => {
  const e = editor();
  e.fire('pointerdown', [0.5, 0.51]);
  assert.equal(e.S.editGesture.handle, 'move');
  e.fire('pointermove', [0.6, 0.61]);
  near(e.mask.start, [0.35, 0.6]); near(e.mask.end, [0.85, 0.6]);
  e.fire('pointermove', [1, 1]);
  near(e.mask.start, [0.5, 0.99]); near(e.mask.end, [1, 0.99]);
  // Moving back uses the original gesture geometry, without accumulated drift.
  e.fire('pointermove', [0.5, 0.51]);
  near(e.mask.start, [0.25, 0.5]); near(e.mask.end, [0.75, 0.5]);
});

test('dragging elsewhere redraws the selected gradient and a later drag edits it', () => {
  const e = editor();
  e.fire('pointerdown', [0.15, 0.2]);
  assert.equal(e.S.editGesture.handle, null);
  e.fire('pointermove', [0.4, 0.7]);
  e.fire('pointerup', [0.4, 0.7]);
  near(e.mask.start, [0.15, 0.2]); near(e.mask.end, [0.4, 0.7]);
  e.fire('pointerdown', [0.15, 0.2]);
  e.fire('pointermove', [0.2, 0.3]);
  e.fire('pointerup', [0.2, 0.3]);
  near(e.mask.start, [0.2, 0.3]); near(e.mask.end, [0.4, 0.7]);
  assert.equal(e.undo.length, 2); assert.equal(e.saves.length, 2);
});

test('painting refinement and Option subtraction keep their gesture priority', () => {
  for (const mode of ['add', 'subtract', 'intersect', 'option']) {
    const e = editor(), before = structuredClone(e.mask);
    e.S.maskRefineMode = mode === 'option' ? null : mode;
    e.fire('pointerdown', before.start, {altKey: mode === 'option'});
    assert.equal(e.S.editGesture.type, 'brush');
    assert.deepEqual(e.mask.start, before.start); assert.deepEqual(e.mask.end, before.end);
    const key = mode === 'option' ? 'subtractStrokes' : `${mode}Strokes`;
    assert.equal(e.mask[key].length, 1);
  }
});

test('hidden pins do not leave invisible draggable targets', () => {
  const e = editor(); e.S.localPinsVisible = false;
  e.fire('pointerdown', [0.25, 0.5]);
  assert.equal(e.S.editGesture.handle, null);
});

test('handle and line hit areas use CSS pixels on zoomed portrait and landscape photos', () => {
  for (const [width, height] of [[900, 600], [600, 900], [7200, 4800]]) {
    const mask = initial(), r = {width, height};
    assert.equal(linearHandleAt(mask, [0.25, 0.5 + 10 / height], r), 'start');
    assert.equal(linearHandleAt(mask, [0.25, 0.5 + 12 / height], r), null);
    assert.equal(linearHandleAt(mask, [0.5, 0.5 + 6 / height], r), 'move');
    assert.equal(linearHandleAt(mask, [0.5, 0.5 + 8 / height], r), null);
    assert.equal(linearHandleAt(mask, [0.9, 0.5], r), null);
  }
});

test('short and zero-length gradients choose the closest endpoint and can be expanded', () => {
  const mask = initial(); mask.end = [0.26, 0.5];
  assert.equal(linearHandleAt(mask, [0.25, 0.5], rect), 'start');
  assert.equal(linearHandleAt(mask, [0.26, 0.5], rect), 'end');
  mask.end = [...mask.start];
  assert.equal(linearHandleAt(mask, mask.start, rect), 'end');
  assert.equal(linearHandleAt(mask, [0.7, 0.5], rect), null);
});

test('moving a diagonal gradient clamps a shared offset on both axes', () => {
  const mask = {...initial(), start: [0.8, 0.1], end: [0.2, 0.9]};
  const gesture = {handle: 'move', origin: [0.5, 0.5], start: mask.start, end: mask.end};
  editLinear(mask, gesture, [1, 0]);
  near(mask.start, [1, 0]); near(mask.end, [0.4, 0.8]);
});

test('cursor distinguishes a draggable dot or line from drawing and painting', () => {
  const mask = initial(), overlay = { style: {}, classList: {toggle() {}} };
  const S = {activePane: 'maskPane', localPinsVisible: true, overlayHoverPoint: [0.25, 0.5]};
  const sync = new Function('S', '$', 'selectedMask', 'editOverlayCursor', source.slice(
    source.indexOf('function syncOverlayCursorClass()'), source.indexOf('function syncViewerChrome()')) +
    '\nreturn syncOverlayCursorClass;')(S,
    id => id === 'cv' ? {getBoundingClientRect: () => rect} : overlay, () => mask, editOverlayCursor);
  sync(); assert.equal(overlay.style.cursor, 'grab');
  S.overlayHoverPoint = [0.5, 0.5]; sync(); assert.equal(overlay.style.cursor, 'grab');
  S.editGesture = {type: 'linear', handle: 'move'}; sync(); assert.equal(overlay.style.cursor, 'grabbing');
  S.editGesture = null; S.overlayHoverPoint = [0.1, 0.1]; sync(); assert.equal(overlay.style.cursor, 'crosshair');
  S.overlayHoverPoint = [0.25, 0.5]; S.localPinsVisible = false;
  sync(); assert.equal(overlay.style.cursor, 'crosshair');
  S.localPinsVisible = true; S.maskRefineMode = 'add'; sync(); assert.equal(overlay.style.cursor, 'none');
  S.maskRefineMode = null; S.maskColorPick = true; sync(); assert.equal(overlay.style.cursor, 'crosshair');
});


test('losing pointer capture clears the drag cursor without sampling an unfinished color area', () => {
  const e = editor();
  e.fire('pointerdown', [.25, .5]);
  e.fire('lostpointercapture', [.25, .5]);
  assert.equal(e.S.editGesture, null);
  assert.equal(e.captured(), null);
  e.S.maskColorPick = true;
  e.fire('pointerdown', [.3, .4], {shiftKey:true});
  e.fire('lostpointercapture', [.3, .4]);
  assert.equal(e.S.editGesture, null);
  assert.deepEqual(e.samples, []);
});
