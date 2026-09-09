/* The browser's flat refinement strokes must combine exactly as edits.py does,
 * because the preview is drawn from these values and the delivered file is not.
 * Intersect takes the smaller of the two weights; it does not multiply. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function refiner(strokeValues) {
  const body = source.slice(source.indexOf('function refineMaskValues('),
                            source.indexOf('function primaryMaskComponent('));
  const deps = { brushStrokeValues: (strokes, width, height, key) =>
    strokeValues[key.split(':')[1]] || new Uint8Array(width * height) };
  return new Function(...Object.keys(deps), `${body}; return refineMaskValues;`)(
    ...Object.values(deps));
}

const bytes = values => Uint8Array.from(values);

test('intersect takes the minimum weight, matching the export rasteriser', () => {
  const refineMaskValues = refiner({ intersect: bytes([255, 128, 64, 0]) });
  const mask = { id: 'm', intersectStrokes: [{}] };
  const values = refineMaskValues(bytes([255, 255, 255, 255]), mask, 2, 2);
  assert.deepEqual(Array.from(values), [255, 128, 64, 0]);
});

test('intersect does not multiply the two weights together', () => {
  const refineMaskValues = refiner({ intersect: bytes([128, 128, 128, 128]) });
  const mask = { id: 'm', intersectStrokes: [{}] };
  const values = refineMaskValues(bytes([128, 128, 128, 128]), mask, 2, 2);
  // min(128, 128) is 128. Multiplying would give 128 * 128 / 255 = 64, which is
  // what made a feathered intersect up to a third weaker on screen than in the
  // exported file.
  assert.deepEqual(Array.from(values), [128, 128, 128, 128]);
});

test('add takes the larger weight and subtract scales it down', () => {
  const refineMaskValues = refiner({
    add: bytes([0, 255, 0, 0]), subtract: bytes([0, 0, 255, 0]) });
  const mask = { id: 'm', intersectStrokes: [] };
  const values = refineMaskValues(bytes([128, 0, 128, 32]), mask, 2, 2);
  assert.deepEqual(Array.from(values), [128, 255, 0, 32]);
});

test('a mask with no intersect strokes is left to add and subtract alone', () => {
  const refineMaskValues = refiner({ intersect: bytes([0, 0, 0, 0]) });
  const mask = { id: 'm', intersectStrokes: [] };
  const values = refineMaskValues(bytes([200, 200, 200, 200]), mask, 2, 2);
  assert.deepEqual(Array.from(values), [200, 200, 200, 200],
    'an empty intersect list must not zero the mask');
});
