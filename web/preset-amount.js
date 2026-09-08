import { cloneValue } from './state.js';
import { GRADE_DEFAULTS } from './gl.js';

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
  for (const group of ['masks', 'heals']) {
    result[group] = (target[group] || []).map(item => {
      const before = (base[group] || []).find(other => other.id === item.id);
      if (equal(before, item)) return cloneValue(item);
      return { ...cloneValue(item), opacity: mix(before?.opacity ?? (before ? 1 : 0), item.opacity ?? 1, t) };
    });
  }
  return result;
}

/** Preserve manual changes outside the preset; retire it if a controlled setting changed. */
export function reconcilePresetAdjustment(adjustment, current) {
  if (!adjustment?.base || !adjustment?.target) return null;
  const expected = blendPresetState(adjustment.base, adjustment.target,
    adjustment.enabled ? adjustment.amount : 0);
  const next = cloneValue(adjustment);
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
