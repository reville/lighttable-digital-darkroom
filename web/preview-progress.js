// Count completed work for the preview being presented. Background detail
// renders never start this indicator, and presentation hides it immediately.
export function createPreviewProgress(publish, {
  schedule = setTimeout, cancel = clearTimeout, showDelay = 150,
} = {}) {
  let showTimer = null;
  let state = { active: false, visible: false, label: '', completed: 0, generation: null };
  const emit = () => publish({ ...state });
  return {
    start(label, generation = null) {
      state = { ...state, active: true, label, completed: 0, generation };
      if (!state.visible && showTimer === null) {
        showTimer = schedule(() => {
          showTimer = null;
          state.visible = true;
          emit();
        }, showDelay);
      }
      emit();
    },
    advance(completed, generation) {
      if (!state.active || generation !== state.generation || !Number.isInteger(completed)) return;
      completed = Math.min(4, completed);
      if (completed <= state.completed) return;
      state.completed = completed;
      emit();
    },
    finish({ error = '' } = {}) {
      cancel(showTimer);
      showTimer = null;
      state = { active: false, visible: Boolean(error), label: error,
        completed: 0, generation: null };
      emit();
    },
  };
}

// Poll the readiness endpoint with the SAME render generation. Requesting a
// new render on every poll invalidates and can cancel the active RAW decode.
export async function waitForRawRefinement({ request, isCurrent,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  maxAttempts = 1800, interval = 100,
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
