const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function linearHandleAt(mask, point, rect) {
  const distance = location => Math.hypot(
    (point[0] - location[0]) * rect.width, (point[1] - location[1]) * rect.height);
  // Test in screen pixels so the dots stay easy to grab at every zoom level.
  const startDistance = distance(mask.start), endDistance = distance(mask.end);
  if (Math.min(startDistance, endDistance) <= 11) {
    return startDistance < endDistance ? 'start' : 'end';
  }
  const dx = (mask.end[0] - mask.start[0]) * rect.width;
  const dy = (mask.end[1] - mask.start[1]) * rect.height;
  const lengthSquared = dx * dx + dy * dy;
  if (!lengthSquared) return null;
  const t = clamp(((point[0] - mask.start[0]) * rect.width * dx +
    (point[1] - mask.start[1]) * rect.height * dy) / lengthSquared, 0, 1);
  const nearest = [mask.start[0] + t * (mask.end[0] - mask.start[0]),
    mask.start[1] + t * (mask.end[1] - mask.start[1])];
  return distance(nearest) <= 7 ? 'move' : null;
}

export function editLinear(mask, gesture, point) {
  if (!gesture.handle) { mask.end = point; return; }
  const endpoints = gesture.handle === 'move' ? ['start', 'end'] : [gesture.handle];
  // Limit the shared offset, not each endpoint, to preserve the gradient's
  // angle and length when moving against the edge of the photo.
  const delta = point.map((value, axis) => clamp(value - gesture.origin[axis],
    -Math.min(...endpoints.map(key => gesture[key][axis])),
    1 - Math.max(...endpoints.map(key => gesture[key][axis]))));
  for (const key of endpoints) mask[key] = gesture[key].map((value, axis) => value + delta[axis]);
}

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

export function radialHandleAt(mask, point, rect) {
  return Object.entries(radialHandles(mask, rect.width, rect.height))
    .find(([, location]) => Math.hypot((point[0] - location[0]) * rect.width,
      (point[1] - location[1]) * rect.height) <= 11)?.[0] || null;
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
