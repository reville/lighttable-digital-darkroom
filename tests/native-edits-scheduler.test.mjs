// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createFrameScheduler} from '../web/render-scheduler.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function withPreview(options, run) {
  const messages = [], frames = [];
  const elements = {lensReset: {},
    cv: {getBoundingClientRect: () => ({left: 0, top: 0, width: 200, height: 100})},
    editOverlay: {listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; },
      setPointerCapture() {}}};
  const S = {optics: {distortion: 0.7, vignette: 0.5}, grade: {chromaticAberrationRedCyan: 0.2},
    heals: [], baseEditsBaked: false, editGesture: null, healToolMode: 'heal',
    healBrush: {radius: 0.04, feather: 0.65, opacity: 1}, ...options};
  const defaults = {distortion: 0, vignette: 0, profileEnabled: false,
    profileOverride: '', profileDistortion: true, profileVignette: true};
  let renders = 0, saves = 0, overlays = 0;
  const context = {S, OPTICS_DEFAULTS: defaults, $: id => elements[id], createFrameScheduler,
    nativePreviewActive: () => options.native !== false,
    postNative: (type, payload) => messages.push({type, payload: structuredClone(payload)}),
    drawGradeNow: () => messages.push({type: 'nativeGrade'}),
    drawEditOverlayNow: () => overlays++, updateReferenceCompositeNow: () => {},
    spotVisualization: () => ({enabled: true, threshold: 0.6}),
    markContinuousInput: () => {}, GRADE_PERF: {input: () => {}},
    syncPreviewBackend: () => {}, renderPhysicalPreview: () => renders++, renderFilm: () => renders++,
    pushUndo: () => {}, syncOpticsPanel: () => {}, syncGrade: () => {}, saveState: () => saves++,
    clamp: (v, lo, hi) => Math.min(hi, Math.max(lo, v)), cur: () => ({name: 'photo'}),
    healHandleAt: () => null, MAX_HEALS: 50, toast: () => {}, editId: () => 'heal-1',
    syncHealPanel: () => {}, syncOverlayCursorClass: () => {}};
  const overlay = options.overlay
    ? between('function drawEditOverlay()', 'const CURVE_KEYS') +
      between('function automaticHealSource(', 'function refreshBaseEdits(') +
      between('function overlayPoint(', 'function overlayDistance(') +
      between("$('editOverlay').addEventListener('pointerdown'", 'function finishEditGesture(')
    : '';
  const previous = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  try {
    vm.runInNewContext(between('const previewFrameScheduler =', 'function drawEditOverlay()') +
      between('function drawGrade()', 'function refreshWebGLSamplingSurface()') +
      between('function nativeEditsPayload(', '/* ---------------------------------------------------------- local tools */') +
      between('function refreshBaseEdits(', 'function setHealToolMode(') +
      between("$('lensReset').onclick =", 'function overlayPoint(') + overlay +
      '\nthis.requestPreview = work => previewFrameScheduler.request(work);', context);
    run({S, messages, elements, context, flush: () => frames.splice(0).forEach(fn => fn()),
      counts: () => ({renders, saves, overlays})});
  } finally { globalThis.requestAnimationFrame = previous; }
}

test('Lens Reset forwards both grade and reset optics in the same frame', () => {
  withPreview({}, e => {
    e.elements.lensReset.onclick({stopPropagation() {}});
    assert.equal(e.messages.length, 0);
    e.flush();
    assert.deepEqual(e.messages.map(m => m.type), ['nativeGrade', 'nativeEdits']);
    assert.equal(e.messages[1].payload.optics.distortion, 0);
    assert.equal(e.messages[1].payload.optics.vignette, 0);
    assert.equal(e.S.grade.chromaticAberrationRedCyan, undefined);
    assert.deepEqual(e.counts(), {renders: 0, saves: 1, overlays: 1});
  });
});

test('coalesced grade and optics updates retain latest values in either request order', () => {
  for (const gradeFirst of [true, false]) withPreview({}, e => {
    const grade = () => e.context.drawGrade(), edits = () => e.context.refreshBaseEdits();
    (gradeFirst ? grade : edits)();
    e.S.optics.distortion = -0.3;
    (gradeFirst ? edits : grade)();
    e.context.requestPreview({visualization: true});
    e.flush();
    assert.deepEqual(e.messages.map(m => m.type), ['nativeGrade', 'nativeEdits', 'nativeSpotVisualization']);
    assert.equal(e.messages[1].payload.optics.distortion, -0.3);
    assert.equal(e.messages[2].payload.threshold, 0.6);
  });
});

test('baked and browser optics changes still use the physical renderer', () => {
  for (const options of [{baseEditsBaked: true}, {optics: {profileEnabled: true}},
    {heals: [{mode: 'remove', target: [.5, .5], source: [.5, .5], radius: .04}]},
    {native: false}]) withPreview(options, e => {
    e.context.drawGrade(); e.context.refreshBaseEdits(); e.flush();
    assert.equal(e.messages.filter(m => m.type === 'nativeEdits').length, 0);
    assert.equal(e.counts().renders, 1);
  });
});

const spot = {mode: 'clone', target: [.35, .5], source: [.7, .5], radius: .14};

test('Heal and Clone spots stay live in Metal while manual optics change', () => {
  const heals = [spot, {...spot, mode: 'heal', target: [.15, .2], source: [.15, .8], radius: .05},
    ...Array(15).fill({...spot, enabled: false})];
  withPreview({heals}, e => {
    e.context.refreshBaseEdits(true); e.flush();
    assert.deepEqual(e.messages.map(m => m.type), ['nativeEdits']);
    assert.equal(e.messages[0].payload.heals.length, 2);
    assert.equal(e.messages[0].payload.optics.distortion, 0.7);
    assert.equal(e.counts().renders, 0);
  });
  // Clone-over-clone only mixes sequentially, which the shader also orders.
  withPreview({heals: [spot, {...spot, target: [.45, .5], source: [.8, .8], radius: .1}]}, e => {
    e.context.refreshBaseEdits(); e.flush();
    assert.equal(e.messages.length, 1);
    assert.equal(e.counts().renders, 0);
  });
});

test('Remove, chained, overlapping Heal and seventeen spots bake the CPU reference', () => {
  for (const heals of [
    [{...spot, mode: 'remove', source: spot.target}],
    [spot, {...spot, target: [.15, .5], source: [.35, .5], radius: .1}],
    [spot, {...spot, mode: 'heal', target: [.45, .5], source: [.8, .8], radius: .1}],
    Array(17).fill(spot),
  ]) withPreview({heals}, e => {
    assert.equal(e.context.nativeBaseRequiresBake(), true);
    e.context.refreshBaseEdits(); e.flush();
    assert.equal(e.messages.length, 0);
    assert.equal(e.counts().renders, 1);
  });
});

function dragSpot(e) {
  const overlay = e.elements.editOverlay.listeners;
  overlay.pointerdown({button: 0, pointerId: 7, clientX: 100, clientY: 50,
    stopPropagation() {}, preventDefault() {}});
  assert.equal(e.S.heals.length, 1);
  assert.equal(e.S.editGesture.type, 'heal-create');
  assert.equal(e.messages.length, 0);
  e.flush();
  const afterAdd = e.messages.length;
  for (const clientX of [110, 120, 130]) overlay.pointermove({pointerId: 7, clientX, clientY: 50});
  assert.equal(e.messages.length, afterAdd);
  e.flush();
  return afterAdd;
}

test('adding and dragging a spot schedules one frame carrying the edits and overlay', () => {
  withPreview({overlay: true, activePane: 'healPane'}, e => {
    const afterAdd = dragSpot(e);
    assert.equal(afterAdd, 1);
    assert.deepEqual(e.messages.map(m => m.type), ['nativeEdits', 'nativeEdits']);
    assert.deepEqual(e.messages[0].payload.heals[0].target, [0.5, 0.5]);
    assert.equal(e.messages[1].payload.heals[0].radius, 0.25);
    assert.deepEqual(e.counts(), {renders: 0, saves: 0, overlays: 2});
  });
});

test('the WebGL fallback only redraws the overlay during a spot drag', () => {
  withPreview({overlay: true, activePane: 'healPane', native: false}, e => {
    assert.equal(dragSpot(e), 0);
    assert.equal(e.messages.length, 0);
    assert.deepEqual(e.counts(), {renders: 0, saves: 0, overlays: 2});
  });
});
