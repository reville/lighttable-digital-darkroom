// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import {groupSimilarPhotos, cullSuggestion} from '../web/cull-similarity.js';

const capture = image => Date.parse(image.date);
const layout = Array.from({length: 48}, (_, i) => (i % 2 ? 180 : 60).toString(16)).join('');
function photo(name, seconds = 0, options = {}) {
  return {name, date: new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString(),
    captureTimeKnown: true, ai: {cull: {criteria: {}, metrics: {focus: {frame: 0.1}},
      similarity: {version: 1, hash: '5555555555555555', layout, contrast: 0.15, aspect: 1.5}}}, ...options};
}
const groups = images => groupSimilarPhotos(images, capture);
const verdict = (image, key, value) => {image.ai.cull.criteria[key] = {verdict: value};};

test('groups near captures with matching scenes and preserves original objects', () => {
  const a = photo('a'), b = photo('b', 10), c = photo('c', 12), d = photo('d', 16);
  c.ai.cull.similarity.hash = d.ai.cull.similarity.hash = 'aaaaaaaaaaaaaaaa';
  assert.deepEqual(groups([a, c, b, d]), [[a, b], [c, d]]);
  assert.equal(groups([a, b])[0][0], a);
});

test('requires reliable capture metadata and excludes mtime, videos, virtual copies and unscored images', () => {
  const a = photo('a');
  for (const overrides of [{captureTimeKnown: false}, {captureTimeKnown: undefined},
    {date: undefined, mtime: 1767225600}, {date: '2026-01-01'}, {date: 'invalid'},
    {kind: 'video'}, {virtual: true}, {ai: {}}, {ai: {cull: {similarity: a.ai.cull.similarity}}}]) {
    assert.deepEqual(groups([a, photo('b', 0, overrides)]), []);
  }
});

test('30-second window is anchored, inclusive, and cannot chain to distant captures', () => {
  const a = photo('a'), b = photo('b', 30), c = photo('c', 31), d = photo('d', 60);
  assert.deepEqual(groups([a, b, c, d]), [[a, b], [c, d]]);
});

test('similarity cannot transitively chain different scenes', () => {
  const a = photo('a'), b = photo('b', 1), c = photo('c', 2);
  b.ai.cull.similarity.hash = 'aa55555555555555';
  c.ai.cull.similarity.hash = 'aaaa555555555555';
  assert.deepEqual(groups([a, b, c]), [[a, b]]);
});

test('ranking uses fewer known issues, more actual passes, then focus; unknown is never a pass', () => {
  const unknown = photo('unknown'), pass = photo('pass'), bad = photo('bad'), focused = photo('focused');
  verdict(unknown, 'eyesOpen', 'unknown');
  verdict(pass, 'eyesOpen', 'yes'); verdict(focused, 'eyesOpen', 'yes');
  focused.ai.cull.metrics.focus.frame = 0.3;
  verdict(bad, 'misfire', 'yes'); verdict(bad, 'eyesOpen', 'yes');
  assert.deepEqual(groups([unknown, pass, bad, focused]), [[focused, pass, unknown, bad]]);
  const negative = photo('negative'); verdict(negative, 'eyesOpen', 'no');
  assert.deepEqual(groups([negative, unknown]), [[negative, unknown]]);
});

test('equal ranks retain input order, regardless of capture sorting', () => {
  const a = photo('a', 2), b = photo('b', 1);
  assert.deepEqual(groups([a, b]), [[a, b]]);
});

test('rejects flat, degenerate, malformed, different-color and different-aspect signatures', () => {
  const a = photo('a');
  for (const patch of [{contrast: 0}, {hash: '0000000000000000'}, {hash: 'ffffffffffffffff'},
    {hash: 'bogus'}, {version: 2}, {layout: ''}, {layout: 'ff'.repeat(48)}, {aspect: 0.66}]) {
    const b = photo('b'); Object.assign(b.ai.cull.similarity, patch);
    assert.deepEqual(groups([a, b]), []);
  }
});

test('dense bursts are bounded and photos occur in at most one group', () => {
  const images = Array.from({length: 140}, (_, i) => photo(String(i)));
  const result = groups(images);
  assert.deepEqual(result.map(group => group.length), [...Array(8).fill(16), 12]);
  assert.equal(new Set(result.flat()).size, 140);
});


test('recommendations require unique positive evidence with no known reject issue', () => {
  const a = photo('a'), b = photo('b');
  assert.equal(cullSuggestion(groups([a, b])[0]), null);
  verdict(a, 'eyesOpen', 'yes');
  assert.equal(cullSuggestion(groups([a, b])[0]), a);
  verdict(b, 'eyesOpen', 'yes');
  assert.equal(cullSuggestion(groups([a, b])[0]), null);
  verdict(a, 'misfire', 'yes'); verdict(b, 'misfire', 'yes');
  assert.equal(cullSuggestion(groups([a, b])[0]), null);
  assert.equal(cullSuggestion([]), null);
});


test('Survey-sized boundaries preserve overflow candidates except a final singleton', () => {
  for (const count of [15, 16, 17, 18, 31, 32, 33, 34, 65, 66]) {
    const images = Array.from({length: count}, (_, i) => photo(String(i)));
    const result = groups(images);
    assert.ok(result.every(group => group.length >= 2 && group.length <= 16));
    const expectedCount = count % 16 === 1 ? count - 1 : count;
    assert.deepEqual(result.flat(), images.slice(0, expectedCount));
    assert.equal(new Set(result.flat()).size, expectedCount);
  }
});
