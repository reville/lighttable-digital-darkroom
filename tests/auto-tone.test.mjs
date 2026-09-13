// SPDX-License-Identifier: GPL-3.0-only
// Auto tone reads the luma histogram and sets Exposure, Blacks and Whites.
// Under the corrected Whites sign a bright end that stops short of white must
// receive a positive Whites, which the shared endpoint formula (grade.py,
// web/gl.js, the WGSL and Metal shaders) turns into a brighter top of the
// range; a dark end that stops short of black must receive a negative Blacks.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

// The endpoint stage every renderer applies after Highlights and Shadows.
function endpoints(value, {whites, blacks}) {
  const w = 1 - whites * 0.35, b = blacks * -0.25;
  return clamp((value - b) / Math.max(w - b, 1e-4), 0, 1);
}

function autoTone(luma) {
  const px = new Uint8ClampedArray(luma.length * 4);
  luma.forEach((value, index) => { px[index * 4] = px[index * 4 + 1] = px[index * 4 + 2] = value; px[index * 4 + 3] = 255; });
  const S = {grade: {exposure: 0, blacks: 0, whites: 0}, gl: {sample: () => ({px})}};
  const calls = [];
  const button = {};
  const context = {S, clamp, $: () => button, tr: text => text,
    photoReadyForEditing: () => true, refreshWebGLSamplingSurface: () => {},
    toast: text => calls.push(text), pushUndo: () => calls.push('undo'),
    syncGrade: () => calls.push('sync'), drawGrade: () => calls.push('draw'), saveState: () => calls.push('save')};
  vm.runInNewContext(between("$('autoBtn').onclick", '/* ---------------------------------------------------------- WB dropper */'), context);
  button.onclick({stopPropagation() {}});
  assert.deepEqual(calls, ['undo', 'sync', 'draw', 'save', 'Auto tone applied']);
  return S.grade;
}

const spread = (low, high, count = 4096) => Array.from({length: count}, (_, i) => low + Math.round((high - low) * i / (count - 1)));

test('a dark, flat image gets a positive Whites and a negative Blacks that stretch it to both ends', () => {
  const luma = spread(30, 140);
  const grade = autoTone(luma);
  assert.ok(grade.whites > 0, `Whites should be positive, got ${grade.whites}`);
  assert.ok(grade.blacks < 0, `Blacks should be negative, got ${grade.blacks}`);
  assert.ok(grade.exposure > 0, 'a dark image is lifted');
  assert.ok(endpoints(140 / 255, grade) > 140 / 255, 'the brightest tones move toward white');
  assert.ok(endpoints(30 / 255, grade) < 30 / 255, 'the darkest tones move toward black');
  assert.ok(endpoints(140 / 255, grade) <= 1 && endpoints(30 / 255, grade) >= 0);
});

test('a bright image that already reaches white leaves Whites alone and deepens the shadows', () => {
  const grade = autoTone(spread(90, 255));
  assert.ok(Math.abs(grade.whites) < 0.01, `Whites stays near zero, got ${grade.whites}`);
  assert.ok(grade.blacks < 0);
  assert.ok(grade.exposure < 0, 'a bright image is pulled down');
  assert.equal(endpoints(1, grade), 1);
  assert.ok(endpoints(90 / 255, grade) < 90 / 255);
});

test('a full-range image is left at the endpoints', () => {
  // The 0.5% percentile cut lands one code inside the extremes of a ramp,
  // so the endpoint terms round to nothing rather than exactly zero.
  const grade = autoTone(spread(0, 255));
  assert.ok(Math.abs(grade.whites) < 0.01, `Whites stays near zero, got ${grade.whites}`);
  assert.ok(Math.abs(grade.blacks) < 0.01, `Blacks stays near zero, got ${grade.blacks}`);
});

test('the Whites term is bounded so Auto never overshoots the slider range', () => {
  const grade = autoTone(spread(5, 12));
  assert.ok(grade.whites <= 1 && grade.whites >= -0.4);
  assert.ok(grade.blacks >= -1 && grade.blacks <= 0.4);
  assert.ok(endpoints(12 / 255, grade) > 12 / 255);
});
