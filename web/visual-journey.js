// Loaded only by the opt-in native visual-review journey. No normal startup work.
export async function runVisualJourney(ctx) {
  const { S, $, cur, executeUICommand: command, uiStateReport: state,
    nativePreviewActive, postNative, pushUndo, editSaveQueue } = ctx;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const environment = () => ({visibility: document.visibilityState, hasFocus: document.hasFocus()});
  const steps = [];
  const names = S.images.map(image => image.name);
  if (names.length < 3) throw new Error('Visual review requires at least three copied photos');
  const settle = async () => {
    const deadline = performance.now() + 45000;
    while (performance.now() < deadline) {
      const report = state();
      if (report.render.state === 'ready' && report.render.name === cur()?.name &&
          report.render.backend === 'native-metal') { await sleep(400); return; }
      await sleep(100);
    }
    throw new Error('Selected photo did not settle in the native renderer');
  };
  const step = async (journey, name, action, check = () => true) => {
    const entry = {journey, name, startedEpochMs: Date.now(), before: state(), environmentBefore: environment()};
    postNative('nativeBenchmarkProgress', {stage: 'visual-step-start', ...entry});
    try {
      await action(); await sleep(650);
      if (!check()) throw new Error(`Visible-state check failed: ${name}`);
      entry.status = 'passed';
    } catch (error) {
      entry.status = 'failed'; entry.error = String(error.message || error);
      throw error;
    } finally {
      entry.endedEpochMs = Date.now(); entry.durationMs = entry.endedEpochMs - entry.startedEpochMs;
      entry.after = state(); steps.push(entry);
      entry.environmentAfter = environment();
      postNative('nativeBenchmarkProgress', {stage: 'visual-step-end', ...entry});
    }
  };
  const go = async name => { await command('goto', {name}); await settle(); };
  await step('browse', 'grid-overview', () => command('view:square'), () => S.viewMode === 'square');
  await step('browse', 'grid-scroll-down-and-back', async () => {
    const grid = $('library');
    if (!grid || grid.scrollHeight <= grid.clientHeight) throw new Error('Grid does not have scrollable content');
    for (const fraction of [0.4, 1, 0.5, 0]) {
      grid.scrollTo({top: fraction * grid.scrollHeight, behavior: 'smooth'}); await sleep(350);
    }
  });
  await step('browse', 'open-landscape', async () => { await command('view:detail'); await go('field.jpg'); });
  await step('browse', 'rate-and-filter', async () => {
    await command('rating:4');
    await command('view:square');
    await command('filter', {query: 'field'}); await sleep(700);
    if (!ctx.visible().some(image => image.name === 'field.jpg')) throw new Error('Search lost the rated photo');
    await command('filter', {query: ''});
    await command('view:detail'); await settle();
  }, () => cur()?.rating === 4);
  await step('browse', 'portrait-landscape-navigation', async () => {
    for (const name of ['portrait.jpg', 'still-life.jpg', 'field.jpg']) {
      if (!names.includes(name)) throw new Error(`Missing representative fixture: ${name}`);
      await go(name);
    }
  });
  await step('zoom', 'fit-to-actual', async () => {
    await command('zoomActual'); await sleep(1000);
  }, () => S.zoomMode === '100');
  await step('zoom', 'pan-across-photo', async () => {
    const previousPan = [S.panX, S.panY];
    const wrap = $('zoomwrap'), bounds = wrap.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2, y = bounds.top + bounds.height / 2;
    const dispatch = (type, dx, dy) => wrap.dispatchEvent(new PointerEvent(type, {
      clientX: x + dx, clientY: y + dy, button: 0, buttons: type === 'pointerup' ? 0 : 1,
      pointerId: 997, bubbles: true }));
    dispatch('pointerdown', 0, 0);
    for (let i = 1; i <= 24; i++) { dispatch('pointermove', i * 10, i * 4); await sleep(16); }
    dispatch('pointerup', 240, 96);
    if (S.panX === previousPan[0] && S.panY === previousPan[1]) throw new Error('Pan gesture did not move the viewport');
  });
  await step('zoom', 'rapid-zoom-reversals', async () => {
    for (const action of ['zoomIn', 'zoomIn', 'zoomOut', 'zoomIn', 'zoomOut', 'zoomOut']) {
      await command(action); await sleep(90);
    }
  });
  await step('zoom', 'navigate-while-zoomed', async () => {
    for (const name of ['portrait.jpg', 'still-life.jpg', 'field.jpg']) await go(name);
  });
  await step('zoom', 'return-to-fit', async () => { await command('zoomFit'); await sleep(1200); },
    () => S.zoomMode === 'fit');
  await step('edit', 'open-basic-controls', () => command('pane:edit'));
  const initialExposure = Number(S.grade.exposure || 0);
  await step('edit', 'continuous-exposure-drag-and-reversal', async () => {
    const input = document.querySelector('[data-g="exposure"]');
    if (!input || input.getBoundingClientRect().width === 0) throw new Error('Exposure slider is not visible');
    pushUndo();
    for (const value of [0.2, 0.4, 0.6, 0.8, 1, 0.7, 0.3, 0, -0.3, 0.5]) {
      input.value = String(value); input.dispatchEvent(new Event('input', {bubbles: true})); await sleep(90);
    }
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }, () => Math.abs(S.grade.exposure - 0.5) < 0.001);
  await step('edit', 'undo-exposure', () => command('undo'),
    () => Math.abs(S.grade.exposure - initialExposure) < 0.001);
  await step('edit', 'redo-exposure', () => command('redo'),
    () => Math.abs(S.grade.exposure - 0.5) < 0.001);
  await step('edit', 'compare-on', () => command('compare'), () => S.compareActive);
  await step('edit', 'compare-off', () => command('compare'), () => !S.compareActive);
  await step('edit', 'save-navigate-return', async () => {
    await ctx.saveState();
    const deadline = performance.now() + 15000;
    while (editSaveQueue.getStatus().state !== 'saved' && performance.now() < deadline) await sleep(100);
    await go('portrait.jpg'); await go('field.jpg');
    const persisted = await fetch(`/api/state?name=${encodeURIComponent('field.jpg')}`).then(r => r.json());
    if (Math.abs(persisted.grade?.exposure - 0.5) > 0.001 || !Number.isFinite(persisted.grade?.exposure)) {
      throw new Error('Exposure did not persist through navigation');
    }
  }, () => Math.abs(S.grade.exposure - 0.5) < 0.001);
  await step('explore', 'rapid-photo-and-zoom-interleaving', async () => {
    for (let i = 0; i < 8; i++) {
      void command('goto', {name: ['portrait.jpg', 'still-life.jpg', 'field.jpg'][i % 3]});
      await command(i % 2 ? 'zoomOut' : 'zoomIn'); await sleep(100);
    }
    await go('field.jpg'); await command('zoomFit'); await sleep(1200);
  });
  if (!nativePreviewActive()) throw new Error('Review left native presentation');
  return {schema: 1, layer: 'visual-review', steps, finalState: state(),
    coverage: 'Scripted UI commands and DOM input events; not OS-level pointer automation. Presets, crop, masks and export are outside this first review.'};
}
