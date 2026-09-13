// SPDX-License-Identifier: GPL-3.0-only
/*
 * Pure helpers for colour sampler readouts. Samplers are placed in
 * normalized photo coordinates and read the rendered, graded pixels.
 * This module has no imports so tests can load it directly.
 */
export const MAX_SAMPLERS = 4;

const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
const linear = (value) => (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);

/** CIE L* (0..100) of an sRGB-encoded colour with components in 0..1. */
export function lightness(red, green, blue) {
  const y = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
  return y > 216 / 24389 ? 116 * Math.cbrt(y) - 16 : y * (24389 / 27);
}

/** Integer RGB (0..255) and L* for one RGBA byte sample, or null. */
export function samplerReading(pixel) {
  if (!pixel || pixel.length < 3) return null;
  const [red, green, blue] = [pixel[0], pixel[1], pixel[2]].map((value) => clamp(value / 255));
  return {
    r: Math.round(red * 255), g: Math.round(green * 255), b: Math.round(blue * 255),
    l: Math.round(lightness(red, green, blue)),
  };
}

/** Add a sampler, keeping at most MAX_SAMPLERS; returns a new list. */
export function addSampler(samplers, u, v) {
  const list = Array.isArray(samplers) ? samplers.slice(0, MAX_SAMPLERS) : [];
  if (list.length >= MAX_SAMPLERS) return list;
  return [...list, { u: clamp(+u), v: clamp(+v) }];
}
