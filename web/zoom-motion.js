/** A short, interruptible camera move shared by DOM and native presentation. */
export function createZoomMotion({paint, settled, requestFrame = requestAnimationFrame,
  cancelFrame = cancelAnimationFrame, now = () => performance.now(),
  reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches,
  setTimer = setTimeout, clearTimer = clearTimeout, duration = 240} = {}) {
  let flight = null, frame = null, timer = null;
  function cancel() {
    if (frame !== null) cancelFrame(frame);
    if (timer !== null) clearTimer(timer);
    frame = timer = null;
    flight = null;
  }
  function finish() {
    if (!flight) return;
    const target = flight.to;
    cancel();
    paint({...target});
    settled();
  }
  function step(at) {
    frame = null;
    if (!flight) return;
    const t = Math.min(1, Math.max(0, (at - flight.started) / duration));
    if (t === 1 || reducedMotion()) { finish(); return; }
    const {from, to} = flight;
    const eased = t * t * (3 - 2 * t);
    // Constant proportional scale feels even across a large Fit-to-1:1 move.
    const zoom = from.zoom * (to.zoom / from.zoom) ** eased;
    // Pan follows scale, keeping the chosen image point under the pointer.
    const mix = Math.abs(to.zoom - from.zoom) > 1e-9
      ? (zoom - from.zoom) / (to.zoom - from.zoom) : eased;
    paint({zoom, panX: from.panX + (to.panX - from.panX) * mix,
      panY: from.panY + (to.panY - from.panY) * mix});
    if (flight) frame = requestFrame(step);
  }
  return {
    get active() { return flight !== null; },
    get target() { return flight ? {...flight.to} : null; },
    cancel,
    finish,
    start(from, to) {
      cancel();
      if (!(from.zoom > 0 && to.zoom > 0) ||
          ![...Object.values(from), ...Object.values(to)].every(Number.isFinite)) return;
      flight = {from: {...from}, to: {...to}, started: now()};
      if (reducedMotion() || duration <= 0 ||
          ['zoom', 'panX', 'panY'].every(key => from[key] === to[key])) {
        finish(); return;
      }
      paint({...from});
      frame = requestFrame(step);
      // An occluded/hidden window may stop RAF. Never leave a half-finished view.
      timer = setTimer(finish, duration + 80);
    },
  };
}
