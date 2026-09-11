// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { createPresentationCache, renderRequestKey } from '../web/presentation-cache.js';
import { gradeBakeRequest } from '../web/preview-processing.js';
import { automaticPreviewWidth } from '../web/view-performance.js';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const image = { name: 'a.dng', fileKey: 'original', mtime: 1 };
const request = { w: 6000, engine: 'rs', native: true, params: { rotate: 0 }, optics: {}, heals: [] };

test('navigation reuses the best whole-photo surface with the exact source and baked edits', () => {
  const cache = createPresentationCache();
  const put = (im, req, label) => cache.set(renderRequestKey(im, req), { label });
  put(image, { ...request, w: 1100 }, 'small');
  put(image, { ...request, w: 3000 }, 'fit');
  put(image, { ...request, viewport: {x: 0, y: 0, width: 600, height: 400} }, 'tile');
  put({ ...image, mtime: 2 }, request, 'old-file');
  put(image, { ...request, params: { rotate: 90 } }, 'rotated');
  put(image, { ...request, native: false }, 'other-backend');
  put(image, { ...request, grade: {exposure: 1}, masks: [{grade: {texture: 1}}] }, 'masked');
  assert.equal(cache.findPreview(image, request).value.label, 'fit');
  assert.equal(cache.findPreview({...image, name: 'other.dng'}, request), null);
  put(image, { ...request, w: 8000 }, 'larger');
  assert.equal(cache.findPreview(image, request).value.label, 'larger');
  put(image, request, 'exact');
  assert.equal(cache.findPreview(image, request).value.label, 'exact');
  cache.set(renderRequestKey(image, {...request, w: 7000}), {refining: true});
  assert.equal(cache.findPreview(image, {...request, w: 7000}).value.label, 'larger');
});

function prefetchHarness() {
  const tasks = [], calls = [], loads = [];
  const photos = [
    {name: 'previous.dng', width: 4000, height: 6000},
    {name: 'current.dng', width: 6000, height: 4000},
    {name: 'next.dng', width: 5000, height: 3000, params: {rotate: 90},
      grade: {exposure: 1}, masks: [{grade: {texture: 1}}]},
  ];
  const context = {
    S: {seq: 5, zoomMode: '100', zoom: 6, params: {}, targetPixelScale: 1},
    navigationGeneration: 3, CLIENT_ID: 'test', GRADE_DEFAULTS: {}, OPTICS_DEFAULTS: {},
    INTERACTIVE_PREVIEW_WIDTH: 1100, window: {devicePixelRatio: 1},
    $: id => id === 'zoomwrap' ? {clientWidth: 1000, clientHeight: 800}
      : {value: id === 'pw' ? 'auto' : 'rs'},
    cur: () => photos[1], visible: () => photos, previewCrop: () => null,
    normalizeFilmParams: params => params || {},
    prefetchState: async () => {}, gradeBakeRequest, automaticPreviewWidth,
    renderRequestKey, presentationCache: createPresentationCache(), nativePreviewActive: () => true,
    api: async (path, body) => {calls.push(body); return {native: {key: calls.length}};},
    postNative: (command, args) => loads.push({command, args}),
    setTimeout: fn => tasks.push(fn), clearTimeout() {},
  };
  for (const name of ['requestedPreviewWidth', 'prefetchImage', 'prefetch']) {
    vm.runInNewContext(source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, 'm'))[0], context);
  }
  vm.runInNewContext('let prefetchTimer = null; let lastNavigationDirection = 1;', context);
  return {context, calls, loads, photos, run: async () => {context.prefetch(); await tasks.shift()();}};
}

test('prefetch warms both neighbors before detail, using their own dimensions and baked masks', async () => {
  const app = prefetchHarness();
  await app.run();
  assert.deepEqual(app.calls.map(r => [r.name, r.w]), [
    ['next.dng', 1100], ['previous.dng', 1100], ['next.dng', 5000], ['previous.dng', 6000],
  ]);
  assert.ok(app.calls.every(r => r.allow_draft === false && r.priority === 'prefetch'));
  assert.equal(app.calls[0].grade.exposure, 1);
  assert.equal(app.calls[0].masks[0].grade.texture, 1);
  assert.equal(app.loads.length, 4);
});

test('navigation or edits during prefetch stop the remaining work and stale native preload', async () => {
  for (const cancel of [app => app.navigationGeneration++, app => app.S.seq++]) {
    const app = prefetchHarness();
    app.context.api = async (_path, body) => {
      app.calls.push(body); cancel(app.context); return {native: {key: 'late'}};
    };
    await app.run();
    assert.equal(app.calls.length, 1);
    assert.equal(app.loads.length, 0);
  }
});

test('retained custom zoom sizes portrait and cropped neighbors at their future pixel scale', () => {
  const {context: app, photos} = prefetchHarness();
  app.S.zoomMode = 'custom'; app.S.targetPixelScale = 0.5;
  // Current fitted zoom is irrelevant to the portrait neighbor's new fit.
  assert.equal(app.requestedPreviewWidth(photos[0], {}, null), 3000);
  assert.equal(app.requestedPreviewWidth(photos[0], {}, {w: 0.8, h: 0.8}), 3000);
  assert.equal(app.requestedPreviewWidth(photos[2], {rotate: 90}, null), 2600);
});

test('native viewport detail stays in the background after a navigation preview is visible', () => {
  const pending = [], renders = [];
  const app = {
    S: {presentedPhotoName: 'new.dng', renderState: 'ready'},
    zoomMotion: {active: false}, lastViewportRenderKey: null, viewportRegionTimer: null,
    cur: () => ({name: 'new.dng'}), performance: {now: () => 0},
    requestedViewportRegion: () => ({x: 50, y: 50, width: 1000, height: 800}),
    requestedPreviewWidth: () => 6000, clearTimeout() {}, setTimeout: fn => pending.push(fn),
    doRender: (_time, options) => renders.push(options),
  };
  vm.runInNewContext(source.match(/^function scheduleViewportRegionRender\([^]*?^}/m)[0], app);
  app.scheduleViewportRegionRender(); pending.shift()();
  assert.equal(renders[0].background, true);
  assert.equal(renders[0].width, 6000);
});
