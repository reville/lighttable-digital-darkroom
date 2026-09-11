// SPDX-License-Identifier: GPL-3.0-only
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  return value;
}

// Ordinary grade/crop are display operations. Spatial local masks additionally
// bake the ordered grade stack; those request fields participate in identity.
export function renderRequestKey(image, request) {
  return JSON.stringify(stable({
    source: [image.name, image.fileKey, image.mtime],
    w: request.w, engine: request.engine, native: request.native,
    params: request.params, optics: request.optics, heals: request.heals,
    grade: request.grade, masks: request.masks,
    viewport: request.viewport || null,
  }));
}

export function createPresentationCache(limit = 48) {
  const entries = new Map();
  return {
    get(key) {
      const value = entries.get(key);
      if (value) { entries.delete(key); entries.set(key, value); }
      return value;
    },
    findPreview(image, request) {
      // Only whole-photo surfaces with the exact source and baked recipe can
      // stand in during navigation. A viewport tile cannot cover a new view.
      const identity = renderRequestKey(image, { ...request, w: undefined, viewport: null });
      let best = null;
      for (const [key, value] of entries) {
        const { w, ...candidate } = JSON.parse(key);
        if (JSON.stringify(candidate) !== identity || !(w > 0)) continue;
        const enough = w >= request.w, bestEnough = best?.width >= request.w;
        if (!best || (enough && !bestEnough) ||
            (enough === bestEnough && (enough ? w < best.width : w > best.width))) {
          best = { key, width: w, value };
        }
      }
      if (best) { entries.delete(best.key); entries.set(best.key, best.value); }
      return best;
    },
    set(key, value) {
      if (!value || value.error || value.cancelled || value.refining) return;
      entries.delete(key); entries.set(key, value);
      while (entries.size > limit) entries.delete(entries.keys().next().value);
    },
    delete(key) { entries.delete(key); },
    clear() { entries.clear(); },
    get size() { return entries.size; },
  };
}
