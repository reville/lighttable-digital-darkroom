import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { gradeBakeRequest, gradeBakeKey } from '../web/preview-processing.js';

const source = readFileSync(new URL('../web/preview-progress.js', import.meta.url), 'utf8');
const { createPreviewProgress, waitForRawRefinement } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const appSource = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const renderSource = appSource.match(/^async function doRender\([^]*?^}/m)[0];
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

// Run the actual orchestration with only HTTP and display boundaries stubbed.
// A slow decode spans several polls; no new render may invalidate its generation.
function renderHarness() {
  const S = { seq: 0, params: { profile_enabled: true }, renderState: 'ready',
    presentedPhotoName: 'photo.dng', optics: {}, heals: [] };
  const requests = [], displays = [], progress = [], scheduled = [], nodes = new Map();
  const noop = () => {};
  let finishDecode, finishPaint;
  const context = {
    S, performance, console, setTimeout: fn => scheduled.push(fn), clearTimeout: noop, CLIENT_ID: 'review',
    gradeBakeRequest, gradeBakeKey,
    prefetchTimer: null, refineTimer: null, viewportRegionTimer: null,
    interactiveRenderPhoto: 'photo.dng', lastContinuousInputAt: -Infinity,
    INTERACTIVE_PREVIEW_WIDTH: 1100, FULL_RESOLUTION_SETTLE_MS: 200,
    PERF: { renders: [] }, window: { dispatchEvent: noop },
    CustomEvent: class { constructor(type, detail) { this.detail = detail; } },
    cur: () => ({ name: 'photo.dng' }),
    $: id => { if (!nodes.has(id)) nodes.set(id, { value: id === 'pw' ? '2200' : 'rs', setAttribute: noop }); return nodes.get(id); },
    readControls: noop, requestedPreviewWidth: () => 2200,
    requestedViewportRegion: () => null, nativePreviewActive: () => false,
    renderRequestKey: () => 'key', presentationCache: { get: noop, set: noop },
    previewGeometryKey: noop, shouldPreservePresentationGeometry: () => true,
    previewProgress: { start: label => progress.push(label), advance: noop, finish: () => progress.push('done') },
    waitForRawRefinement: options => waitForRawRefinement({ ...options, sleep: async () => {} }),
    api: async (path, body) => {
      requests.push({ path, generation: body.generation });
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
    setRenderPresentation: noop, drawGrade: noop, syncOpticsPanel: noop,
    syncBrowserOriginal: noop, prefetch: noop,
  };
  vm.runInNewContext(`${renderSource}\nglobalThis.render = doRender;`, context);
  return { ...context, requests, displays, progress,
    finishDecode: () => finishDecode(), finishPaint: () => finishPaint(),
    runScheduled: () => scheduled.shift()() };
}

test('actual RAW render waits on one generation, retains accurate pixels, and finishes after paint', async () => {
  const app = renderHarness();
  const rendered = app.render();
  await tick();
  assert.deepEqual(app.requests.map(r => r.path), ['/api/render', ...Array(4).fill('/api/refine')]);
  assert.ok(app.requests.every(r => r.generation === 1));
  assert.deepEqual(app.displays, [], 'draft must not replace existing accurate pixels');
  assert.equal(app.progress.at(-1), 'done', 'an existing usable preview hides progress during RAW work');
  app.finishDecode(); await tick();
  assert.equal(app.requests.at(-1).path, '/api/render');
  assert.equal(app.requests.at(-1).generation, 2);
  assert.deepEqual(app.displays, [2]);
  assert.equal(app.progress.filter(value => value !== 'done').length, 1, 'background refinement must not restart the bar');
  app.finishPaint(); await rendered;
  assert.equal(app.progress.at(-1), 'done');
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
