// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  sampleCurveTable, invertCurveTable, curveSampleProblem, neutralLevel, lookupCurveTable,
  isIdentityTable, curveHandles, IDENTITY_TABLE,
} from '../web/curve-sampling.js';
import { MAX_SAMPLERS, lightness, samplerReading, addSampler } from '../web/color-readout.js';

const near = (actual, expected, tolerance = 0.01) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);
const monotone = (table) => table.every((value, index) => !index || value >= table[index - 1] - 1e-9);

test('black and white points send the sampled input to the range ends', () => {
  const black = sampleCurveTable(null, 'black', 0.2);
  near(lookupCurveTable(black, 0.2), 0);
  near(lookupCurveTable(black, 0.6), 0.5);
  near(black[255], 1, 1e-9);
  const white = sampleCurveTable(null, 'white', 0.8);
  near(lookupCurveTable(white, 0.8), 1);
  near(lookupCurveTable(white, 0.4), 0.5);
  assert.ok(monotone(black) && monotone(white));
});

test('a grey point reaches the neutral level without bending the channel into an S', () => {
  const table = sampleCurveTable(null, 'grey', 0.3, 0.25);
  near(lookupCurveTable(table, 0.3), 0.25);
  assert.ok(monotone(table));
  // A single power curve never crosses the diagonal twice.
  const above = table.slice(1, 255).map((value, index) => value > (index + 1) / 255);
  assert.ok(above.every((value) => value === above[0]));
  const existing = IDENTITY_TABLE.map((value) => value ** 0.8);
  const composed = sampleCurveTable(existing, 'grey', 0.4, 0.5);
  near(lookupCurveTable(composed, 0.4), 0.5);
});

test('identity detection and editor handles', () => {
  assert.ok(isIdentityTable(null));
  assert.ok(isIdentityTable([...IDENTITY_TABLE]));
  assert.ok(!isIdentityTable(sampleCurveTable(null, 'black', 0.1)));
  assert.deepEqual(curveHandles(null), [[0, 0], [1, 1]]);
  assert.equal(curveHandles([...IDENTITY_TABLE]).length, 9);
});

test('grey levels, problems, and curve inversion', () => {
  assert.equal(neutralLevel([0.3, 0.6, 0.9]), 0.6);
  assert.equal(curveSampleProblem('black', [0.1, 0.6, 0.2]), 'too-bright');
  assert.equal(curveSampleProblem('white', [0.9, 0.4, 0.9]), 'too-dark');
  assert.equal(curveSampleProblem('grey', [0.01, 0.5, 0.5]), 'too-dark');
  assert.equal(curveSampleProblem('grey', [0.4, 0.5, 0.6]), '');
  const squared = Array.from({ length: 256 }, (_, index) => (index / 255) ** 2);
  assert.ok(Math.abs(invertCurveTable(squared, 0.25) - 0.5) < 0.01);
  assert.equal(invertCurveTable(null, 0.4), 0.4);
});

test('sampler readings and the four-sampler limit', () => {
  assert.equal(Math.round(lightness(1, 1, 1)), 100);
  assert.equal(Math.round(lightness(0, 0, 0)), 0);
  assert.deepEqual(samplerReading(new Uint8Array([119, 119, 119, 255])), { r: 119, g: 119, b: 119, l: 50 });
  assert.equal(samplerReading(null), null);
  let list = [];
  for (let index = 0; index < MAX_SAMPLERS + 2; index++) list = addSampler(list, index / 10, 2);
  assert.equal(list.length, MAX_SAMPLERS);
  assert.deepEqual(list[0], { u: 0, v: 1 });
});
