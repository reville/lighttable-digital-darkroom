// SPDX-License-Identifier: GPL-3.0-only
/*
 * Pure helpers for the tone-curve eyedroppers. Every value is normalized to
 * 0..1 and curves are the 256-entry tables that travel to every renderer.
 * A black point maps the sampled input to 0, a white point maps it to 1, and
 * a grey point maps each channel's input to one shared neutral level. The
 * channel's existing curve keeps its shape: the eyedropper remaps the input
 * before that curve, the way a levels adjustment would.
 * This module has no imports so tests can load it directly.
 */
const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
export const IDENTITY_TABLE = Object.freeze(Array.from({ length: 256 }, (_, index) => index / 255));
export const HANDLE_INDICES = Object.freeze([0, 32, 64, 96, 128, 160, 192, 224, 255]);

const validTable = (table) => Array.isArray(table) && table.length === 256;

/** Linear lookup at a fractional input, matching the renderers' interpolation. */
export function lookupCurveTable(table, value) {
  const source = validTable(table) ? table : IDENTITY_TABLE;
  const position = clamp(value) * 255;
  const low = Math.floor(position);
  const high = Math.min(255, low + 1);
  return source[low] + (source[high] - source[low]) * (position - low);
}

/** The input whose table output is closest to `value` (identity without a table). */
export function invertCurveTable(table, value) {
  if (!validTable(table)) return clamp(value);
  let best = 0;
  let distance = Infinity;
  for (let index = 0; index < 256; index++) {
    const next = Math.abs(table[index] - value);
    if (next < distance) { distance = next; best = index; }
  }
  return best / 255;
}

/** Why a sample cannot set this point, or '' when it can. */
export function curveSampleProblem(kind, rgb) {
  const values = rgb.map((value) => clamp(value));
  if (kind === 'black' && Math.max(...values) >= 0.5) return 'too-bright';
  if (kind === 'white' && Math.min(...values) <= 0.5) return 'too-dark';
  if (kind === 'grey') {
    if (Math.min(...values) < 0.03) return 'too-dark';
    if (Math.max(...values) > 0.97) return 'too-bright';
  }
  return '';
}

/** The shared output level a grey sample should reach. */
export function neutralLevel(rgb) {
  return clamp((rgb[0] + rgb[1] + rgb[2]) / 3);
}

/**
 * Return a new 256-entry table for one channel after sampling `input`.
 * `kind` is 'black', 'white', or 'grey'; `output` is the grey target.
 */
export function sampleCurveTable(table, kind, input, output = 0.5) {
  const x = clamp(input, 1 / 255, 254 / 255);
  let remap = (value) => value;
  if (kind === 'black') remap = (value) => clamp((value - x) / (1 - x));
  else if (kind === 'white') remap = (value) => clamp(value / x);
  else if (kind === 'grey') {
    const target = clamp(invertCurveTable(table, clamp(output)), 1 / 255, 254 / 255);
    const exponent = clamp(Math.log(target) / Math.log(x), 0.2, 5);
    remap = (value) => value ** exponent;
  }
  return IDENTITY_TABLE.map((value) => clamp(lookupCurveTable(table, remap(value))));
}

export function isIdentityTable(table) {
  return !validTable(table) || table.every((value, index) => Math.abs(value - index / 255) < 0.002);
}

/** Representative editor handles for a table, as the curve editor rebuilds them. */
export function curveHandles(table) {
  return validTable(table) ? HANDLE_INDICES.map((index) => [index / 255, table[index]]) : [[0, 0], [1, 1]];
}
