// SPDX-License-Identifier: GPL-3.0-only
import { installCanvasHandleCursor } from './edit-cursor.js';
import { isIdentityPoints } from './color-tools.js';
import { t as tr } from './i18n.js';

const CHANNELS = { L: 'curveL', R: 'curveR', G: 'curveG', B: 'curveB' };
const COLORS = { curveL: '#e9e9e7', curveR: '#f87171', curveG: '#4ade80', curveB: '#6b8ff0' };
const GAP = 1 / 255;
const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
const identity = () => [[0, 0], [1, 1]];
const copyPoints = points => points.map(point => [...point]);
const validLUT = value => Array.isArray(value) && value.length === 256 && value.every(Number.isFinite);
const copyCurve = value => Array.isArray(value) ? [...value] : value;
const sameCurve = (left, right) => Array.isArray(left) && Array.isArray(right)
  ? left.length === right.length && left.every((value, index) => value === right[index])
  : left === right;

// Local PCHIP interpolation: harmonic-mean slopes preserve each segment's
// direction without forcing a flat tangent at every control point. The editor
// supplies finite, normalized points with distinct x coordinates.
// https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.PchipInterpolator.html
export function localCurveLUT(points) {
  const sorted = [...points].sort((a, b) => a[0] - b[0]);
  if (sorted.every(([x, y]) => x === y)) {
    return Array.from({ length: 256 }, (_, index) => index / 255);
  }
  const count = sorted.length, intervals = [], secants = [];
  for (let index = 0; index < count - 1; index++) {
    intervals.push(sorted[index + 1][0] - sorted[index][0]);
    secants.push((sorted[index + 1][1] - sorted[index][1]) / intervals[index]);
  }
  const slopes = new Array(count).fill(0);
  if (count === 2) slopes[0] = slopes[1] = secants[0];
  else {
    for (let index = 1; index < count - 1; index++) {
      const before = secants[index - 1], after = secants[index];
      if (before * after <= 0) continue;
      const first = 2 * intervals[index] + intervals[index - 1];
      const second = intervals[index] + 2 * intervals[index - 1];
      slopes[index] = (first + second) / (first / before + second / after);
    }
    const endpoint = (h0, h1, d0, d1) => {
      const slope = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1);
      if (slope * d0 <= 0) return 0;
      return d0 * d1 <= 0 && Math.abs(slope) > 3 * Math.abs(d0) ? 3 * d0 : slope;
    };
    slopes[0] = endpoint(intervals[0], intervals[1], secants[0], secants[1]);
    slopes[count - 1] = endpoint(intervals.at(-1), intervals.at(-2), secants.at(-1), secants.at(-2));
  }
  let segment = 0;
  return Array.from({ length: 256 }, (_, index) => {
    const x = index / 255;
    if (x <= sorted[0][0]) return sorted[0][1];
    if (x >= sorted.at(-1)[0]) return sorted.at(-1)[1];
    while (segment < count - 2 && x > sorted[segment + 1][0]) segment++;
    const low = sorted[segment], high = sorted[segment + 1];
    const h = intervals[segment], t = (x - low[0]) / h;
    const t2 = t * t, t3 = t2 * t;
    return clamp((2 * t3 - 3 * t2 + 1) * low[1]
      + (t3 - 2 * t2 + t) * h * slopes[segment]
      + (-2 * t3 + 3 * t2) * high[1]
      + (t3 - t2) * h * slopes[segment + 1]);
  });
}

/**
 * Edit the selected mask's grade only. Call sync() after mask selection, Undo,
 * grade replacement or language changes. changed(mask) redraws the preview;
 * save(mask) runs once per completed gesture. Cancellation restores its input.
 * An optional select uses L/R/G/B values; without one, only curveL is exposed.
 */
export function installMaskCurve({ canvas, reset, channel, getMask, pushUndo, dropUndo, changed, save }) {
  const cache = new WeakMap();
  let state = null, selected = 0, gesture = null;
  let refreshCursor = () => {};
  canvas.tabIndex = 0;
  canvas.style.touchAction = 'none';
  canvas.setAttribute('role', 'slider');
  canvas.setAttribute('aria-valuemin', '0');
  canvas.setAttribute('aria-valuemax', '255');
  canvas.setAttribute('aria-orientation', 'vertical');

  const current = () => ({ mask: getMask(), key: CHANNELS[channel?.value] || 'curveL' });
  const metrics = () => {
    const width = canvas.width, height = canvas.height;
    const inset = Math.min(8, width / 4, height / 4);
    return { width, height, inset, w: Math.max(1, width - inset * 2), h: Math.max(1, height - inset * 2) };
  };
  const at = event => {
    const rect = canvas.getBoundingClientRect(), { width, height, inset, w, h } = metrics();
    return [clamp(((event.clientX - rect.left) * width / Math.max(1, rect.width) - inset) / w),
      clamp(1 - ((event.clientY - rect.top) * height / Math.max(1, rect.height) - inset) / h)];
  };
  const near = point => {
    const rect = canvas.getBoundingClientRect(), { width, height, w, h } = metrics();
    let index = -1, distance = 11;
    state.points.forEach(([x, y], candidate) => {
      const next = Math.hypot((x - point[0]) * w * rect.width / width,
        (y - point[1]) * h * rect.height / height);
      if (next < distance) { index = candidate; distance = next; }
    });
    return index;
  };

  function remember() {
    state.source = copyCurve(state.mask.grade?.[state.key]);
    let channels = cache.get(state.mask);
    if (!channels) { channels = new Map(); cache.set(state.mask, channels); }
    channels.set(state.key, state);
  }

  function draw() {
    refreshCursor();
    const active = !!state;
    canvas.setAttribute('aria-disabled', String(!active));
    canvas.tabIndex = active ? 0 : -1;
    if (channel) channel.disabled = !active;
    reset.disabled = !active || state.mask.grade?.[state.key] == null;
    const instructions = tr('Drag points to adjust the curve. Double-click an interior point to remove it. Use [ and ] to select points, arrow keys to move, Enter to add, and Delete to remove.');
    canvas.title = instructions;
    canvas.setAttribute('aria-description', instructions);
    canvas.setAttribute('aria-label', tr('Mask tone curve'));
    const [input, output] = active ? state.points[selected] : [0, 0];
    canvas.setAttribute('aria-valuenow', String(Math.round(output * 255)));
    canvas.setAttribute('aria-valuetext', active
      ? tr('Point {point} of {count}. Input {input}, output {output}.', {
        point: selected + 1, count: state.points.length, input: Math.round(input * 255), output: Math.round(output * 255),
      }) : tr('Select a mask first.'));
    const context = canvas.getContext('2d');
    if (!context) return;
    const { width, height, inset, w, h } = metrics();
    context.clearRect(0, 0, width, height);
    const path = lut => {
      context.beginPath();
      lut.forEach((value, index) => {
        const x = inset + index / (lut.length - 1) * w, y = inset + (1 - clamp(value)) * h;
        if (index) context.lineTo(x, y); else context.moveTo(x, y);
      });
      context.stroke();
    };
    context.strokeStyle = '#353535'; context.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      context.beginPath(); context.moveTo(inset + w * i / 4, inset);
      context.lineTo(inset + w * i / 4, inset + h); context.stroke();
      context.beginPath(); context.moveTo(inset, inset + h * i / 4);
      context.lineTo(inset + w, inset + h * i / 4); context.stroke();
    }
    path([0, 1]);
    if (!active) return;
    const stored = state.mask.grade?.[state.key];
    const lut = validLUT(stored) ? stored : localCurveLUT(state.points);
    context.strokeStyle = COLORS[state.key]; context.lineWidth = 1.5; path(lut);
    state.points.forEach(([x, y], index) => {
      context.fillStyle = index === selected ? '#fff' : COLORS[state.key];
      const size = index === selected ? 8 : 5;
      context.fillRect(inset + x * w - size / 2, inset + (1 - y) * h - size / 2, size, size);
    });
  }

  function release(previous) {
    if (previous?.pointerId != null && canvas.hasPointerCapture?.(previous.pointerId)) {
      canvas.releasePointerCapture(previous.pointerId);
    }
  }

  function finish(cancel = false) {
    const previous = gesture;
    if (!previous) return;
    gesture = null;
    refreshCursor();
    release(previous);
    if (!previous.dirty) return;
    // An external edit or Undo owns a replaced value; never roll it back.
    const stillOwned = previous.mask.grade === previous.grade
      && sameCurve(previous.mask.grade?.[previous.key], previous.lastValue);
    if (cancel && stillOwned) {
      if (previous.hadCurve) previous.grade[previous.key] = previous.beforeValue;
      else delete previous.grade[previous.key];
      if (!previous.hadGrade && Object.keys(previous.grade).length === 0) delete previous.mask.grade;
      previous.state.points = previous.beforePoints;
      previous.state.source = copyCurve(previous.beforeValue);
      // The curve is back where it started, so the step this gesture pushed
      // would be an Undo that does nothing. Withdraw it.
      dropUndo?.(previous.undoState);
      changed(previous.mask);
    } else if (!cancel && stillOwned) save(previous.mask);
  }

  function sync() {
    let { mask, key } = current();
    if (gesture && (mask !== gesture.mask || key !== gesture.key
      || mask?.grade !== gesture.grade || !sameCurve(mask?.grade?.[key], gesture.lastValue))) {
      finish(true);
      ({ mask, key } = current());
    }
    if (!mask) { state = null; selected = 0; draw(); return; }
    const source = mask.grade?.[key];
    let next = cache.get(mask)?.get(key);
    if (!next || !sameCurve(next.source, source)) {
      next = { mask, key, source: copyCurve(source), points: validLUT(source)
        ? [0, 32, 64, 96, 128, 160, 192, 224, 255].map(index => [index / 255, clamp(source[index])])
        : identity() };
    }
    if (state !== next) selected = 0;
    state = next;
    selected = Math.min(selected, state.points.length - 1);
    remember(); draw();
  }

  function begin(pointerId) {
    gesture = { mask: state.mask, key: state.key, state, grade: state.mask.grade,
      hadGrade: !!state.mask.grade, hadCurve: Object.hasOwn(state.mask.grade || {}, state.key),
      beforeValue: state.mask.grade?.[state.key], beforePoints: copyPoints(state.points),
      lastValue: copyCurve(state.mask.grade?.[state.key]), pointerId, dirty: false };
  }

  function edit(update) {
    if (!gesture.dirty) gesture.undoState = pushUndo();
    update();
    const grade = state.mask.grade ||= {};
    if (isIdentityPoints(state.points)) delete grade[state.key];
    else grade[state.key] = localCurveLUT(state.points);
    gesture.grade = grade;
    gesture.lastValue = copyCurve(grade[state.key]);
    gesture.dirty = true;
    remember(); draw(); changed(state.mask);
  }

  function move(point) {
    const points = state.points;
    const x = selected === 0 ? 0 : selected === points.length - 1 ? 1
      : clamp(point[0], points[selected - 1][0] + GAP, points[selected + 1][0] - GAP);
    const next = [x, clamp(point[1])];
    if (next.every((value, index) => value === points[selected][index])) return;
    edit(() => { points[selected] = next; });
  }

  function insert(point) {
    const points = state.points;
    const closeX = points.findIndex(([x]) => Math.abs(x - point[0]) < GAP);
    if (closeX >= 0) { selected = closeX; return; }
    const index = points.findIndex(([x]) => x > point[0]);
    if (index <= 0 || points[index][0] - points[index - 1][0] < GAP * 2) return;
    const x = clamp(point[0], points[index - 1][0] + GAP, points[index][0] - GAP);
    edit(() => { points.splice(index, 0, [x, clamp(point[1])]); selected = index; });
  }

  function remove() {
    if (selected <= 0 || selected >= state.points.length - 1) return;
    edit(() => { state.points.splice(selected, 1); selected--; });
  }

  canvas.addEventListener('pointerdown', event => {
    sync();
    if (!state || gesture || event.button !== 0 || event.isPrimary === false) return;
    event.preventDefault(); event.stopPropagation(); canvas.focus({ preventScroll: true });
    begin(event.pointerId);
    const point = at(event), index = near(point);
    if (index >= 0) selected = index; else insert(point);
    canvas.setPointerCapture(event.pointerId); draw();
  });
  canvas.addEventListener('pointermove', event => {
    sync();
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    event.preventDefault(); move(at(event));
  });
  canvas.addEventListener('pointerup', event => {
    sync();
    if (gesture?.pointerId === event.pointerId) finish();
  });
  canvas.addEventListener('pointercancel', event => {
    if (gesture?.pointerId === event.pointerId) { finish(true); sync(); }
  });
  canvas.addEventListener('lostpointercapture', event => {
    if (gesture?.pointerId === event.pointerId) { finish(true); sync(); }
  });
  canvas.addEventListener('dblclick', event => {
    sync();
    if (!state || gesture) return;
    event.preventDefault(); event.stopPropagation();
    const index = near(at(event));
    if (index <= 0 || index >= state.points.length - 1) return;
    selected = index; begin(); remove(); finish();
  });
  canvas.addEventListener('keydown', event => {
    sync();
    if (!state || event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key;
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '[', ']', 'Home', 'End', 'Enter', 'Delete', 'Backspace', 'Escape'].includes(key)) return;
    event.preventDefault(); event.stopPropagation();
    if (key === 'Escape') { finish(true); sync(); return; }
    if (gesture?.pointerId != null) return;
    if (['[', ']', 'Home', 'End'].includes(key)) {
      finish();
      selected = key === 'Home' ? 0 : key === 'End' ? state.points.length - 1
        : clamp(selected + (key === ']' ? 1 : -1), 0, state.points.length - 1);
      draw(); return;
    }
    if (!gesture) begin();
    if (key.startsWith('Arrow')) {
      const point = [...state.points[selected]], step = (event.shiftKey ? 10 : 1) / 255;
      point[key === 'ArrowLeft' || key === 'ArrowRight' ? 0 : 1]
        += key === 'ArrowLeft' || key === 'ArrowDown' ? -step : step;
      move(point);
    } else if (key === 'Delete' || key === 'Backspace') {
      if (!event.repeat) remove();
    } else if (!event.repeat) {
      const index = Math.min(selected, state.points.length - 2);
      const left = state.points[index], right = state.points[index + 1];
      insert([(left[0] + right[0]) / 2, (left[1] + right[1]) / 2]);
    }
  });
  canvas.addEventListener('keyup', event => {
    if (gesture && gesture.pointerId == null
      && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Enter', 'Delete', 'Backspace'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation(); finish();
    }
  });
  canvas.addEventListener('blur', () => { finish(); });
  reset.addEventListener('click', () => {
    sync();
    if (!state || reset.disabled) return;
    finish(); begin();
    edit(() => { state.points = identity(); selected = 0; }); finish();
  });
  channel?.addEventListener('change', sync);
  refreshCursor = installCanvasHandleCursor(canvas, {
    hitCursor: point => {
      if (!state) return 'default';
      const index = near(at(point));
      return index < 0 ? 'crosshair' : index === 0 || index === state.points.length - 1 ? 'ns-resize' : 'grab';
    },
    dragCursor: () => gesture?.pointerId == null ? null :
      selected === 0 || selected === state.points.length - 1 ? 'ns-resize' : 'grabbing',
  });
  sync();
  return { sync };
}
