// Local spatial filters must see global grading and every preceding mask.
// The server resolves these recipes using the same ordered operations as export.
export function gradeBakeRequest(grade, masks, holdBefore = false) {
  const required = !holdBefore && (masks || []).some((mask) =>
    mask.enabled !== false && (mask.opacity ?? 1) > 0 &&
    ((+mask.grade?.texture || 0) !== 0 || (+mask.grade?.clarity || 0) !== 0));
  return required ? { grade, masks } : {};
}

export function gradeBakeKey(request) {
  return request.masks ? JSON.stringify([request.grade, request.masks]) : null;
}
