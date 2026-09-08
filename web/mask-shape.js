const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export function radialHandles(mask, width, height) {
  const minimum = Math.min(width, height);
  const angle = (mask.angle || 0) * Math.PI / 180;
  const cos = Math.cos(angle), sin = Math.sin(angle);
  const rx = (mask.radiusX ?? mask.radius) * minimum;
  const ry = (mask.radiusY ?? mask.radius) * minimum;
  const at = (x, y) => [mask.center[0] + (x * cos - y * sin) / width,
    mask.center[1] + (x * sin + y * cos) / height];
  return {center: [...mask.center], x: at(rx, 0), y: at(0, ry), rotate: at(0, -ry - 24)};
}

export function editRadial(mask, gesture, point, rect, round = false) {
  const minimum = Math.min(rect.width, rect.height);
  if (gesture.handle === 'center') {
    mask.center = [clamp(gesture.center[0] + point[0] - gesture.start[0], 0, 1),
      clamp(gesture.center[1] + point[1] - gesture.start[1], 0, 1)];
    return;
  }
  const dx = (point[0] - mask.center[0]) * rect.width;
  const dy = (point[1] - mask.center[1]) * rect.height;
  if (gesture.handle === 'rotate') {
    mask.angle = ((Math.atan2(dy, dx) * 180 / Math.PI + 270) % 360) - 180;
    return;
  }
  const angle = (mask.angle || 0) * Math.PI / 180;
  const x = clamp(Math.abs(dx * Math.cos(angle) + dy * Math.sin(angle)) / minimum, 0.01, 1.5);
  const y = clamp(Math.abs(-dx * Math.sin(angle) + dy * Math.cos(angle)) / minimum, 0.01, 1.5);
  if (!gesture.handle || gesture.handle === 'x') mask.radiusX = x;
  if (!gesture.handle || gesture.handle === 'y') mask.radiusY = y;
  if (round) mask.radiusX = mask.radiusY = Math.max(x, y);
  mask.radius = Math.min(mask.radiusX, mask.radiusY);
}
