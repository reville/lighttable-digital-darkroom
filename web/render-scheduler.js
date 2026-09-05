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

export function createFrameScheduler(callback) {
  let frame = 0;
  let pending = {};
  const flush = () => {
    frame = 0;
    const work = pending;
    pending = {};
    callback(work);
  };
  return {
    request(work = {}) {
      for (const [key, value] of Object.entries(work)) {
        pending[key] = value || pending[key];
      }
      if (!frame) frame = requestAnimationFrame(flush);
    },
    flush() {
      if (!frame) return;
      cancelAnimationFrame(frame);
      flush();
    },
    cancel() {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      pending = {};
    },
  };
}
