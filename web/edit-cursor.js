// SPDX-License-Identifier: GPL-3.0-only
import { linearHandleAt, radialHandleAt } from './mask-shape.js';

const rotateIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path d="M18 8a7 7 0 1 0 1 7M18 3v6h-6" fill="none" stroke="white" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><path d="M18 8a7 7 0 1 0 1 7M18 3v6h-6" fill="none" stroke="black" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
export const ROTATE_CURSOR = `url("data:image/svg+xml,${encodeURIComponent(rotateIcon)}") 12 12, crosshair`;

export function radialHandleCursor(handle, angle = 0, dragging = false) {
  if (handle === 'center') return dragging ? 'grabbing' : 'grab';
  if (handle === 'rotate') return ROTATE_CURSOR;
  if (handle !== 'x' && handle !== 'y') return 'crosshair';
  const direction = ((angle + (handle === 'y' ? 90 : 0)) % 180 + 180) % 180;
  return ['ew-resize', 'nwse-resize', 'ns-resize', 'nesw-resize'][Math.round(direction / 45) % 4];
}

export function healHandleAt(heals, selectedId, point, rect) {
  const selected = heals.find(spot => spot.id === selectedId);
  const candidates = [...(selected ? [selected] : []), ...[...heals].reverse().filter(spot => spot !== selected)];
  const distance = location => Math.hypot((point[0] - location[0]) * rect.width,
    (point[1] - location[1]) * rect.height);
  for (const spot of candidates) {
    const radius = Math.max(9, spot.radius * Math.min(rect.width, rect.height) + 5);
    if (spot.mode !== 'remove' && distance(spot.source) <= radius) return { spot, handle: 'source' };
    if (distance(spot.target) <= radius) return { spot, handle: 'target' };
  }
  return null;
}

export function editOverlayCursor(state, mask, rect) {
  const gesture = state.editGesture;
  const point = state.localPinsVisible && state.overlayHoverPoint;
  if (state.activePane === 'healPane') {
    if (gesture?.type.startsWith('heal-move')) return 'grabbing';
    if (!gesture && point && healHandleAt(state.heals, state.selectedHealId, point, rect)) return 'grab';
    return 'none';
  }
  if (state.activePane !== 'maskPane') return '';
  if (state.maskColorPick || gesture?.type === 'semantic-object') return 'crosshair';
  // During a captured drag, modifiers do not change the operation in progress.
  if (gesture?.type === 'linear') return gesture.handle ? 'grabbing' : 'crosshair';
  if (gesture?.type === 'radial') return radialHandleCursor(gesture.handle, mask?.angle, true);
  if (gesture?.type === 'brush' || (mask &&
      (mask.type === 'brush' || state.maskRefineMode || state.overlayAltKey))) return 'none';
  if (point && mask?.type === 'linear' && linearHandleAt(mask, point, rect)) return 'grab';
  if (point && mask?.type === 'radial') return radialHandleCursor(radialHandleAt(mask, point, rect), mask.angle);
  return mask && ['linear', 'radial'].includes(mask.type) ? 'crosshair' : 'default';
}

// Canvas controls use the same hit test as pointerdown, including endpoint
// constraints. Refresh after redraws as well as pointer events.
export function installCanvasHandleCursor(canvas, { hitCursor, dragCursor }) {
  let point = null;
  const refresh = () => {
    const rect = canvas.getBoundingClientRect();
    const inside = point && point.clientX >= rect.left && point.clientX <= rect.left + rect.width &&
      point.clientY >= rect.top && point.clientY <= rect.top + rect.height;
    canvas.style.cursor = dragCursor() || (inside ? hitCursor(point) : '') || '';
  };
  for (const type of ['pointerenter', 'pointermove', 'pointerdown', 'pointerup']) {
    canvas.addEventListener(type, event => {
      point = { clientX: event.clientX, clientY: event.clientY };
      refresh();
    });
  }
  for (const type of ['pointerleave', 'pointercancel']) {
    canvas.addEventListener(type, () => { point = null; refresh(); });
  }
  canvas.addEventListener('lostpointercapture', refresh);
  return refresh;
}
