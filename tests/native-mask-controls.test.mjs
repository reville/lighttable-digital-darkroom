import assert from 'node:assert/strict';
import fs from 'node:fs';
import {test} from 'node:test';
import {gradeBakeRequest, gradeBakeKey} from '../web/preview-processing.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function harness({native = true, baked = false, dirty = false, dragging = false} = {}) {
  const mask = {id: 'gradient', type: 'linear', enabled: true, opacity: 1,
    lumaLow: 0, lumaHigh: 1, colorHue: null, colorRange: 30, colorAmount: 1,
    grade: {exposure: 0}};
  const messages = [], webgl = [], handlers = {};
  let rasters = 0, saves = 0, renders = 0;
  const input = {dataset: {local: 'exposure'}, value: '0',
    addEventListener: (type, handler) => { handlers[type] = handler; }, closest: () => null};
  const S = {grade: {}, masks: [mask], maskTextureDirty: dirty, renderState: 'ready',
    gradeEditsBaked: baked, activePane: 'maskPane', editGesture: dragging ? {} : null,
    gl: {draw: (...args) => webgl.push(args)}};
  S.presentedGradeKey = gradeBakeKey(gradeBakeRequest(S.grade, S.masks));
  const deps = {S, MAX_MASKS: 16, LOCAL_GRADE_DEFAULTS: {}, GRADE_DEFAULTS: {},
    gradeBakeRequest, gradeBakeKey, selectedMask: () => mask, fmtG: String,
    document: {querySelectorAll: () => [input], querySelector: () => ({})},
    pushUndo: () => {}, saveState: () => { saves++; }, drawEditOverlay: () => {},
    scheduleViewportRegionRender: () => {}, syncPreviewBackend: () => {},
    nativePreviewActive: () => native, renderPhysicalPreview: () => { renders++; },
    scheduleHistogram: () => {}, GRADE_PERF: {take: () => null},
    nativeGradePayload: grade => ({grade}), spotVisualization: () => ({enabled: false}),
    nativeMaskChannelPayload: () => ({channel: 0, tile: 0, data: 'channel', masks: [mask]}),
    buildMaskTexture: () => { rasters++; return {width: 2, height: 1, data: new Uint8Array(8)}; },
    bytesToBase64: bytes => Buffer.from(bytes).toString('base64'),
    postNative: (action, payload) => messages.push({action, payload: structuredClone(payload)})};
  const code = 'let packedMaskData = {};\n' + between('function drawGradeNow(', 'function drawGrade()') +
    between('function nativeMaskPayload(', 'function nativeMaskChannelPayload(') +
    between("document.querySelectorAll('[data-local]').forEach((input) => {\n  input.addEventListener", 'function automaticHealSource(') +
    '\nfunction drawGrade() { drawGradeNow(); } return drawGradeNow;';
  const draw = new Function(...Object.keys(deps), code)(...Object.values(deps));
  return {S, mask, messages, webgl, draw, handlers, input,
    change(key, value) { input.dataset.local = key; input.value = String(value); handlers.input(); },
    counts: () => ({rasters, saves, renders})};
}

if (process.argv.includes('--fixtures')) {
  // The native pixel test replays exactly what the real local-slider listener
  // sends, after the geometry has already been uploaded.
  const e = harness(), frames = [];
  for (const exposure of [0, 1, -1, 0]) {
    e.messages.length = 0;
    e.change('exposure', exposure);
    frames.push({exposure, messages: e.messages.slice()});
  }
  process.stdout.write(JSON.stringify(frames));
} else {
  test('native local sliders update mask settings even when geometry is unchanged', () => {
    const e = harness();
    for (const [key, value] of Object.entries({exposure: 2.12, contrast: 0.48,
      highlights: 0.54, shadows: -0.76, temp: 0.2, tint: -0.1, saturation: 0.5})) {
      e.messages.length = 0;
      e.change(key, value);
      const updates = e.messages.filter(message => message.action === 'nativeMasks');
      assert.equal(updates.length, 1, `${key} must reach the native mask renderer`);
      assert.equal(updates[0].payload.masks[0].grade[key], value);
      assert.ok(!('data' in updates[0].payload), 'slider motion must reuse mask geometry');
      assert.equal(e.counts().rasters, 0);
    }
    e.handlers.change(); assert.equal(e.counts().saves, 1);
    e.handlers.dblclick();
    assert.equal(e.messages.at(-1).payload.masks[0].grade.saturation, 0);
  });

  test('opacity, brightness/color ranges, enable and deletion update without raster work', () => {
    const e = harness();
    Object.assign(e.mask, {enabled: false, opacity: 0.3, lumaLow: 0.2, lumaHigh: 0.8,
      colorHue: 40, colorRange: 25, colorAmount: 0.6});
    e.draw();
    const payload = e.messages.at(-1).payload;
    for (const key of ['enabled', 'opacity', 'lumaLow', 'lumaHigh', 'colorHue', 'colorRange', 'colorAmount']) {
      assert.equal(payload.masks[0][key], e.mask[key]);
    }
    e.S.masks = []; e.draw();
    assert.deepEqual(e.messages.at(-1).payload.masks, []);
    assert.equal(e.counts().rasters, 0);
  });

  test('geometry edits retain full and provisional channel uploads', () => {
    for (const dragging of [false, true]) {
      const e = harness({dirty: true, dragging}); e.draw();
      const updates = e.messages.filter(message => message.action === 'nativeMasks');
      assert.equal(updates.length, 1);
      assert.ok('data' in updates[0].payload);
      assert.equal(e.counts().rasters, dragging ? 0 : 1);
      assert.equal(e.S.maskTextureDirty, false);
    }
  });

  test('baked local adjustments are not applied a second time', () => {
    const e = harness({baked: true}); e.draw();
    assert.deepEqual(e.messages.at(-1).payload.masks, []);
  });

  test('spatial local edits still wait for the ordered server render', () => {
    const e = harness(); e.change('texture', 0.3);
    assert.equal(e.counts().renders, 1); assert.equal(e.messages.length, 0);
  });

  test('browser previews continue drawing live local sliders without native messages', () => {
    const e = harness({native: false}); e.change('exposure', 1.5);
    assert.equal(e.webgl[0][1][0].grade.exposure, 1.5);
    assert.equal(e.messages.length, 0);
  });
}
