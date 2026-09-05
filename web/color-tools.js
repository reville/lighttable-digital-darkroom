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
