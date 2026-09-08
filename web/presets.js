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

export const LOOK_GRADE_KEYS = new Set([
  'contrast', 'highlights', 'shadows', 'whites', 'blacks', 'vibrance', 'saturation',
  'texture', 'clarity', 'dehaze', 'vignette', 'curveL', 'curveR', 'curveG', 'curveB',
  'hsl', 'pointColor', 'colorGrading',
]);
export const CREATIVE_FILM_KEYS = new Set([
  'stock', 'paper', 'workflow_mode', 'paper_locked', 'output_recipe',
  'development_time', 'print_development_time', 'exposure_ev', 'print_exposure',
  'gamma', 'auto_exposure', 'scan_sharpen', 'couplers_on', 'couplers_amount',
  'halation_on', 'halation_amount', 'grain_on', 'grain_amount', 'glare_on',
  'glare_amount', 'camera_diffusion_family', 'camera_diffusion_strength',
  'print_preflash', 'print_y_filter_shift', 'print_m_filter_shift',
  'scan_softness', 'scan_sharpness',
]);

export function hasApplicablePresetSettings(
  preset, normalizeOptics, opticsDefaults,
) {
  if (!preset) return false;
  const grade = preset.grade || {};
  const included = preset.includedGrade || Object.keys(grade);
  const hasGrade = included.some((key) =>
    Object.prototype.hasOwnProperty.call(grade, key) &&
    (preset.scope !== 'look' || LOOK_GRADE_KEYS.has(key)));
  const hasFilm = preset.filmMode === 'on' || preset.filmMode === 'off' ||
    (preset.includeFilm && Object.keys(preset.params || {}).length > 0);
  if (preset.scope === 'look') return hasGrade || hasFilm;
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
  const look = preset.scope === 'look';
  const included = preset.includedGrade || Object.keys(preset.grade || {});
  next.grade = cloneValue(replace && !look ? GRADE_DEFAULTS : state.grade || GRADE_DEFAULTS);
  for (const key of included) {
    if (!Object.prototype.hasOwnProperty.call(preset.grade || {}, key) ||
        (look && !LOOK_GRADE_KEYS.has(key))) continue;
    if (key === 'hsl' && !replace) {
      next.grade.hsl = { ...(next.grade.hsl || {}) };
      for (const [band, adjustment] of Object.entries(preset.grade.hsl || {})) {
        next.grade.hsl[band] = { ...(next.grade.hsl[band] || {}), ...cloneValue(adjustment) };
      }
    } else next.grade[key] = cloneValue(preset.grade[key]);
  }
  if (look) {
    // Public looks never carry per-photo corrections, even in Replace mode.
  } else if (replace) {
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
  if (look && preset.params) {
    const overlay = {};
    for (const key of preset.includedFilm || []) {
      if (CREATIVE_FILM_KEYS.has(key) && Object.hasOwn(preset.params, key)) {
        overlay[key] = cloneValue(preset.params[key]);
      }
    }
    next.params = mergeFilmParams(next.params || {}, overlay);
  } else if (preset.includeFilm && preset.params) {
    next.params = replace ? normalizeFilmParams(cloneValue(preset.params))
      : mergeFilmParams(next.params || {}, cloneValue(preset.params));
  }
  if (preset.filmMode === 'on' || preset.filmMode === 'off') {
    next.params = { ...(next.params || {}), profile_enabled: preset.filmMode === 'on' };
  }
  if (filmOff) next.params = { ...(next.params || {}), profile_enabled: false };
  return next;
}
