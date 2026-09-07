import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

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

test('quick cache hits never show; draft, decode, and final paint share a steady badge', () => {
  const timer = clock(), updates = [];
  const progress = createPreviewProgress(state => updates.push(state), timer);
  progress.start('Applying film…'); timer.advance(40); progress.finish();
  timer.advance(500);
  assert.ok(updates.every(state => !state.visible));
  progress.start('Applying film…'); timer.advance(150);
  const shown = updates.length - 1;
  progress.start('Refining RAW detail…'); timer.advance(2000);
  progress.start('Finishing RAW preview…'); timer.advance(100);
  progress.finish(); timer.advance(100);
  progress.start('Updating preview detail…'); timer.advance(1000);
  assert.ok(updates.slice(shown).every(state => state.visible));
  progress.finish(); timer.advance(140);
  assert.equal(updates.at(-1).visible, false);
});

test('visible work holds for a minimum duration and errors clear busy immediately', () => {
  const timer = clock(), updates = [];
  const progress = createPreviewProgress(state => updates.push(state), timer);
  progress.start('Working'); timer.advance(150); progress.finish();
  timer.advance(399); assert.equal(updates.at(-1).visible, true);
  timer.advance(1); assert.equal(updates.at(-1).visible, false);
  progress.start('Working'); timer.advance(150);
  progress.finish({ error: 'Could not finish preview' });
  assert.deepEqual(updates.at(-1), { active: false, visible: true, label: 'Could not finish preview' });
  progress.start('Next photo'); timer.advance(500);
  assert.equal(updates.at(-1).label, 'Next photo');
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
  const requests = [], displays = [], progress = [], nodes = new Map();
  const noop = () => {};
  let finishDecode, finishPaint;
  const context = {
    S, performance, console, setTimeout, clearTimeout, CLIENT_ID: 'review',
    prefetchTimer: null, refineTimer: null, viewportRegionTimer: null,
    interactiveRenderPhoto: 'photo.dng', lastContinuousInputAt: -Infinity,
    INTERACTIVE_PREVIEW_WIDTH: 1100, FULL_RESOLUTION_SETTLE_MS: 200,
    PERF: { renders: [] }, window: { dispatchEvent: noop },
    CustomEvent: class { constructor(type, detail) { this.detail = detail; } },
    cur: () => ({ name: 'photo.dng' }),
    $: id => { if (!nodes.has(id)) nodes.set(id, { value: id === 'pw' ? '2200' : 'rs' }); return nodes.get(id); },
    readControls: noop, requestedPreviewWidth: () => 2200,
    requestedViewportRegion: () => null, nativePreviewActive: () => false,
    renderRequestKey: () => 'key', presentationCache: { get: noop, set: noop },
    previewGeometryKey: noop, shouldPreservePresentationGeometry: () => true,
    previewProgress: { start: label => progress.push(label), finish: () => progress.push('done') },
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
    finishDecode: () => finishDecode(), finishPaint: () => finishPaint() };
}

test('actual RAW render waits on one generation, retains accurate pixels, and finishes after paint', async () => {
  const app = renderHarness();
  const rendered = app.render();
  await tick();
  assert.deepEqual(app.requests.map(r => r.path), ['/api/render', ...Array(4).fill('/api/refine')]);
  assert.ok(app.requests.every(r => r.generation === 1));
  assert.deepEqual(app.displays, [], 'draft must not replace existing accurate pixels');
  assert.ok(!app.progress.includes('done'));
  app.finishDecode(); await tick();
  assert.equal(app.requests.at(-1).path, '/api/render');
  assert.equal(app.requests.at(-1).generation, 2);
  assert.deepEqual(app.displays, [2]);
  assert.ok(!app.progress.includes('done'), 'HTTP response is not presentation completion');
  app.finishPaint(); await rendered;
  assert.equal(app.progress.at(-1), 'done');
});

test('obsolete RAW completion neither renders nor hides progress for the new photo', async () => {
  const app = renderHarness();
  const rendered = app.render(); await tick();
  app.S.seq++;
  app.finishDecode(); await rendered;
  assert.equal(app.requests.filter(r => r.path === '/api/render').length, 1);
  assert.ok(!app.progress.includes('done'));
});
