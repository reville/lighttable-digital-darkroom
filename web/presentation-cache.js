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
