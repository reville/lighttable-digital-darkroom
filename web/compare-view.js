// Compare positions belong to the displayed photo; its controls use screen pixels.
export function clampComparePosition(value) {
  const position = Number(value);
  return Number.isFinite(position) ? Math.max(0, Math.min(1, position)) : 0.5;
}

export function compareViewGeometry(image, viewport, position) {
  if (!(image.width > 0 && image.height > 0 && viewport.width > 0 && viewport.height > 0)) return null;
  const left = Math.max(image.left, viewport.left);
  const top = Math.max(image.top, viewport.top);
  const right = Math.min(image.left + image.width, viewport.left + viewport.width);
  const bottom = Math.min(image.top + image.height, viewport.top + viewport.height);
  if (right <= left || bottom <= top) return null;
  return {
    left: left - viewport.left,
    top: top - viewport.top,
    width: right - left,
    height: bottom - top,
    dividerX: image.left + clampComparePosition(position) * image.width - left,
  };
}

export function comparePositionAtViewCenter(image, viewport) {
  if (!(image.width > 0 && viewport.width > 0)) return null;
  return clampComparePosition((viewport.left + viewport.width / 2 - image.left) / image.width);
}
