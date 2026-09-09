import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createFrameScheduler} from '../web/render-scheduler.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function withPreview(options, run) {
  const messages = [], frames = [], elements = {lensReset: {}};
  const S = {optics: {distortion: 0.7, vignette: 0.5}, grade: {chromaticAberrationRedCyan: 0.2},
    heals: [], baseEditsBaked: false, ...options};
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
    pushUndo: () => {}, syncOpticsPanel: () => {}, syncGrade: () => {}, saveState: () => saves++};
  const previous = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  try {
    vm.runInNewContext(between('const previewFrameScheduler =', 'function drawEditOverlay()') +
      between('function drawGrade()', 'function refreshWebGLSamplingSurface()') +
      between('function nativeEditsPayload(', '/* ---------------------------------------------------------- local tools */') +
      between('function refreshBaseEdits(', 'function setHealToolMode(') +
      between("$('lensReset').onclick =", 'function overlayPoint(') +
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
    {heals: [{enabled: true}]}, {native: false}]) withPreview(options, e => {
    e.context.drawGrade(); e.context.refreshBaseEdits(); e.flush();
    assert.equal(e.messages.filter(m => m.type === 'nativeEdits').length, 0);
    assert.equal(e.counts().renders, 1);
  });
});
