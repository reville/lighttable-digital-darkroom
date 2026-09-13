// SPDX-License-Identifier: GPL-3.0-only
/* Feather narrows or widens a linear gradient's transition band, centred on
 * the midpoint between its two handles. canvasGeometryValues rasterises the
 * live preview; edits._raster_component rasterises the delivered file. Both
 * must produce the same weights for the same geometry, or a user sees one
 * photograph on screen and receives another.
 *
 * The expected weights below are the same ones asserted independently in
 * tests/test_mask_preview_parity.py's LinearGradientFeatherTests, against
 * edits.raster_mask, for the identical start/end/feather geometry. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function loadCanvasGeometryValues() {
  const body = source.slice(
    source.indexOf('function canvasGeometryValues('),
    source.indexOf('function componentGeometryValues('));
  const deps = {
    LINEAR_MIN_SPAN: 10 ** -3.5,
    clamp: (value, lo, hi) => Math.min(hi, Math.max(lo, value)),
    smoothStep: (edge0, edge1, value) => {
      if (edge1 <= edge0) return value >= edge1 ? 1 : 0;
      const t = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0)));
      return t * t * (3 - 2 * t);
    },
  };
  return new Function(...Object.keys(deps), `${body}; return canvasGeometryValues;`)(
    ...Object.values(deps));
}

// start=(0.2, 0.5), end=(0.6, 0.5) on a 101-wide raster puts the handle-line
// projection at exactly t=0, 0.25, 0.5, 0.75, 1 for x=20, 30, 40, 50, 60.
const XS = [20, 30, 40, 50, 60];
const WIDTH = 101, HEIGHT = 3, ROW = 1;

function weightsAt(feather) {
  const canvasGeometryValues = loadCanvasGeometryValues();
  const component = { type: 'linear', start: [0.2, 0.5], end: [0.6, 0.5], feather };
  const values = canvasGeometryValues(component, WIDTH, HEIGHT);
  return XS.map((x) => values[ROW * WIDTH + x] / 255);
}

test('default feather is a smoothstep across the whole span', () => {
  const weights = weightsAt(1);
  [0, 0.15625, 0.5, 0.84375, 1].forEach((expected, i) => {
    assert.ok(Math.abs(weights[i] - expected) < 1 / 255 + 1e-6,
      `x=${XS[i]}: ${weights[i]} != ${expected}`);
  });
});

test('zero feather is a hard edge at the midpoint', () => {
  const weights = weightsAt(0);
  assert.deepEqual(weights, [0, 0, 1, 1, 1]);
});

test('half feather narrows the band symmetrically', () => {
  const weights = weightsAt(0.5);
  [0, 0, 0.5, 1, 1].forEach((expected, i) => {
    assert.ok(Math.abs(weights[i] - expected) < 1 / 255 + 1e-6,
      `x=${XS[i]}: ${weights[i]} != ${expected}`);
  });
});
