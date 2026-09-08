// Photo geometry uses displayed CSS pixels; only the visible overlay is
// rasterized, at the display's density. Its bitmap never inherits photo zoom.
export function screenOverlayGeometry(image, frame, viewport, pixelRatio = 1) {
  if (![image, frame, viewport].every(rect => rect.width > 0 && rect.height > 0)) return null;
  const left = Math.max(image.left, frame.left, viewport.left);
  const top = Math.max(image.top, frame.top, viewport.top);
  const right = Math.min(image.left + image.width, frame.left + frame.width, viewport.left + viewport.width);
  const bottom = Math.min(image.top + image.height, frame.top + frame.height, viewport.top + viewport.height);
  if (right <= left || bottom <= top) return null;
  const ratio = Number.isFinite(pixelRatio) && pixelRatio > 0 ? pixelRatio : 1;
  return {
    left: left - viewport.left, top: top - viewport.top,
    width: right - left, height: bottom - top,
    sourceX: image.left - left, sourceY: image.top - top,
    sourceWidth: image.width, sourceHeight: image.height,
    pixelWidth: Math.ceil((right - left) * ratio),
    pixelHeight: Math.ceil((bottom - top) * ratio),
    pixelRatio: ratio,
  };
}

export function prepareScreenOverlay(canvas, geometry) {
  const ctx = canvas.getContext('2d');
  if (!geometry) {
    canvas.style.width = '0px'; canvas.style.height = '0px';
    canvas.width = 1; canvas.height = 1;
    return null;
  }
  const g = geometry;
  Object.assign(canvas.style, {
    left: `${g.left}px`, top: `${g.top}px`,
    width: `${g.width}px`, height: `${g.height}px`,
  });
  if (canvas.width !== g.pixelWidth) canvas.width = g.pixelWidth;
  if (canvas.height !== g.pixelHeight) canvas.height = g.pixelHeight;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Use the actual backing-to-CSS ratio, including rounding at fractional DPR.
  const sx = canvas.width / g.width, sy = canvas.height / g.height;
  ctx.setTransform(sx, 0, 0, sy, g.sourceX * sx, g.sourceY * sy);
  return { ctx, width: g.sourceWidth, height: g.sourceHeight, pixelRatio: g.pixelRatio };
}
