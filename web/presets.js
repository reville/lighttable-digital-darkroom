import { cloneValue } from './state.js';
import { GRADE_DEFAULTS } from './gl.js';
import { normalizeMasks, normalizeHeals, normalizeOptics, OPTICS_DEFAULTS } from './editor-panels.js';

export function bytesToBase64(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}

export function hasApplicablePresetSettings(
  preset, normalizeOptics, opticsDefaults,
) {
  if (!preset) return false;
  const grade = preset.grade || {};
  const included = preset.includedGrade || Object.keys(grade);
  const hasGrade = included.some((key) =>
    Object.prototype.hasOwnProperty.call(grade, key));
  const hasFilm = preset.includeFilm && Object.keys(preset.params || {}).length > 0;
  const hasLocal = (preset.masks || []).length > 0 || (preset.heals || []).length > 0;
  const optics = normalizeOptics(preset.optics);
  const hasOptics = Object.keys(opticsDefaults).some((key) =>
    optics[key] !== opticsDefaults[key]);
  return hasGrade || hasFilm || hasLocal || hasOptics;
}

/** Compose both previews and committed edits without mutating either input. */
export function composePresetState(state, preset, {
  replace = false, filmOff = false,
  normalizeFilmParams = (params) => params,
  mergeFilmParams = (base, overlay) => ({ ...base, ...overlay }),
  createId = (prefix) => `${prefix}-${crypto.randomUUID()}`,
} = {}) {
  const next = cloneValue(state);
  const included = preset.includedGrade || Object.keys(preset.grade || {});
  next.grade = cloneValue(replace ? GRADE_DEFAULTS : state.grade || GRADE_DEFAULTS);
  for (const key of included) {
    if (!Object.prototype.hasOwnProperty.call(preset.grade || {}, key)) continue;
    if (key === 'hsl' && !replace) {
      next.grade.hsl = { ...(next.grade.hsl || {}) };
      for (const [band, adjustment] of Object.entries(preset.grade.hsl || {})) {
        next.grade.hsl[band] = { ...(next.grade.hsl[band] || {}), ...cloneValue(adjustment) };
      }
    } else next.grade[key] = cloneValue(preset.grade[key]);
  }
  if (replace) {
    next.masks = normalizeMasks(cloneValue(preset.masks));
    next.heals = normalizeHeals(cloneValue(preset.heals));
    next.optics = normalizeOptics(cloneValue(preset.optics));
  } else {
    const masks = (preset.masks || []).map((mask) => ({ ...cloneValue(mask), id: createId('mask') }));
    const heals = (preset.heals || []).map((spot) => ({ ...cloneValue(spot), id: createId('heal') }));
    next.masks = normalizeMasks([...(next.masks || []), ...masks]);
    next.heals = normalizeHeals([...(next.heals || []), ...heals]);
    next.optics = normalizeOptics(next.optics);
    const optics = normalizeOptics(preset.optics);
    for (const key of Object.keys(OPTICS_DEFAULTS)) {
      if (optics[key] !== OPTICS_DEFAULTS[key]) next.optics[key] = optics[key];
    }
  }
  if (preset.includeFilm && preset.params) {
    next.params = replace ? normalizeFilmParams(cloneValue(preset.params))
      : mergeFilmParams(next.params || {}, cloneValue(preset.params));
  }
  if (filmOff) next.params = { ...(next.params || {}), profile_enabled: false };
  return next;
}
