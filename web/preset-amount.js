import { cloneValue } from './state.js';
import { GRADE_DEFAULTS } from './gl.js';
import { LOOK_GRADE_KEYS, CREATIVE_FILM_KEYS } from './presets.js';

const GROUPS = ['params', 'grade', 'masks', 'heals', 'optics'];
function equal(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 0.00011;
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].every(key => equal(a[key], b[key]));
}
const mix = (a, b, t) => a + (b - a) * t;
export const presetEditState = (state) => Object.fromEntries(
  GROUPS.map(key => [key, cloneValue(state[key])]),
);

function blend(a, b, t, fallback = 0) {
  if (equal(a, b)) return cloneValue(b);
  if (typeof b === 'number') return mix(typeof a === 'number' ? a : fallback, b, t);
  if (b && typeof b === 'object' && !Array.isArray(b)) {
    return Object.fromEntries(Object.keys(b).map(key => [key, blend(a?.[key], b[key], t)]));
  }
  return cloneValue(b);
}

function curveAt(curve, x) {
  if (!Array.isArray(curve) || curve.length < 2) return x;
  const position = x * (curve.length - 1), lo = Math.floor(position);
  return mix(curve[lo], curve[Math.min(lo + 1, curve.length - 1)], position - lo);
}

function blendGrade(a = {}, b = {}, t) {
  const result = blend(a, b, t);
  for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (/^curve[LRGB]$/.test(key)) {
      result[key] = Array.from({ length: 256 }, (_, i) =>
        mix(curveAt(a[key], i / 255), curveAt(b[key], i / 255), t));
      // Match grade._clean_curve: nearly neutral curves are omitted on save.
      // Otherwise a low Amount loses its preset as soon as the photo reloads.
      if (result[key].every((value, i) => Math.abs(value - i / 255) < 0.002)) delete result[key];
    } else if (key === 'pointColor') {
      // Selection geometry stays fixed; only the correction changes strength.
      result[key] = (b[key] || []).map((point, i) => {
        const next = cloneValue(point), before = a[key]?.[i];
        const sameSelection = before && ['hue', 'range', 'refSaturation', 'refLuminance']
          .every(field => before[field] === point[field]);
        for (const field of ['hueShift', 'saturation', 'luminance', 'uniformHue', 'uniformSaturation', 'uniformLuminance']) {
          if (field in point) next[field] = mix(sameSelection ? before[field] || 0 : 0, point[field], t);
        }
        return next;
      });
    } else if (key === 'colorGrading' && b[key]) {
      result[key] = blend(a[key], b[key], t);
      for (const tone of ['shadows', 'midtones', 'highlights', 'global']) {
        const before = a[key]?.[tone], after = b[key][tone];
        if (!after) continue;
        // A new tint fades in at its chosen hue instead of rotating through red.
        const start = before?.saturation ? before.hue || 0 : after.hue || 0;
        const delta = ((after.hue || 0) - start + 540) % 360 - 180;
        result[key][tone].hue = (start + delta * t + 360) % 360;
      }
      if ('blending' in b[key]) result[key].blending = mix(a[key]?.blending ?? 0.5, b[key].blending, t);
    } else if (typeof (b[key] ?? a[key]) === 'number') {
      result[key] = mix(a[key] ?? GRADE_DEFAULTS[key] ?? 0, b[key] ?? GRADE_DEFAULTS[key] ?? 0, t);
    }
  }
  return result;
}

/** Amount edits actual settings, so every preview and export uses the same result. */
export function blendPresetState(base, target, amount) {
  const t = Math.max(0, Math.min(100, Number(amount) || 0)) / 100;
  if (!t) return cloneValue(base);
  if (t === 1) return cloneValue(target);
  const result = cloneValue(target);
  result.grade = blendGrade(base.grade, target.grade, t);
  for (const group of ['params', 'optics']) result[group] = blend(base[group], target[group], t);
  // Development times select measured profile variants, just like stock and
  // paper. Interpolating these numbers selects a nonexistent dropdown option.
  for (const key of ['development_time', 'print_development_time']) {
    if (key in (target.params || {})) result.params[key] = target.params[key];
  }
  for (const group of ['masks', 'heals']) {
    result[group] = (target[group] || []).map(item => {
      const before = (base[group] || []).find(other => other.id === item.id);
      if (equal(before, item)) return cloneValue(item);
      return { ...cloneValue(item), opacity: mix(before?.opacity ?? (before ? 1 : 0), item.opacity ?? 1, t) };
    });
  }
  return result;
}

export function presetControlledSettings(preset) {
  if (!preset) return { grade: [], film: [], filmMode: 'preserve' };
  const look = preset.scope === 'look';
  const gradeKeys = [];
  const includedGrade = preset.includedGrade || Object.keys(preset.grade || {});
  for (const key of includedGrade) {
    if (Object.prototype.hasOwnProperty.call(preset.grade || {}, key)) {
      if (!look || LOOK_GRADE_KEYS.has(key)) {
        gradeKeys.push(key);
      }
    }
  }
  const filmKeys = [];
  const rawFilmKeys = preset.includedFilm || Object.keys(preset.params || {});
  if (look && preset.params) {
    for (const key of rawFilmKeys) {
      if (CREATIVE_FILM_KEYS.has(key) && Object.prototype.hasOwnProperty.call(preset.params, key)) {
        filmKeys.push(key);
      }
    }
  } else if (preset.includeFilm && preset.params) {
    for (const key of rawFilmKeys) {
      if (key !== 'profile_enabled' && Object.prototype.hasOwnProperty.call(preset.params, key)) {
        filmKeys.push(key);
      }
    }
  }
  const filmMode = preset.filmMode || (preset.includeFilm ? 'on' : 'preserve');
  return { grade: gradeKeys, film: filmKeys, filmMode };
}

/** Preserve manual changes outside the preset; retire it if a controlled setting changed. */
export function reconcilePresetAdjustment(adjustment, current) {
  if (!adjustment?.base || !adjustment?.target) return null;
  const expected = blendPresetState(adjustment.base, adjustment.target,
    adjustment.enabled ? adjustment.amount : 0);
  const next = cloneValue(adjustment);
  const controlled = adjustment.controlled;

  if (controlled && typeof controlled === 'object') {
    const controlledGrade = new Set(controlled.grade || []);
    const controlledFilm = new Set(controlled.film || []);
    const filmMode = controlled.filmMode || 'preserve';
    const controlledOptics = new Set(controlled.optics || []);

    // 1. Film profile enabled check
    const currentFilmOn = current?.params?.profile_enabled !== false;
    const expectedFilmOn = expected?.params?.profile_enabled !== false;
    if (filmMode === 'on' && !currentFilmOn && expectedFilmOn) {
      return null;
    }
    if (filmMode === 'off' && currentFilmOn && !expectedFilmOn) {
      return null;
    }
    if (currentFilmOn !== expectedFilmOn) {
      if (filmMode === 'preserve') {
        next.base.params = { ...(next.base.params || {}), profile_enabled: currentFilmOn };
        next.target.params = { ...(next.target.params || {}), profile_enabled: currentFilmOn };
      } else {
        return null;
      }
    }

    // 2. Check params
    const currentParams = current?.params || {};
    const expectedParams = expected?.params || {};
    for (const key of new Set([...Object.keys(expectedParams), ...Object.keys(currentParams)])) {
      if (key === 'profile_enabled') continue;
      if (equal(currentParams[key], expectedParams[key])) continue;
      if (controlledFilm.has(key)) {
        return null;
      }
      if (currentParams[key] === undefined) {
        delete next.base.params[key];
        delete next.target.params[key];
      } else {
        next.base.params = next.base.params || {};
        next.target.params = next.target.params || {};
        next.base.params[key] = cloneValue(currentParams[key]);
        next.target.params[key] = cloneValue(currentParams[key]);
      }
    }

    // 3. Check grade
    const currentGrade = current?.grade || {};
    const expectedGrade = expected?.grade || {};
    for (const key of new Set([...Object.keys(expectedGrade), ...Object.keys(currentGrade)])) {
      if (equal(currentGrade[key], expectedGrade[key])) continue;
      if (controlledGrade.has(key)) {
        return null;
      }
      if (currentGrade[key] === undefined) {
        delete next.base.grade[key];
        delete next.target.grade[key];
      } else {
        next.base.grade = next.base.grade || {};
        next.target.grade = next.target.grade || {};
        next.base.grade[key] = cloneValue(currentGrade[key]);
        next.target.grade[key] = cloneValue(currentGrade[key]);
      }
    }

    // 4. Check optics
    const currentOptics = current?.optics || {};
    const expectedOptics = expected?.optics || {};
    for (const key of new Set([...Object.keys(expectedOptics), ...Object.keys(currentOptics)])) {
      if (equal(currentOptics[key], expectedOptics[key])) continue;
      if (controlledOptics.has(key)) {
        return null;
      }
      if (currentOptics[key] === undefined) {
        delete next.base.optics[key];
        delete next.target.optics[key];
      } else {
        next.base.optics = next.base.optics || {};
        next.target.optics = next.target.optics || {};
        next.base.optics[key] = cloneValue(currentOptics[key]);
        next.target.optics[key] = cloneValue(currentOptics[key]);
      }
    }

    // 5. Check masks & heals
    for (const group of ['masks', 'heals']) {
      if (equal(current[group], expected[group])) continue;
      const controlledList = controlled[group];
      if (Array.isArray(controlledList) && controlledList.length > 0) {
        return null;
      }
      next.base[group] = cloneValue(current[group] || []);
      next.target[group] = cloneValue(current[group] || []);
    }

    return next;
  }

  function reconcile(base, target, expected, current) {
    if (equal(current, expected)) return true;
    if (equal(base, target)) return true;
    if (!expected || Array.isArray(expected) || typeof expected !== 'object') return false;
    for (const key of new Set([...Object.keys(expected), ...Object.keys(current || {})])) {
      if (equal(current?.[key], expected[key])) continue;
      if (equal(base?.[key], target?.[key])) {
        for (const state of [base, target]) {
          if (current?.[key] === undefined) delete state[key];
          else state[key] = cloneValue(current[key]);
        }
      } else {
        if (expected[key] && typeof expected[key] === 'object' && !Array.isArray(expected[key])) {
          base[key] ||= {}; target[key] ||= {};
        }
        if (!reconcile(base?.[key], target?.[key], expected[key], current?.[key])) return false;
      }
    }
    return true;
  }
  for (const group of GROUPS) {
    if (equal(current[group], expected[group])) continue;
    if (equal(next.base[group], next.target[group])) {
      next.base[group] = cloneValue(current[group]); next.target[group] = cloneValue(current[group]);
    } else if (!reconcile(next.base[group], next.target[group], expected[group], current[group])) return null;
  }
  return next;
}
