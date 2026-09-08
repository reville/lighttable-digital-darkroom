import assert from 'node:assert/strict';
import { test } from 'node:test';
import { clampComparePosition, compareViewGeometry, comparePositionAtViewCenter } from '../web/compare-view.js';

const viewport = { left: 200, top: 100, width: 800, height: 600 };

test('Fit chrome follows the photo, including its letterboxing', () => {
  const image = { left: 200, top: 150, width: 800, height: 500 };
  assert.deepEqual(compareViewGeometry(image, viewport, 0.5), {
    left: 0, top: 50, width: 800, height: 500, dividerX: 400,
  });
});

test('zoomed and vertically panned chrome stays within the visible window', () => {
  const image = { left: -700, top: -900, width: 2400, height: 1800 };
  assert.deepEqual(compareViewGeometry(image, viewport, 0.5), {
    left: 0, top: 0, width: 800, height: 600, dividerX: 300,
  });
  const position = comparePositionAtViewCenter(image, viewport);
  assert.ok(Math.abs(position - 13 / 24) < 1e-12);
  assert.equal(compareViewGeometry(image, viewport, position).dividerX, 400);
});

test('snap recovers an offscreen divider without recentering the image', () => {
  const image = { left: -700, top: -900, width: 2400, height: 1800 };
  const before = { ...image };
  assert.equal(compareViewGeometry(image, viewport, 0.9).dividerX, 1260);
  const snapped = compareViewGeometry(image, viewport, comparePositionAtViewCenter(image, viewport));
  assert.equal(snapped.dividerX, viewport.width / 2);
  assert.deepEqual(image, before);
});

test('snap reaches the visible center at both edges of the maximum 32x zoom', () => {
  for (const left of [viewport.left, viewport.left - viewport.width * 31]) {
    const image = { left, top: -500, width: viewport.width * 32, height: 24000 };
    const position = comparePositionAtViewCenter(image, viewport);
    assert.ok(position < 0.02 || position > 0.98);
    assert.equal(compareViewGeometry(image, viewport, position).dividerX, 400);
  }
});

test('portrait and resized viewports use the current visible geometry', () => {
  const image = { left: 350, top: -300, width: 500, height: 1200 };
  const narrow = { left: 300, top: 80, width: 500, height: 500 };
  const result = compareViewGeometry(image, narrow, comparePositionAtViewCenter(image, narrow));
  assert.deepEqual(result, { left: 50, top: 0, width: 450, height: 500, dividerX: 200 });
  assert.equal(result.left + result.dividerX, narrow.width / 2);
});

test('empty or nonintersecting views have no divider geometry', () => {
  assert.equal(compareViewGeometry({ left: 0, top: 0, width: 0, height: 0 }, viewport, 0.5), null);
  assert.equal(compareViewGeometry({ left: 1200, top: 0, width: 100, height: 100 }, viewport, 0.5), null);
  assert.equal(comparePositionAtViewCenter({ width: 0 }, viewport), null);
});

test('drag limits include photo edges without treating zero as the midpoint', () => {
  assert.equal(clampComparePosition(0), 0);
  assert.equal(clampComparePosition(-1), 0);
  assert.equal(clampComparePosition(2), 1);
  assert.equal(clampComparePosition(NaN), 0.5);
});
