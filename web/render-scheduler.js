export function afterVisiblePaint(maxWaitMs = 1000) {
  return new Promise((resolve) => {
    let settled = false;
    const frameIds = [];
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      frameIds.forEach((frame) => cancelAnimationFrame(frame));
      resolve(performance.now());
    };
    const timer = setTimeout(finish, maxWaitMs);
    frameIds.push(requestAnimationFrame(() => {
      frameIds.push(requestAnimationFrame(finish));
    }));
  });
}

export function debounce(callback, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

export function createFrameScheduler(callback, {
  requestFrame = fn => requestAnimationFrame(fn),
  cancelFrame = id => cancelAnimationFrame(id),
  setTimer = setTimeout, clearTimer = clearTimeout, maxWaitMs = 100,
} = {}) {
  let frame = null, timer = null, ticket = 0;
  let pending = {};
  const unschedule = () => {
    if (frame !== null) cancelFrame(frame);
    if (timer !== null) clearTimer(timer);
    frame = timer = null;
    ticket++;
  };
  const flush = () => {
    unschedule();
    const work = pending;
    pending = {};
    callback(work);
  };
  return {
    request(work = {}) {
      for (const [key, value] of Object.entries(work)) {
        pending[key] = value || pending[key];
      }
      if (frame !== null) return;
      const current = ++ticket;
      const deliver = () => { if (ticket === current) flush(); };
      frame = requestFrame(() => {
        if (ticket !== current) return;
        frame = null;
        deliver();
      });
      // Occluded WebViews can suspend RAF while still accepting commands.
      // A late frame must neither duplicate work nor consume a newer request.
      timer = setTimer(deliver, maxWaitMs);
    },
    flush() {
      if (frame === null) return;
      flush();
    },
    cancel() {
      unschedule();
      pending = {};
    },
  };
}
