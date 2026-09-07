// One visible status spans draft, RAW decode, and final presentation. Quick
// cache hits stay silent; closely spaced stages never flash the badge off/on.
export function createPreviewProgress(publish, {
  now = () => performance.now(), schedule = setTimeout, cancel = clearTimeout,
  showDelay = 150, minimumVisible = 400, hideDelay = 140,
} = {}) {
  let showTimer = null, hideTimer = null, shownAt = 0;
  let state = { active: false, visible: false, label: '' };
  const emit = () => publish({ ...state });
  return {
    start(label) {
      cancel(hideTimer);
      hideTimer = null;
      state = { ...state, active: true, label };
      if (!state.visible && showTimer === null) {
        showTimer = schedule(() => {
          showTimer = null;
          shownAt = now();
          state.visible = true;
          emit();
        }, showDelay);
      }
      emit();
    },
    finish({ immediate = false, error = '' } = {}) {
      cancel(showTimer);
      cancel(hideTimer);
      showTimer = hideTimer = null;
      state.active = false;
      const hide = () => {
        hideTimer = null;
        state = { active: false, visible: Boolean(error), label: error };
        emit();
      };
      if (error || immediate || !state.visible) return hide();
      state.label = 'Preview ready';
      emit();
      hideTimer = schedule(hide, Math.max(hideDelay, minimumVisible - (now() - shownAt)));
    },
  };
}

// Poll the readiness endpoint with the SAME render generation. Requesting a
// new render on every poll invalidates and can cancel the active RAW decode.
export async function waitForRawRefinement({ request, isCurrent,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  maxAttempts = 360, interval = 500,
}) {
  for (let attempt = 0; attempt < maxAttempts && isCurrent(); attempt++) {
    const result = await request();
    if (!isCurrent()) return false;
    if (result.error) throw new Error(result.error);
    if (result.ready) return true;
    if (attempt + 1 < maxAttempts) await sleep(interval);
  }
  if (isCurrent()) throw new Error('RAW preview could not finish. Try the photo again.');
  return false;
}
