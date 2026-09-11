// SPDX-License-Identifier: GPL-3.0-only
// Local spatial filters must see global grading and every preceding mask.
// The server resolves these recipes using the same ordered operations as export.
export function gradeBakeRequest(grade, masks, holdBefore = false) {
  const required = !holdBefore && (masks || []).some((mask) =>
    mask.enabled !== false && (mask.opacity ?? 1) > 0 &&
    (['texture', 'clarity', 'whites', 'blacks'].some(key => (+mask.grade?.[key] || 0) !== 0) ||
      ['curveL', 'curveR', 'curveG', 'curveB'].some(key => mask.grade?.[key]?.length === 256)));
  return required ? { grade, masks } : {};
}

export function gradeBakeKey(request) {
  return request.masks ? JSON.stringify([request.grade, request.masks]) : null;
}
