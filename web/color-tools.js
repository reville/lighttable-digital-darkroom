// SPDX-License-Identifier: GPL-3.0-only
export function monotoneLUT(points) {
  const sorted = [...points].sort((a, b) => a[0] - b[0]);
  const output = new Array(256);
  for (let index = 0; index < 256; index++) {
    const x = index / 255;
    let low = sorted[0], high = sorted.at(-1);
    for (let segment = 0; segment < sorted.length - 1; segment++) {
      if (x >= sorted[segment][0] && x <= sorted[segment + 1][0]) {
        low = sorted[segment]; high = sorted[segment + 1]; break;
      }
    }
    if (x <= sorted[0][0]) { output[index] = sorted[0][1]; continue; }
    if (x >= sorted.at(-1)[0]) { output[index] = sorted.at(-1)[1]; continue; }
    const t = (x - low[0]) / Math.max(high[0] - low[0], 1e-6);
    const smooth = t * t * (3 - 2 * t);
    output[index] = Math.max(0, Math.min(1, low[1] + (high[1] - low[1]) * smooth));
  }
  return output;
}

export function isIdentityPoints(points) {
  return points.length === 2 && points[0][0] === 0 && points[0][1] === 0 &&
    points[1][0] === 1 && points[1][1] === 1;
}

export function evalParametricLUT(params) {
  const { highlights = 0, lights = 0, darks = 0, shadows = 0,
          splitSD = 0.25, splitDL = 0.50, splitLH = 0.75 } = params || {};
  if (highlights === 0 && lights === 0 && darks === 0 && shadows === 0) return null;
  const nodes = [
    { x: 0, delta: 0 },
    { x: splitSD * 0.5, delta: (shadows / 100) * 0.25 },
    { x: (splitSD + splitDL) * 0.5, delta: (darks / 100) * 0.25 },
    { x: (splitDL + splitLH) * 0.5, delta: (lights / 100) * 0.25 },
    { x: (splitLH + 1.0) * 0.5, delta: (highlights / 100) * 0.25 },
    { x: 1.0, delta: 0 },
  ];
  const output = new Array(256);
  for (let i = 0; i < 256; i++) {
    const x = i / 255;
    let seg = 0;
    for (let s = 0; s < nodes.length - 1; s++) {
      if (x >= nodes[s].x && x <= nodes[s + 1].x) { seg = s; break; }
    }
    const n0 = nodes[seg], n1 = nodes[seg + 1];
    const t = (x - n0.x) / Math.max(n1.x - n0.x, 1e-6);
    const smooth = t * t * (3 - 2 * t);
    const delta = n0.delta + (n1.delta - n0.delta) * smooth;
    output[i] = Math.max(0, Math.min(1, x + delta));
  }
  return output;
}

export function rgbHue(red, green, blue) {
  const maximum = Math.max(red, green, blue);
  const minimum = Math.min(red, green, blue);
  const delta = maximum - minimum;
  if (delta < 1e-6) return 0;
  let hue;
  if (maximum === red) hue = ((green - blue) / delta) % 6;
  else if (maximum === green) hue = (blue - red) / delta + 2;
  else hue = (red - green) / delta + 4;
  return (hue * 60 + 360) % 360;
}

export function emptyColorGrading(tones) {
  return Object.fromEntries([
    ...tones.map((tone) => [tone, { hue: 0, saturation: 0, luminance: 0 }]),
    ['balance', 0], ['blending', 0.5],
  ]);
}
