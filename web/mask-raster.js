// Analytic coverage contract mirrored in mask_raster.py; no Canvas blending.
const clamp = (x, low, high) => Math.max(low, Math.min(high, x));
export function strokeCoverage(stroke, width, height) {
  const coverage = new Float32Array(width * height);
  return extendStrokeCoverage(stroke, width, height, coverage, 0);
}

function extendStrokeCoverage(stroke, width, height, coverage, startSegment) {
  const radius = Math.max(0.5, stroke.size * Math.min(width, height) / 2);
  const points = stroke.points.map(([x, y]) => [x * (width - 1), y * (height - 1)]);
  const feather = stroke.feather;
  for (let n = startSegment; n < Math.max(1, points.length - 1); n++) {
    const a = points[n], b = points[n + 1] || a;
    if (!a) break;
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const denominator = Math.max(dx * dx + dy * dy, 1e-12);
    const x0 = Math.max(0, Math.floor(Math.min(a[0], b[0]) - radius - 1));
    const x1 = Math.min(width, Math.ceil(Math.max(a[0], b[0]) + radius + 1));
    const y0 = Math.max(0, Math.floor(Math.min(a[1], b[1]) - radius - 1));
    const y1 = Math.min(height, Math.ceil(Math.max(a[1], b[1]) + radius + 1));
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
      const t = clamp(((x - a[0]) * dx + (y - a[1]) * dy) / denominator, 0, 1);
      const distance = Math.hypot(x - a[0] - t * dx, y - a[1] - t * dy) / radius;
      const fade = feather > 0 ? clamp((distance - (1 - feather)) / feather, 0, 1) : 0;
      const alpha = feather > 0 ? 1 - fade * fade * (3 - 2 * fade) : +(distance <= 1);
      const i = y * width + x;
      coverage[i] = Math.max(coverage[i], alpha);
    }
  }
  return coverage;
}

export function accumulateStroke(combined, coverage, stroke, edge = null) {
  for (let i = 0; i < combined.length; i++) {
    const alpha = coverage[i] * stroke.flow * (edge ? edge[i] / 255 : 1);
    combined[i] = Math.max(combined[i], Math.min(stroke.density ?? 1, combined[i] + alpha));
  }
}

const strokeSettings = stroke => ({
  buildUp: !!stroke.buildUp, size: stroke.size, feather: stroke.feather,
  flow: stroke.flow, density: stroke.density,
  edgeMask: stroke.edgeMask ? { data: stroke.edgeMask.data, encoding: stroke.edgeMask.encoding,
    width: stroke.edgeMask.width, height: stroke.edgeMask.height } : null,
});
function sameSettings(stroke, snapshot) {
  if (['size', 'feather', 'flow', 'density'].some(key => stroke[key] !== snapshot[key])
    || !!stroke.buildUp !== snapshot.buildUp || !!stroke.edgeMask !== !!snapshot.edgeMask) return false;
  return !stroke.edgeMask || ['data', 'encoding', 'width', 'height']
    .every(key => stroke.edgeMask[key] === snapshot.edgeMask[key]);
}
const samePointPrefix = (stroke, snapshot) => stroke.points.length >= snapshot.points.length
  && snapshot.points.every(([x, y], index) => stroke.points[index][0] === x && stroke.points[index][1] === y);
const sameStroke = (stroke, snapshot) => sameSettings(stroke, snapshot)
  && stroke.points.length === snapshot.points.length && samePointPrefix(stroke, snapshot);

/**
 * Cache a committed aggregate plus one editable tail stroke per caller key.
 * edgeValues(stroke, width, height) returns decoded 8-bit coverage, or null /
 * undefined while loading. Pending edge masks produce no coverage and are never
 * cached. legacyValues supplies the existing raster for one legacy stroke.
 * Returned bytes belong to the caller and may safely be modified by refinements.
 */
export function createStrokeRasterCache({ legacyValues, edgeValues,
  maxEntries = 6, maxBytes = 24 * 1024 * 1024 } = {}) {
  const entries = new Map();
  let retainedBytes = 0;
  function remove(key) {
    const entry = entries.get(key);
    if (entry) retainedBytes -= entry.bytes;
    entries.delete(key);
  }
  function touch(key, entry) { entries.delete(key); entries.set(key, entry); }
  function raster(strokes, width, height, key = '') {
    const count = strokes.length, pixels = width * height;
    if (!count) { remove(key); return new Uint8Array(pixels); }
    const cached = entries.get(key);
    const old = cached?.width === width && cached?.height === height ? cached : null;
    if (old && count === old.snapshots.length
      && strokes.every((stroke, index) => sameStroke(stroke, old.snapshots[index]))) {
      touch(key, old); return old.output.slice();
    }
    let ready = true;
    const coverage = stroke => stroke.buildUp
      ? strokeCoverage(stroke, width, height) : legacyValues(stroke, width, height);
    const apply = (combined, values, stroke) => {
      if (!stroke.buildUp) {
        for (let i = 0; i < pixels; i++) combined[i] = Math.max(combined[i], values[i] / 255);
        return;
      }
      const edge = stroke.edgeMask ? edgeValues?.(stroke, width, height) : null;
      if (stroke.edgeMask && (!edge || edge.length !== pixels)) { ready = false; return; }
      accumulateStroke(combined, values, stroke, edge);
    };
    let prefix, tail;
    if (old && count === old.snapshots.length
      && strokes.slice(0, -1).every((stroke, index) => sameStroke(stroke, old.snapshots[index]))) {
      prefix = old.prefix;
      const last = strokes.at(-1), before = old.snapshots.at(-1);
      tail = last.buildUp && sameSettings(last, before) && samePointPrefix(last, before)
        ? extendStrokeCoverage(last, width, height, old.tail.slice(), Math.max(0, before.points.length - 1))
        : coverage(last);
    } else if (old && count > old.snapshots.length
      && old.snapshots.every((snapshot, index) => sameStroke(strokes[index], snapshot))) {
      prefix = old.prefix.slice();
      apply(prefix, old.tail, strokes[old.snapshots.length - 1]);
      for (let index = old.snapshots.length; index < count - 1; index++) {
        apply(prefix, coverage(strokes[index]), strokes[index]);
      }
      tail = coverage(strokes.at(-1));
    } else {
      prefix = new Float32Array(pixels);
      for (const stroke of strokes.slice(0, -1)) apply(prefix, coverage(stroke), stroke);
      tail = coverage(strokes.at(-1));
    }
    const combined = prefix.slice();
    apply(combined, tail, strokes.at(-1));
    const output = new Uint8Array(pixels);
    for (let i = 0; i < pixels; i++) output[i] = Math.round(combined[i] * 255);
    const snapshots = strokes.map(stroke => ({ ...strokeSettings(stroke),
      points: stroke.points.map(point => [...point]) }));
    const bytes = prefix.byteLength + tail.byteLength + output.byteLength
      + snapshots.reduce((total, stroke) => total + 128 + stroke.points.length * 16
        + String(stroke.edgeMask?.data || '').length * 2, 0);
    remove(key);
    if (ready && maxEntries > 0 && bytes <= maxBytes) {
      const entry = { width, height, prefix, tail, output, snapshots, bytes };
      entries.set(key, entry); retainedBytes += bytes;
      while (entries.size > maxEntries || retainedBytes > maxBytes) remove(entries.keys().next().value);
      return output.slice();
    }
    return output;
  }
  return { raster, clear: () => { entries.clear(); retainedBytes = 0; },
    stats: () => ({ entries: entries.size, bytes: retainedBytes }) };
}

// Freeze color similarity at paint time so future grading cannot move an edge.
export function autoMaskValues(pixels, width, height, point, tolerance = 0.18) {
  const x = clamp(Math.round(point[0] * (width - 1)), 0, width - 1);
  const y = clamp(Math.round(point[1] * (height - 1)), 0, height - 1);
  const seed = (y * width + x) * 4;
  const values = new Uint8Array(width * height);
  for (let i = 0; i < values.length; i++) {
    const p = i * 4;
    const distance = Math.hypot(pixels[p] - pixels[seed], pixels[p + 1] - pixels[seed + 1],
      pixels[p + 2] - pixels[seed + 2]) / (255 * Math.sqrt(3));
    const fade = clamp((distance - tolerance * 0.5) / Math.max(tolerance * 0.5, 1e-6), 0, 1);
    values[i] = Math.round((1 - fade * fade * (3 - 2 * fade)) * 255);
  }
  return values;
}
