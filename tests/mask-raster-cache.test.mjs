import test from 'node:test';
import assert from 'node:assert/strict';
import { createStrokeRasterCache, strokeCoverage, accumulateStroke } from '../web/mask-raster.js';

const stroke = (settings = {}) => ({ buildUp: true, size: 0.25, feather: 0.7,
  flow: 0.2, density: 0.8, points: [[0.1, 0.2], [0.3, 0.5], [0.7, 0.4]], ...settings });
const legacy = (item, width, height) => Uint8Array.from(strokeCoverage(item, width, height),
  value => Math.round(value * item.flow * 255));
function full(strokes, width, height, edgeValues = () => null) {
  const combined = new Float32Array(width * height);
  for (const item of strokes) {
    if (item.buildUp) {
      const edge = item.edgeMask ? edgeValues(item, width, height) : null;
      if (item.edgeMask && !edge) continue;
      accumulateStroke(combined, strokeCoverage(item, width, height), item, edge);
    } else {
      const values = legacy(item, width, height);
      for (let i = 0; i < values.length; i++) combined[i] = Math.max(combined[i], values[i] / 255);
    }
  }
  return Uint8Array.from(combined, value => Math.round(value * 255));
}

test('incremental tail coverage is byte-identical to a complete raster at every appended point', () => {
  const strokes = [stroke(), stroke({ points: [[0.7, 0.1]] })];
  const cache = createStrokeRasterCache();
  for (let index = 0; index < 25; index++) {
    assert.deepEqual(cache.raster(strokes, 97, 65, 'mask'), full(strokes, 97, 65));
    strokes[1].points.push([0.1 + (index % 7) * 0.11, 0.5 + Math.sin(index) * 0.3]);
  }
});

test('committed prefixes advance once when strokes are added and invalidate on undo or truncation', () => {
  const strokes = [], cache = createStrokeRasterCache();
  for (let index = 0; index < 8; index++) {
    strokes.push(stroke({ density: 0.35 + index * 0.07, points: [[0.2 + index * 0.07, 0.5]] }));
    assert.deepEqual(cache.raster(strokes, 91, 60), full(strokes, 91, 60));
  }
  strokes.splice(3);
  assert.deepEqual(cache.raster(strokes, 91, 60), full(strokes, 91, 60));
  strokes[2].points.push([0.8, 0.1], [0.8, 0.9]);
  cache.raster(strokes, 91, 60);
  strokes[2].points.pop();
  assert.deepEqual(cache.raster(strokes, 91, 60), full(strokes, 91, 60));
  assert.deepEqual(cache.raster([], 91, 60), full([], 91, 60));
  assert.equal(cache.stats().entries, 0);
});

test('changing Flow, Density, geometry, legacy mode or any prior point invalidates stale coverage', () => {
  const modifications = [item => { item.flow = 0.07; }, item => { item.density = 0.09; },
    item => { item.size = 0.08; }, item => { item.feather = 0; },
    item => { item.buildUp = false; }, item => { item.points[1][1] = 0.1; },
    item => { item.points = [[0.8, 0.8]]; }];
  for (const mutate of modifications) for (const index of [0, 1]) {
    const strokes = [stroke(), stroke({ flow: 0.4 })], cache = createStrokeRasterCache({ legacyValues: legacy });
    const before = cache.raster(strokes, 81, 57);
    mutate(strokes[index]);
    const expected = full(strokes, 81, 57);
    assert.notDeepEqual(expected, before);
    assert.deepEqual(cache.raster(strokes, 81, 57), expected);
  }
});

test('mixed legacy and modern strokes keep exact ordering while reusing completed legacy work', () => {
  let calls = 0;
  const cache = createStrokeRasterCache({ legacyValues: (...args) => { calls++; return legacy(...args); } });
  const strokes = [stroke({ buildUp: false, flow: 0.5 }), stroke()];
  assert.deepEqual(cache.raster(strokes, 101, 73), full(strokes, 101, 73));
  assert.equal(calls, 1);
  strokes[1].points.push([0.3, 0.9]);
  assert.deepEqual(cache.raster(strokes, 101, 73), full(strokes, 101, 73));
  assert.equal(calls, 1);
  strokes.push(stroke({ buildUp: false, flow: 0.7 }));
  assert.deepEqual(cache.raster(strokes, 101, 73), full(strokes, 101, 73));
  assert.equal(calls, 2);
  strokes[2].points.push([0.4, 0.8]);
  assert.deepEqual(cache.raster(strokes, 101, 73), full(strokes, 101, 73));
  assert.equal(calls, 3);
});

test('pending PNG clips are never retained as empty results and changed clip assets invalidate', () => {
  let ready = false, calls = 0;
  const edgeValues = (item, width, height) => {
    calls++;
    return ready ? new Uint8Array(width * height).fill(item.edgeMask.data === 'first' ? 255 : 70) : null;
  };
  const strokes = [stroke({ edgeMask: { data: 'first', encoding: 'png', width: 20, height: 10 } }), stroke()];
  const cache = createStrokeRasterCache({ edgeValues });
  const pending = cache.raster(strokes, 85, 51);
  assert.equal(cache.stats().entries, 0);
  ready = true;
  const decoded = cache.raster(strokes, 85, 51);
  assert.notDeepEqual(decoded, pending);
  assert.deepEqual(decoded, full(strokes, 85, 51, edgeValues));
  const beforeReuse = calls;
  assert.deepEqual(cache.raster(strokes, 85, 51), decoded);
  assert.equal(calls, beforeReuse);
  strokes[0].edgeMask.data = 'second';
  assert.deepEqual(cache.raster(strokes, 85, 51), full(strokes, 85, 51, edgeValues));
});

test('decoded empty edge masks may be cached but caller modifications cannot corrupt reuse', () => {
  const cache = createStrokeRasterCache({ edgeValues: (_, width, height) => new Uint8Array(width * height) });
  const strokes = [stroke({ edgeMask: { data: 'empty', encoding: 'png', width: 1, height: 1 } })];
  const result = cache.raster(strokes, 71, 48);
  assert.equal(cache.stats().entries, 1);
  result.fill(255);
  assert.deepEqual(cache.raster(strokes, 71, 48), new Uint8Array(71 * 48));
});

test('entry and byte budgets evict least-recently-used entries and clear releases all retained data', () => {
  let calls = 0;
  const legacyValues = (...args) => { calls++; return legacy(...args); };
  const cache = createStrokeRasterCache({ maxEntries: 2, maxBytes: 80000, legacyValues });
  const strokes = [stroke({ buildUp: false })];
  cache.raster(strokes, 40, 30, 'first'); cache.raster(strokes, 40, 30, 'second');
  cache.raster(strokes, 40, 30, 'first'); cache.raster(strokes, 40, 30, 'third');
  assert.equal(cache.stats().entries, 2);
  assert.equal(calls, 3);
  cache.raster(strokes, 40, 30, 'second');
  assert.equal(calls, 4);
  assert.ok(cache.stats().bytes <= 80000);
  cache.raster(strokes, 100, 100, 'oversized');
  assert.equal(cache.stats().entries, 2);
  cache.clear();
  assert.deepEqual(cache.stats(), { entries: 0, bytes: 0 });
  const tiny = createStrokeRasterCache({ maxBytes: 1, legacyValues });
  assert.deepEqual(tiny.raster(strokes, 40, 30), full(strokes, 40, 30));
  assert.equal(tiny.stats().entries, 0);
});

test('dimension changes and bitmap metadata changes rebuild rather than reuse the old raster', () => {
  let calls = 0;
  const edgeValues = (item, width, height) => {
    calls++; return new Uint8Array(width * height).fill(Math.min(255, item.edgeMask.width * 10));
  };
  const cache = createStrokeRasterCache({ edgeValues });
  const strokes = [stroke({ edgeMask: { data: 'clip', encoding: 'png', width: 10, height: 5 } })];
  cache.raster(strokes, 67, 49);
  assert.deepEqual(cache.raster(strokes, 81, 55), full(strokes, 81, 55, edgeValues));
  strokes[0].edgeMask.width = 20;
  const before = calls;
  assert.deepEqual(cache.raster(strokes, 81, 55), full(strokes, 81, 55, edgeValues));
  assert.equal(calls, before + 2);
});
