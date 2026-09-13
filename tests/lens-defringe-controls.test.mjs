// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { normalizeOptics, defringeActive, OPTICS_DEFAULTS, DEFRINGE_KEYS } from '../web/editor-panels.js';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const section = (id) => {
  const start = html.indexOf(`id="${id}"`);
  assert.ok(start > 0, `${id} is in the markup`);
  const open = html.lastIndexOf('<', start);
  const tag = html.slice(open + 1, html.indexOf(' ', open));
  let depth = 0, cursor = open;
  const token = new RegExp(`<(/?)${tag}\\b`, 'g');
  token.lastIndex = open;
  for (let found; (found = token.exec(html));) {
    depth += found[1] ? -1 : 1;
    if (depth === 0) { cursor = html.indexOf('>', found.index); break; }
  }
  return html.slice(open, cursor + 1);
};

test('defringe and chromatic switches normalize with bounded travel', () => {
  const value = normalizeOptics({ defringePurple: 4, defringeGreen: -2, defringePurpleHueStart: 720,
    defringeGreenHueEnd: 'x', profileChromatic: false });
  assert.equal(value.defringePurple, 1);
  assert.equal(value.defringeGreen, 0);
  assert.equal(value.defringePurpleHueStart, 360);
  assert.equal(value.defringeGreenHueEnd, OPTICS_DEFAULTS.defringeGreenHueEnd);
  assert.equal(value.profileChromatic, false);
  assert.equal(normalizeOptics({}).profileChromatic, true);
  assert.equal(defringeActive(normalizeOptics({})), false);
  assert.equal(defringeActive({ defringeGreen: 0.2 }), true);
  assert.deepEqual(DEFRINGE_KEYS, ['defringePurple', 'defringeGreen']);
});

test('the lens panel carries the chromatic switch and defringe sliders', () => {
  const lens = section('lensPane');
  assert.match(lens, /id="lensProfileChromatic"/);
  for (const key of Object.keys(OPTICS_DEFAULTS).filter((name) => name.startsWith('defringe'))) {
    assert.match(lens, new RegExp(`data-optics="${key}"`), key);
  }
  assert.match(lens, /id="lensDatabaseNote"/);
  assert.doesNotMatch(html, /bundled camera and lens database/);
});

test('capture white balance lives in Color, outside the Film profile controls', () => {
  const colour = section('colorSection');
  const film = section('filmProfileControls');
  assert.match(colour, /id="captureWbBlock"/);
  for (const id of ['wb_mode', 'wb_temperature', 'wb_tint', 'wbCustom']) {
    assert.match(colour, new RegExp(`id="${id}"`), id);
    assert.doesNotMatch(film, new RegExp(`id="${id}"`), id);
    assert.equal(html.split(`id="${id}"`).length, 2, `${id} appears once`);
  }
  assert.match(html, /id="newPhotoLensProfile"/);
});
