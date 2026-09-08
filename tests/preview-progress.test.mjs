import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { gradeBakeRequest, gradeBakeKey } from '../web/preview-processing.js';
import { previewFailureMessage } from '../web/preview-detail.js';
import { createFrameScheduler } from '../web/render-scheduler.js';

import { createPreviewProgress, waitForRawRefinement } from '../web/preview-progress.js';
import {t as tr, tn as trn} from '../web/i18n.js';
const appSource = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const renderSource = appSource.match(/^async function doRender\([^]*?^}/m)[0];
const nativeEventSource = appSource.match(/^window.lightTableNativeEvent = \(event\) => \{[^]*?^};/m)[0];
const autoSource = appSource.match(/^function scheduleAutomaticPreview\([^]*?^}/m)[0];
const progressiveSource = appSource.match(/^function scheduleProgressiveRender\([^]*?^}/m)[0];
const tick = () => new Promise(resolve => setImmediate(resolve));

function clock() {
  let time = 0, id = 0;
  const timers = new Map();
  return {
    now: () => time,
    schedule: (fn, delay) => { timers.set(++id, { fn, at: time + delay }); return id; },
    cancel: id => timers.delete(id),
    advance(ms) {
      const end = time + ms;
      for (;;) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        time = next[1].at; timers.delete(next[0]); next[1].fn();
      }
      time = end;
    },
  };
}

function zoomHarness() {
  const timer = clock(), renders = [];
  const context = {
    S: { viewMode: 'detail', renderState: 'ready', presentedPhotoName: 'photo.dng',
      previewDetail: { name: 'photo.dng', refining: false, renderedWidth: 2200 } },
    automaticPreviewRequest: { name: 'photo.dng', width: 2200 },
    automaticPreviewTimer: null, renderTimer: null, settleRenderTimer: null,
    zoomMotion: {active: false},
    lastInteractiveRenderAt: 0, INTERACTIVE_PREVIEW_WIDTH: 1100,
    performance: { now: timer.now }, setTimeout: timer.schedule, clearTimeout: timer.cancel,
    $: () => ({ value: 'auto' }), cur: () => ({ name: 'photo.dng' }),
    width: 3000, requestedPreviewWidth: () => context.width,
    viewFrameScheduler: { flush() {} }, viewportRegionEnabled: () => false,
    doRender: (_time, options) => {
      renders.push(options);
      context.automaticPreviewRequest = { name: 'photo.dng', width: options.width };
    },
  };
  vm.runInNewContext(`${autoSource}\n${progressiveSource}`, context);
  return { context, timer, renders };
}

test('zoom asks once for larger detail, keeps it on zoom out, and coalesces rapid clicks', () => {
  const { context: app, timer, renders } = zoomHarness();
  app.scheduleAutomaticPreview(); timer.advance(100);
  app.width = 4000; app.scheduleAutomaticPreview(); timer.advance(200);
  assert.deepEqual(renders.map(r => r.width), [4000], 'never insert an 1100px zoom preview');
  assert.equal(renders[0].background, true);
  assert.equal(renders[0].phase, 'settled');
  app.scheduleAutomaticPreview(); timer.advance(200);
  assert.equal(renders.length, 1, 'do not duplicate an in-flight detail request');
  app.S.previewDetail.renderedWidth = 4000;
  app.width = 1800; app.scheduleAutomaticPreview(); timer.advance(200);
  app.width = 3500; app.scheduleAutomaticPreview(); timer.advance(200);
  assert.equal(renders.length, 1, 'reuse the sharp surface in both directions');
  app.width = 5000; app.scheduleAutomaticPreview(); timer.advance(200);
  assert.deepEqual(renders.map(r => r.width), [4000, 5000]);
});

test('animation defers detail rendering until the final viewport settles', () => {
  const { context: app, timer, renders } = zoomHarness();
  app.scheduleAutomaticPreview(); timer.advance(100);
  app.zoomMotion.active = true;
  app.scheduleAutomaticPreview(); timer.advance(500);
  assert.equal(renders.length, 0);
  app.width = 5000; app.zoomMotion.active = false;
  app.scheduleAutomaticPreview(); timer.advance(200);
  assert.deepEqual(renders.map(r => r.width), [5000]);
  assert.equal(renders[0].background, true);
});

test('navigation opens at requested detail while editing retains its responsive small pass', () => {
  const { context: app, timer, renders } = zoomHarness();
  app.S.renderState = 'pending';
  app.scheduleProgressiveRender(0); timer.advance(0);
  assert.equal(renders[0].width, 3000);
  assert.equal(renders[0].phase, 'settled');
  app.S.renderState = 'ready';
  app.scheduleProgressiveRender(0); timer.advance(0);
  assert.equal(renders[1].width, 1100);
  assert.equal(renders[1].phase, 'interactive');
});

test('failed zoom detail is retryable and an outgoing photo cannot suppress the new preview', () => {
  const { context: app, timer, renders } = zoomHarness();
  app.automaticPreviewRequest = null;
  app.scheduleAutomaticPreview(); timer.advance(200);
  assert.equal(renders.length, 1);
  app.S.renderState = 'pending';
  app.S.presentedPhotoName = 'previous.dng';
  app.S.previewDetail = { name: 'previous.dng', refining: false, renderedWidth: 8000 };
  app.automaticPreviewRequest = { name: 'previous.dng', width: 8000 };
  app.scheduleAutomaticPreview(); timer.advance(200);
  assert.equal(renders.length, 2);
  assert.equal(renders[1].background, false);
});

test('progress only advances on completed stages and hides as soon as the preview is painted', () => {
  const timer = clock(), updates = [];
  const progress = createPreviewProgress(state => updates.push(state), timer);
  progress.start('Applying film…', 1); timer.advance(40); progress.finish();
  timer.advance(500);
  assert.ok(updates.every(state => !state.visible), 'quick cache hits stay silent');
  progress.start('Applying film…', 2); timer.advance(150);
  assert.equal(updates.at(-1).completed, 0);
  progress.advance(1, 2); assert.equal(updates.at(-1).completed, 1);
  timer.advance(2000); assert.equal(updates.at(-1).completed, 1, 'time is not progress');
  progress.advance(3, 2); assert.equal(updates.at(-1).completed, 3);
  progress.advance(2, 2); assert.equal(updates.at(-1).completed, 3, 'out of order stages do not regress');
  progress.advance(5, 2); assert.equal(updates.at(-1).completed, 4, 'server cannot claim presentation');
  progress.finish(); assert.equal(updates.at(-1).visible, false, 'no completion linger');
  progress.advance(4, 2); timer.advance(1000);
  assert.equal(updates.at(-1).visible, false, 'late server events cannot reopen the bar');
});

test('new requests reset progress and reject obsolete generations; errors clear busy immediately', () => {
  const timer = clock(), updates = [];
  const progress = createPreviewProgress(state => updates.push(state), timer);
  progress.start('Working', 1); timer.advance(150); progress.advance(3, 1);
  progress.start('Next photo', 2);
  progress.advance(4, 1); assert.equal(updates.at(-1).completed, 0);
  progress.advance(2, 2); assert.equal(updates.at(-1).completed, 2);
  progress.finish({ error: 'Could not finish preview' });
  assert.deepEqual(updates.at(-1), { active: false, visible: true,
    label: 'Could not finish preview', completed: 0, generation: null });
  progress.start('Next photo', 3);
  assert.equal(updates.at(-1).completed, 0);
});

test('RAW polling stops on navigation, failure, or the retry limit', async () => {
  let current = true, calls = 0;
  const result = await waitForRawRefinement({
    isCurrent: () => current,
    request: async () => { calls++; current = false; return { ready: true }; },
  });
  assert.equal(result, false); assert.equal(calls, 1);
  await assert.rejects(waitForRawRefinement({ isCurrent: () => true,
    request: async () => ({ error: 'decode failed' }) }), /decode failed/);
  await assert.rejects(waitForRawRefinement({ isCurrent: () => true,
    request: async () => ({ ready: false }), maxAttempts: 3, sleep: async () => {} }), /could not finish/);
});

test('ready RAW detail is detected within 100 ms without shortening the slow-decoder allowance', async () => {
  let time = 0;
  await waitForRawRefinement({ isCurrent: () => true,
    request: async () => ({ ready: time >= 1250 }), sleep: async ms => { time += ms; } });
  assert.equal(time, 1300);
  time = 0;
  await waitForRawRefinement({ isCurrent: () => true,
    request: async () => ({ ready: time >= 179000 }), sleep: async ms => { time += ms; } });
  assert.equal(time, 179000);
});

// Run the actual orchestration with only HTTP and display boundaries stubbed.
// A slow decode spans several polls; no new render may invalidate its generation.
function renderHarness(overrides = {}) {
  const S = { seq: 0, params: { profile_enabled: true }, renderState: 'ready',
    presentedPhotoName: 'photo.dng', optics: {}, heals: [],
    previewDetail: { name: 'photo.dng', refining: false } };
  const requests = [], displays = [], progress = [], presentations = [], scheduled = [], nodes = new Map();
  const noop = () => {};
  let finishDecode, finishPaint;
  const context = {tr, trn,
    S, performance, console, setTimeout: fn => scheduled.push(fn), clearTimeout: noop, CLIENT_ID: 'review',
    gradeBakeRequest, gradeBakeKey, previewFailureMessage,
    prefetchTimer: null, refineTimer: null, viewportRegionTimer: null, automaticPreviewRequest: null,
    interactiveRenderPhoto: 'photo.dng', lastContinuousInputAt: -Infinity,
    INTERACTIVE_PREVIEW_WIDTH: 1100, FULL_RESOLUTION_SETTLE_MS: 200,
    PERF: { renders: [] }, window: { dispatchEvent: noop },
    CustomEvent: class { constructor(type, detail) { this.detail = detail; } },
    cur: () => ({ name: 'photo.dng' }),
    $: id => { if (!nodes.has(id)) nodes.set(id, { value: id === 'pw' ? '2200' : 'rs',
      setAttribute(key, value) { this[key] = value; } }); return nodes.get(id); },
    readControls: noop, viewFrameScheduler: { flush: noop }, requestedPreviewWidth: () => 2200,
    requestedViewportRegion: () => null, nativePreviewActive: () => false,
    renderRequestKey: () => 'key', presentationCache: { get: noop, set: noop },
    previewGeometryKey: noop, shouldPreservePresentationGeometry: () => true,
    previewProgress: { start: label => progress.push(label), advance: noop, finish: () => progress.push('done') },
    waitForRawRefinement: options => waitForRawRefinement({ ...options, sleep: async () => {} }),
    api: async (path, body) => {
      requests.push({ path, generation: body.generation, width: body.w, allowDraft: body.allow_draft });
      if (path === '/api/refine') {
        if (requests.filter(r => r.path === path).length < 4) return { ready: false };
        return new Promise(resolve => { finishDecode = () => resolve({ ready: true }); });
      }
      return { refining: body.generation === 1, cached: false, ms: 10 };
    },
    setBaseImage: async (m, generation) => {
      displays.push(generation);
      return new Promise(resolve => { finishPaint = () => resolve({ presentedAt: performance.now(), uploadedAt: performance.now() }); });
    },
    setRenderPresentation: (...args) => presentations.push(args), drawGrade: noop, syncOpticsPanel: noop,
    syncBrowserOriginal: noop, prefetch: noop,
    ...overrides,
  };
  vm.runInNewContext(`${nativeEventSource}\n${renderSource}\nglobalThis.render = doRender;`, context);
  return { ...context, requests, displays, progress, presentations,
    finishDecode: () => finishDecode(), finishPaint: () => finishPaint(),
    runScheduled: () => scheduled.shift()() };
}

test('native 1:1 request applies queued zoom layout before measuring its source region', async () => {
  const previousRequest = globalThis.requestAnimationFrame;
  const previousCancel = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  try {
    let canvasWidth = 600;
    const scheduler = createFrameScheduler(() => { canvasWidth = 3000; });
    scheduler.request({ view: true });
    const pixelWindow = vm.runInNewContext(`(${appSource.match(/^function viewportPixelWindow\([^]*?^}/m)[0]})`);
    const requests = [];
    const app = renderHarness({
      viewFrameScheduler: scheduler,
      requestedPreviewWidth: () => 3000,
      nativePreviewActive: () => true,
      requestedViewportRegion: () => pixelWindow(
        { left: 0, top: 0, right: canvasWidth, bottom: canvasWidth * 2 / 3,
          width: canvasWidth, height: canvasWidth * 2 / 3 },
        { left: 0, top: 0, right: 600, bottom: 400 }, 3000, 2000),
      api: async (_path, body) => { requests.push(body); return {}; },
      setBaseImage: async () => ({ presentation: 'native-metal',
        presentedAt: performance.now(), uploadedAt: performance.now() }),
    });
    await app.render();
    assert.equal(requests.length, 1);
    assert.ok(requests[0].viewport.width < 900, 'must not request the old fitted full image');
    assert.ok(requests[0].viewport.height < 700);
    assert.equal(app.PERF.renders[0].presentation, 'native-metal');
  } finally {
    globalThis.requestAnimationFrame = previousRequest;
    globalThis.cancelAnimationFrame = previousCancel;
  }
});

test('actual RAW render waits on one generation, retains accurate pixels, and finishes after paint', async () => {
  const app = renderHarness();
  const rendered = app.render();
  await tick();
  assert.deepEqual(app.requests.map(r => r.path), ['/api/render', ...Array(4).fill('/api/refine')]);
  assert.ok(app.requests.every(r => r.generation === 1));
  assert.deepEqual(app.displays, [], 'draft must not replace existing accurate pixels');
  assert.equal(app.progress.at(-1), 'done', 'an existing usable preview hides progress during RAW work');
  assert.equal(app.requests[0].allowDraft, false, 'do not compute a draft that cannot be displayed');
  app.finishDecode(); await tick();
  assert.equal(app.requests.at(-1).path, '/api/render');
  assert.equal(app.requests.at(-1).generation, 2);
  assert.deepEqual(app.displays, [2]);
  assert.equal(app.progress.filter(value => value !== 'done').length, 1, 'background refinement must not restart the bar');
  app.finishPaint(); await rendered;
  assert.equal(app.progress.at(-1), 'done');
});

test('native failure reason survives the desktop event and render pipeline', async () => {
  for (const reason of ['Metal could not allocate the preview texture.', undefined]) {
    const nativePreviewPending = new Map(), toasts = [];
    const app = renderHarness({
      nativePreviewPending, toast: message => toasts.push(message),
      api: async () => ({}),
      setBaseImage: (_, generation) => new Promise(resolve => nativePreviewPending.set(generation, { resolve })),
    });
    const rendered = app.render(); await tick();
    app.window.lightTableNativeEvent({ type: 'nativePreviewFailed', generation: app.S.seq, message: reason });
    await rendered;
    const expected = reason || 'Native preview unavailable';
    assert.deepEqual(app.presentations, [['error', 'photo.dng', `Could not display this photo\n${expected}`]]);
    assert.deepEqual(toasts, [expected]);
    assert.equal(app.$('zoomwrap')['aria-busy'], 'false');
    assert.equal(nativePreviewPending.size, 0);
  }
});

test('server and thrown failures show their reported cause in the photo area', async () => {
  for (const [api, expected] of [
    [async () => ({ error: 'The original file is missing.' }), 'Could not render this photo\nThe original file is missing.'],
    [async () => { throw new Error('Connection lost'); }, 'Could not finish this preview\nConnection lost'],
  ]) {
    const app = renderHarness({ api });
    app.S.renderState = 'pending'; app.S.renderName = 'photo.dng';
    await app.render();
    assert.deepEqual(app.presentations, [['error', 'photo.dng', expected]]);
  }
  assert.equal(previewFailureMessage('Could not display this photo', {}), 'Could not display this photo');
  assert.equal(previewFailureMessage('Could not display this photo', '  '), 'Could not display this photo');
});

test('a late native failure cannot replace the current photo error or show a stale toast', async () => {
  const nativePreviewPending = new Map(), toasts = [];
  const app = renderHarness({
    nativePreviewPending, toast: message => toasts.push(message), api: async () => ({}),
    setBaseImage: (_, generation) => new Promise(resolve => nativePreviewPending.set(generation, { resolve })),
  });
  const rendered = app.render(); await tick();
  const oldGeneration = app.S.seq++;
  app.window.lightTableNativeEvent({ type: 'nativePreviewFailed', generation: oldGeneration, message: 'Old failure' });
  await rendered;
  assert.deepEqual(app.presentations, []);
  assert.deepEqual(toasts, []);
});

test('a displayed neutral RAW draft is not mistaken for accurate pixels; refine before a large film render', async () => {
  const app = renderHarness();
  app.S.previewDetail.refining = true;
  const rendered = app.render(performance.now(), {
    width: 1100, requestedWidth: 2200, phase: 'interactive',
  });
  await tick();
  assert.deepEqual(app.displays, [1], 'the small film result must replace the neutral draft');
  assert.equal(app.requests[0].allowDraft, false, 'even a first render asks for consistent RAW pixels');
  assert.equal(app.requests.length, 1, 'first visible paint still takes priority over demosaic');
  app.finishPaint(); await tick();
  assert.deepEqual(app.requests.map(r => r.path), ['/api/render', ...Array(4).fill('/api/refine')]);
  assert.ok(app.requests.slice(1).every(r => r.width === 2200));
  app.finishDecode(); await tick();
  assert.deepEqual(app.requests.filter(r => r.path === '/api/render').map(r => r.width), [1100, 2200]);
  assert.deepEqual(app.displays, [1, 2]);
  app.finishPaint(); await rendered;
  assert.equal(app.progress.at(-1), 'done');
  assert.equal(app.PERF.renders.filter(r => r.presentation === 'draft-skipped').length, 0);
});

test('obsolete RAW completion neither renders nor hides progress for the new photo', async () => {
  const app = renderHarness();
  const rendered = app.render(); await tick();
  app.S.seq++;
  const progressBefore = [...app.progress];
  app.finishDecode(); await rendered;
  assert.equal(app.requests.filter(r => r.path === '/api/render').length, 1);
  assert.deepEqual(app.progress, progressBefore);
});


test('a new RAW draft hides progress after presentation while accurate detail keeps rendering', async () => {
  const app = renderHarness();
  app.S.renderState = 'pending';
  const rendered = app.render(); await tick();
  assert.deepEqual(app.displays, [1]);
  assert.ok(!app.progress.includes('done'), 'must wait for preview presentation');
  app.finishPaint(); await tick();
  assert.equal(app.progress.at(-1), 'done');
  assert.ok(app.requests.some(request => request.path === '/api/refine'));
  app.finishDecode(); await tick();
  assert.deepEqual(app.displays, [1, 2]);
  assert.equal(app.progress.filter(value => value !== 'done').length, 1);
  app.finishPaint(); await rendered;
});


test('full-size rendering stays silent after an interactive preview is displayed', async () => {
  const app = renderHarness();
  app.S.seq = 1; // Render a non-refining response.
  const rendered = app.render(performance.now(), {width: 1100, requestedWidth: 2200, phase: 'interactive'});
  await tick();
  assert.ok(!app.progress.includes('done'));
  app.finishPaint(); await rendered;
  assert.equal(app.progress.at(-1), 'done');
  app.runScheduled(); await tick();
  assert.deepEqual(app.displays, [2, 3]);
  assert.equal(app.progress.filter(value => value !== 'done').length, 1, 'full size is background work');
  app.finishPaint(); await tick();
  assert.equal(app.progress.at(-1), 'done');
});
