import { t as tr } from './i18n.js';

/** Display names are separate from the stable mask/removal protocol keys. */
export function localToolLabel(type) {
  const labels = {
    brush: tr('Brush'), linear: tr('Linear'), radial: tr('Radial'),
    subject: tr('Subject'), sky: tr('Sky'), object: tr('Object'), depth: tr('Depth'),
    person: tr('Person'), 'face-skin': tr('Face skin'), eyes: tr('Eyes'),
    eyebrows: tr('Eyebrows'), lips: tr('Lips'), teeth: tr('Teeth'), hair: tr('Hair'),
    remove: tr('Remove'), heal: tr('Heal'), clone: tr('Clone'),
  };
  return labels[type] || String(type || '');
}

export const LOCAL_GRADE_DEFAULTS = Object.freeze({
  exposure: 0, contrast: 0, highlights: 0, shadows: 0, whites: 0, blacks: 0,
  temp: 0, tint: 0, saturation: 0, texture: 0, clarity: 0,
});

export const OPTICS_DEFAULTS = Object.freeze({
  profileOverride: null, profileEnabled: false, profileDistortion: true, profileVignette: true,
  flipHorizontal: false, flipVertical: false,
  distortion: 0, vignette: 0, vertical: 0, horizontal: 0, rotate: 0, scale: 1,
});

export const MAX_MASKS = 16;
export const MAX_MASK_COMPONENTS = 12;
export const MAX_TOTAL_MASK_POINTS = 20000;
export const MAX_HEALS = 50;
// A linear gradient shorter than this fraction of the frame has no usable
// direction and contributes nothing. Mirrors LINEAR_MIN_SPAN in edits.py; the
// threshold is normalized so preview and export agree at any resolution.
export const LINEAR_MIN_SPAN = 1e-4;

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const editId = (prefix) => `${prefix}-${
  (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`}`;

export function normalizeMasks(raw) {
  let pointsRemaining = MAX_TOTAL_MASK_POINTS;
  const normalizeStrokes = (value) => (Array.isArray(value) ? value : [])
    .slice(0, 64).map((stroke) => {
      const points = (Array.isArray(stroke?.points) ? stroke.points : [])
        .slice(0, Math.min(512, pointsRemaining)).map((point) => [
          clamp(+(point?.[0] ?? 0.5), 0, 1),
          clamp(+(point?.[1] ?? 0.5), 0, 1),
        ]);
      pointsRemaining -= points.length;
      return {
        size: clamp(+(stroke?.size ?? 0.08), 0.005, 0.5),
        feather: clamp(+(stroke?.feather ?? 0.65), 0, 1),
        flow: clamp(+(stroke?.flow ?? 1), 0.01, 1), points,
        ...(stroke?.buildUp ? {buildUp: true, density: clamp(+(stroke.density ?? 1), 0.01, 1),
          ...(stroke.edgeMask ? {edgeMask: {...stroke.edgeMask}} : {})} : {}),
      };
    }).filter((stroke) => stroke.points.length);
  return (Array.isArray(raw) ? raw : []).slice(0, MAX_MASKS).map((mask, index) => {
    const allowed = ['brush', 'linear', 'radial', 'subject', 'sky', 'object', 'depth',
      'person', 'face-skin', 'eyes', 'eyebrows', 'lips', 'teeth', 'hair'];
    const sourceComponents = Array.isArray(mask?.components) && mask.components.length
      ? mask.components : [{...(mask || {}), invert: false}];
    const components = sourceComponents.slice(0, MAX_MASK_COMPONENTS).map((source, componentIndex) => {
      const type = allowed.includes(source?.type) ? source.type : 'radial';
      const component = {
        id: String(source?.id || editId('component')),
        type,
        combine: componentIndex === 0 ? 'add' :
          (['add', 'subtract', 'intersect'].includes(source?.combine) ? source.combine : 'add'),
        invert: !!source?.invert,
      };
      if (type === 'brush') component.strokes = normalizeStrokes(source?.strokes);
      else if (type === 'linear') {
        component.start = Array.isArray(source?.start) ? source.start : [0.25, 0.5];
        component.end = Array.isArray(source?.end) ? source.end : [0.75, 0.5];
      } else if (type === 'radial') {
        component.center = Array.isArray(source?.center) ? source.center : [0.5, 0.5];
        component.radius = clamp(+(source?.radius ?? 0.25), 0.01, 1.5);
        component.radiusX = clamp(+(source?.radiusX ?? component.radius), 0.01, 1.5);
        component.radiusY = clamp(+(source?.radiusY ?? component.radius), 0.01, 1.5);
        component.angle = clamp(+(source?.angle ?? 0), -180, 180);
        component.feather = clamp(+(source?.feather ?? 0.65), 0, 1);
      } else {
        component.bitmap = {
          width: clamp(Math.round(+source?.bitmap?.width || 1), 1,
            source?.bitmap?.encoding === 'png' ? 1024 : 256),
          height: clamp(Math.round(+source?.bitmap?.height || 1), 1,
            source?.bitmap?.encoding === 'png' ? 1024 : 256),
          data: String(source?.bitmap?.data || ''),
        };
        if (source?.bitmap?.encoding === 'png') component.bitmap.encoding = 'png';
        component.provider = String(source?.provider || 'on-device');
        if (type === 'depth') {
          component.depthLow = clamp(+(source?.depthLow ?? 0.55), 0, 1);
          component.depthHigh = clamp(+(source?.depthHigh ?? 1), 0, 1);
          if (component.depthLow > component.depthHigh) {
            [component.depthLow, component.depthHigh] = [component.depthHigh, component.depthLow];
          }
        }
      }
      return component;
    });
    const primary = components[0];
    const type = primary.type;
    const result = {
      id: String(mask?.id || editId('mask')),
      name: String(mask?.name || tr('Mask {number}', {number: index + 1})),
      type, enabled: mask?.enabled !== false, invert: !!mask?.invert,
      opacity: clamp(+(mask?.opacity ?? 1), 0, 1),
      lumaLow: clamp(+(mask?.lumaLow ?? 0), 0, 1),
      lumaHigh: clamp(+(mask?.lumaHigh ?? 1), 0, 1),
      colorHue: mask?.colorHue == null || !Number.isFinite(+mask.colorHue)
        ? null : ((+mask.colorHue % 360) + 360) % 360,
      colorRange: clamp(+(mask?.colorRange ?? 30), 2, 90),
      colorAmount: clamp(+(mask?.colorAmount ?? 1), 0, 1),
      grade: { ...LOCAL_GRADE_DEFAULTS, ...(mask?.grade || {}) },
      components,
    };
    if (result.lumaLow > result.lumaHigh) {
      [result.lumaLow, result.lumaHigh] = [result.lumaHigh, result.lumaLow];
    }
    result.addStrokes = normalizeStrokes(mask?.addStrokes);
    result.subtractStrokes = normalizeStrokes(mask?.subtractStrokes);
    result.intersectStrokes = normalizeStrokes(mask?.intersectStrokes);
    if (type === 'brush') result.strokes = primary.strokes;
    else if (type === 'linear') {
      result.start = primary.start; result.end = primary.end;
    } else if (type === 'radial') {
      result.center = primary.center; result.radius = primary.radius;
      result.radiusX = primary.radiusX; result.radiusY = primary.radiusY;
      result.angle = primary.angle;
      result.feather = primary.feather;
    } else {
      result.bitmap = primary.bitmap; result.provider = primary.provider;
      if (type === 'depth') {
        result.depthLow = primary.depthLow; result.depthHigh = primary.depthHigh;
      }
    }
    return result;
  });
}

export function normalizeHeals(raw) {
  return (Array.isArray(raw) ? raw : []).slice(0, MAX_HEALS).map((spot) => ({
    id: String(spot?.id || editId('heal')),
    mode: ['remove', 'clone'].includes(spot?.mode) ? spot.mode : 'heal',
    enabled: spot?.enabled !== false,
    target: Array.isArray(spot?.target) ? spot.target : [0.5, 0.5],
    source: Array.isArray(spot?.source) ? spot.source : [0.4, 0.4],
    radius: clamp(+(spot?.radius ?? 0.04), 0.005, 0.25),
    feather: clamp(+(spot?.feather ?? 0.65), 0, 1),
    opacity: clamp(+(spot?.opacity ?? 1), 0, 1),
  }));
}

export function normalizeOptics(raw) {
  const value = { ...OPTICS_DEFAULTS, ...(raw || {}) };
  value.profileEnabled = !!value.profileEnabled;
  value.profileDistortion = value.profileDistortion !== false;
  value.profileVignette = value.profileVignette !== false;
  const overrideKeys = ['cameraMaker', 'cameraModel', 'lensMaker', 'lensModel'];
  value.profileOverride = overrideKeys.every((key) => typeof value.profileOverride?.[key] === 'string'
    && value.profileOverride[key].length > 0 && value.profileOverride[key].length <= 256)
    ? Object.fromEntries(overrideKeys.map((key) => [key, value.profileOverride[key]])) : null;
  value.flipHorizontal = !!value.flipHorizontal;
  value.flipVertical = !!value.flipVertical;
  ['distortion', 'vignette', 'vertical', 'horizontal'].forEach((key) => {
    value[key] = clamp(+value[key] || 0, -1, 1);
  });
  value.rotate = clamp(+value.rotate || 0, -15, 15);
  value.scale = clamp(+value.scale || 1, 1, 1.6);
  return value;
}
