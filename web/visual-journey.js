// SPDX-License-Identifier: GPL-3.0-only
// Loaded only by the opt-in native visual-review journey. No normal startup work.
export async function runVisualJourney(ctx) {
  const { S, $, cur, executeUICommand: command, uiStateReport: state,
    nativePreviewActive, postNative, pushUndo, editSaveQueue } = ctx;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const environment = () => ({visibility: document.visibilityState, hasFocus: document.hasFocus()});
  const geometry = () => {
    const bounds = id => { const r = $(id).getBoundingClientRect(); return {width:r.width, height:r.height}; };
    return {frame:bounds('cmp'), viewport:bounds('zoomwrap'), zoomReadout:$('zoomVal').textContent,
      transform:getComputedStyle($('cmp')).transform};
  };
  const fitVisible = () => {
    const g = geometry(), matrix = new DOMMatrixReadOnly(g.transform);
    return S.zoomMode === 'fit' && Math.abs(matrix.a - 1) < .01 &&
      g.frame.width <= g.viewport.width + 2 && g.frame.height <= g.viewport.height + 2;
  };
  const burst = async (name, count, action) => {
    const startedEpochMs = Date.now(), pending = [];
    for (let i=0;i<count;i++) pending.push(action(i));
    const dispatchMs = Date.now() - startedEpochMs;
    await Promise.all(pending);
    postNative('nativeBenchmarkProgress', {stage:'visual-burst', name, count, startedEpochMs, dispatchMs});
  };
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
    const entry = {journey, name, startedEpochMs: Date.now(), before: state(), environmentBefore: environment(), geometryBefore:geometry()};
    postNative('nativeBenchmarkProgress', {stage: 'visual-step-start', ...entry});
    try {
      await action(); await sleep(650);
      if (!check()) throw new Error(`Visible-state check failed: ${name}`);
      entry.status = 'passed';
    } catch (error) {
      entry.status = 'failed'; entry.error = String(error.message || error);
    } finally {
      entry.endedEpochMs = Date.now(); entry.durationMs = entry.endedEpochMs - entry.startedEpochMs;
      entry.after = state(); steps.push(entry);
      entry.environmentAfter = environment();
      entry.geometryAfter = geometry();
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
    fitVisible);
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
  }, fitVisible);
  await step('explore', 'zoom-button-mashing', async () => {
    await burst('zoom-buttons', 36, i => command(i % 3 === 0 ? 'zoomIn' : i % 3 === 1 ? 'zoomOut' : 'zoomFit'));
    await command('zoomFit'); await sleep(1200);
  }, fitVisible);
  await step('explore', 'navigation-button-mashing', async () => {
    await burst('navigation-buttons', 24, i => command(i % 2 ? 'previousPhoto' : 'nextPhoto'));
    await go('field.jpg'); await command('zoomFit'); await sleep(1200);
  }, () => cur()?.name === 'field.jpg' && S.editingName === 'field.jpg' && fitVisible());
  await step('explore', 'compare-and-panel-button-mashing', async () => {
    const initial = S.compareActive;
    await burst('compare-panels', 24, i => command(['compare','toggleLibrary','toggleFilmstrip'][i % 3]));
    if (S.compareActive !== initial) throw new Error('Even compare toggles did not restore the starting state');
    await sleep(1200);
  }, fitVisible);
  await step('explore', 'exposure-input-mashing-and-undo-redo', async () => {
    await command('pane:edit');
    const input = document.querySelector('[data-g="exposure"]');
    pushUndo();
    await burst('exposure-inputs', 40, i => {
      input.value = String(i === 39 ? .65 : (i % 9 - 4) / 4);
      input.dispatchEvent(new Event('input', {bubbles:true}));
    });
    input.dispatchEvent(new Event('change', {bubbles:true}));
    await burst('undo-redo', 20, i => command(i % 2 ? 'redo' : 'undo'));
  }, () => Math.abs(S.grade.exposure - .65) < .001);
  await step('explore', 'scope-switching', async () => {
    await command('pane:edit');
    for (const mode of ['waveform','parade','vectorscope','histogram']) {
      $('scopeMenuButton').click(); document.querySelector(`[data-scope="${mode}"]`).click();
      await sleep(450);
      const cv = $('hist'), px = cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
      if (!px.some((value,index) => index % 4 === 3 && value > 0)) throw new Error(`Empty ${mode} canvas`);
    }
  });
  await step('explore', 'crop-open-cancel-mashing', async () => {
    for (let i=0;i<6;i++) { await command('pane:crop'); $('cropCancel').click(); }
    await command('pane:edit'); await command('zoomFit'); await sleep(1200);
  }, () => !S.cropping && fitVisible());
  await step('explore', 'film-mode-reversals', async () => {
    const initial = S.params.profile_enabled;
    await burst('film-mode-buttons', 8, () => command('filmToggle'));
    await settle();
    if (S.params.profile_enabled !== initial) throw new Error('Film toggle burst changed the final mode');
  });
  await step('explore', 'final-persistence-after-mashing', async () => {
    await ctx.saveState(true); await editSaveQueue.flush();
    await go('portrait.jpg'); await go('field.jpg');
    await command('zoomFit'); await sleep(1200);
    const saved = await fetch('/api/state?name=field.jpg').then(r => r.json());
    if (Math.abs(saved.grade?.exposure - .65) > .001 || !Number.isFinite(saved.grade?.exposure))
      throw new Error('Burst edits were not persisted');
  }, () => Math.abs(S.grade.exposure - .65) < .001 && fitVisible());
  if (!nativePreviewActive()) throw new Error('Review left native presentation');
  return {schema: 1, layer: 'visual-review', steps, finalState: state(),
    coverage: 'Scripted UI commands and DOM input events including rapid bursts, scopes, crop cancellation and film mode reversals. OS input, presets, mask creation, RAW decoding and export remain outside this run.'};
}
