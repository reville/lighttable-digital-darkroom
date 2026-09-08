import { t as tr } from './i18n.js';
import { localToolLabel } from './editor-panels.js';
/** Edit transfer plans preserve every unselected destination setting. */
const copy = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
export const TRANSFER_GROUPS = [
  ['film', tr("Film look"), tr("Stock, paper and film effects")],
  ['raw', tr("RAW development"), tr("Camera profile, capture white balance and denoise")],
  ['tone', tr("Tone"), tr("Exposure, contrast, highlights, shadows and master curve")],
  ['color', tr("Color"), tr("White balance, HSL, color curves and grading")],
  ['detail', tr("Detail and effects"), tr("Sharpening, noise, clarity, texture and vignette")],
  ['optics', tr("Lens corrections"), tr("Lens profile, distortion and chromatic aberration")],
  ['crop', tr("Crop and geometry"), tr("Crop, rotation, flips and perspective")],
  ['masks', tr("Local masks"), tr("Replace local masks; detect AI selections on each photo")],
  ['heals', tr("Healing"), tr("Copy spot positions; use only with matching framing")],
];
const RAW_KEYS = ['raw_profile', 'raw_highlight_recovery', 'raw_sensor_denoise',
  'learned_denoise', 'learned_denoise_strength', 'developProfile', 'wb_mode',
  'wb_temperature', 'wb_tint'];
const GRADE_KEYS = {
  tone: ['exposure', 'contrast', 'highlights', 'shadows', 'whites', 'blacks', 'curveL'],
  color: ['temp', 'tint', 'vibrance', 'saturation', 'curveR', 'curveG', 'curveB',
    'hsl', 'pointColor', 'colorGrading'],
  detail: ['texture', 'clarity', 'dehaze', 'vignette', 'vignetteSize', 'vignetteFeather', 'sharpness', 'sharpenRadius',
    'sharpenDetail', 'sharpenMasking', 'luminanceNoise', 'colorNoise'],
  optics: ['chromaticAberrationRedCyan', 'chromaticAberrationBlueYellow'],
};
export const GEOMETRY_KEYS = ['rotate', 'vertical', 'horizontal', 'scale', 'flipHorizontal', 'flipVertical'];
export function transferChoices(value = {}) {
  const defaults = ['film', 'raw', 'tone', 'color', 'detail', 'optics'];
  return Object.fromEntries(TRANSFER_GROUPS.map(([id]) => [id,
    typeof value[id] === 'boolean' ? value[id] : defaults.includes(id)]));
}
function replaceKeys(destination, source, keys) {
  for (const key of keys) {
    if (Object.hasOwn(source || {}, key)) destination[key] = copy(source[key]);
    else delete destination[key];
  }
}
export function transferPatch(source, destination, choices) {
  const selected = transferChoices(choices), patch = {};
  if (selected.film || selected.raw || selected.crop) {
    patch.params = copy(destination.params || {});
    const keys = new Set([...Object.keys(source.params || {}), ...Object.keys(patch.params)]);
    replaceKeys(patch.params, source.params, [...keys].filter(key =>
      key === 'rotate' ? selected.crop : RAW_KEYS.includes(key) ? selected.raw : selected.film));
  }
  if (['tone', 'color', 'detail', 'optics'].some(key => selected[key])) {
    patch.grade = copy(destination.grade || {});
    for (const [group, keys] of Object.entries(GRADE_KEYS)) {
      if (selected[group]) replaceKeys(patch.grade, source.grade, keys);
    }
  }
  if (selected.optics || selected.crop) {
    patch.optics = copy(destination.optics || {});
    const keys = new Set([...Object.keys(source.optics || {}), ...Object.keys(patch.optics)]);
    replaceKeys(patch.optics, source.optics, [...keys].filter(key =>
      GEOMETRY_KEYS.includes(key) ? selected.crop : selected.optics));
  }
  if (selected.crop) {
    patch.crop = copy(source.crop || null);
    patch.cropChoices = copy(source.cropChoices || null);
  }
  if (selected.masks) patch.masks = copy(source.masks || []);
  if (selected.heals) patch.heals = copy(source.heals || []);
  return patch;
}
const MANUAL_MASKS = new Set(['brush', 'linear', 'radial']);
/** Recompute bitmap selections; never reuse a source subject raster on another photo.
 * Object seeds and brush refinements carry image-specific intent and require review.
 * Reject the target before saving any patch if that intent cannot be recovered. */
export async function regenerateTransferMasks(masks, generate, { samePhoto = false } = {}) {
  const result = copy(masks || []);
  if (samePhoto) return result;
  const generated = new Map();
  for (const mask of result) {
    const components = mask.components?.length ? mask.components : [mask];
    const autoMask = [mask, ...components].some(component =>
      ['strokes', 'addStrokes', 'subtractStrokes', 'intersectStrokes'].some(key =>
        Array.isArray(component[key]) && component[key].some(stroke => stroke?.edgeMask != null)));
    if (autoMask) {
      throw new Error(tr("“{value}” uses Auto Mask. Recreate its strokes on this photo or exclude masks.", {value: (mask.name || tr("Mask"))}));
    }
    const smart = components.some(component => !MANUAL_MASKS.has(component.type));
    if (smart && (components.some(component => component.type === 'brush') ||
        ['addStrokes', 'subtractStrokes', 'intersectStrokes'].some(key => mask[key]?.length))) {
      throw new Error(tr("“{value}” has painted refinements. Recreate it on this photo or exclude masks.", {value: (mask.name || tr("Mask"))}));
    }
    for (const component of components) {
      if (MANUAL_MASKS.has(component.type)) continue;
      if (component.type === 'object') {
        throw new Error(tr("“{value}” needs a new object selection. Exclude masks to paste other edits.", {value: (mask.name || tr("Object mask"))}));
      }
      if (!generated.has(component.type)) {
        const response = await generate(component.type);
        if (!response?.bitmap || response.error) throw new Error(response?.error || tr('No {tool} selection found', {tool: localToolLabel(component.type)}));
        generated.set(component.type, response);
      }
      const response = generated.get(component.type);
      component.bitmap = copy(response.bitmap);
      component.provider = response.provider;
    }
    if (mask.components?.length) {
      // Legacy top-level data must not retain a stale source bitmap either.
      const first = components[0];
      if (first.bitmap) { mask.bitmap = copy(first.bitmap); mask.provider = first.provider; }
      else { delete mask.bitmap; delete mask.provider; }
    }
  }
  return result;
}
export function cropGeometry(state) {
  return { crop: copy(state.crop || null), cropChoices: copy(state.cropChoices || null),
    rotate: state.params?.rotate || 0,
    optics: Object.fromEntries(GEOMETRY_KEYS.map(key => [key, copy(state.optics?.[key])])) };
}
export function restoreCropGeometry(state, entry) {
  const restored = copy(state);
  restored.crop = copy(entry.crop); restored.cropChoices = copy(entry.cropChoices);
  restored.params = { ...restored.params, rotate: entry.rotate };
  restored.optics = { ...restored.optics };
  replaceKeys(restored.optics, entry.optics, GEOMETRY_KEYS);
  return restored;
}
