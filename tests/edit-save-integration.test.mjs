import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = file => readFileSync(new URL(`../web/${file}`, import.meta.url), 'utf8');
const appSource = read('app.js');
const plain = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));
function appFunction(name) {
  const pattern = new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, 'm');
  const match = appSource.match(pattern);
  assert.ok(match, `app.js must define ${name}`);
  return match[0];
}
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

// Execute the real save/navigation/undo functions with rendering and transport
// boundaries stubbed. No application logic is copied into the test harness.
function harness({manual = false} = {}) {
  const nodes = new Map(), timers = new Map(), requests = [], stateReads = [], history = [], toasts = [];
  const historyFlushes = [];
  let nextTimer = 0;
  const noop = () => {};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, style: {}, width: 0, height: 0,
      classList: {remove: noop, toggle: noop, contains: () => false},
      removeAttribute: noop, setAttribute: noop,
    });
    return nodes.get(id);
  };
  const S = {
    images: ['A.raw', 'B.raw'].map((name, index) => ({
      name, params: {profile_enabled: false}, grade: {exposure: index},
      status: 'pending', rating: 0, label: 'none', keywords: [], versions: [],
      masks: [], heals: [], optics: {}, crop: null, stateLoaded: true,
    })),
    idx: 0, seq: 0, activePane: 'editPane', viewMode: 'detail', catalogEnabled: true,
  };
  const context = {
    console, structuredClone, Promise, AggregateError, S,
    $: node, cur: () => S.images[S.idx],
    window: {addEventListener: noop},
    NATIVE_PREVIEW: false, GRADE_DEFAULTS: {},
    PRESET_BROWSER: null, METADATA: null,
    HISTORY: {
      record: (name, label, state) => history.push(plain({name, label, state})),
      refresh: noop,
      flush: async name => { historyFlushes.push({name, recordCount: history.length}); return true; },
    },
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    api: async (path, state) => {
      const result = deferred();
      requests.push({path, state: plain(state), ...result});
      if (!manual) result.resolve({ok: true});
      return result.promise;
    },
    fetch: url => {
      const result = deferred();
      stateReads.push({url, ...result});
      return result.promise.then(state => ({json: async () => state}));
    },
    cloneValue: value => value === undefined ? undefined : plain(value),
    cleanLabel: value => value || 'none',
    serializableMasks: () => plain(S.masks),
    normalizeFilmParams: value => plain(value || {}),
    normalizeMasks: value => plain(value || []),
    normalizeHeals: value => plain(value || []),
    normalizeOptics: value => plain(value || {}),
    selectedMask: () => S.masks?.[0], selectedHeal: () => S.heals?.[0],
    restoreCropChoices: noop,
    displayName: image => image.name,
    isRawInput: () => false,
    toast: message => toasts.push(message),
  };
  for (const name of [
    'readControls', 'invalidateEditedThumbnail', 'scheduleNativeMenuState',
    'syncControls', 'syncGrade', 'syncCurveFromGrade', 'syncHsl', 'syncMaskPanel',
    'syncHealPanel', 'syncOpticsPanel', 'renderKeywords', 'renderVersions',
    'refreshLists', 'drawGrade', 'applyCropVisual', 'renderFilm', 'refreshBaseEdits',
    'setRenderPresentation', 'scheduleNativeViewportLayout', 'applyView', 'setCropMode',
    'setCompareActive', 'updateLoupeInfoOverlay', 'syncAIPhoto', 'loadLensProfile',
    'loadRawCameraDefault', 'showExif', 'presentVideo', 'broadcastToLoupe', 'prefetch',
    'setEditorLoading',
  ]) context[name] = noop;
  const stateStart = appSource.indexOf("let _lastHistorySnapshot = '';");
  const stateEnd = appSource.indexOf('function photoMatchesQuery(', stateStart);
  assert.ok(stateStart >= 0 && stateEnd > stateStart);
  const code = [
    read('edit-save-queue.js').replace('export function ', 'function '),
    read('photo-undo.js').replace('export function ', 'function '),
    'const photoUndo = createPhotoUndoHistory();',
    'const _pendingStateFetches = new Map();',
    'let navigationGeneration = 0, lastNavigationDirection = 1;',
    'let renderTimer, refineTimer, settleRenderTimer, browserOriginal, browserOriginalTextureURL;',
    ...['snapshot', 'filmRenderFingerprint', 'baseEditsFingerprint', 'updateUndoRedoButtons',
      'pushUndoState', 'pushUndo', 'restore', 'undo', 'redo', 'isStateLoaded',
      'normalizeLibraryImage', 'prefetchState',
      'showCurrentImage', 'go', 'persistMark', 'saveStateFor'].map(appFunction),
    appSource.slice(stateStart, stateEnd),
    'globalThis.app = {saveState, saveStateFor, persistMark, go, showCurrentImage, pushUndo, undo, redo, flushEditSaves, queue: editSaveQueue, photoUndo};',
  ].join('\n');
  vm.runInNewContext(code + '\neditRecoveryReady = true; editRecovery = {put: async () => true, remove: async () => true};', context, {filename: 'actual-app-save-functions.js'});
  context.app.showCurrentImage(S.images[0]);
  return {...context.app, context, S, requests, stateReads, history, historyFlushes, nodes, timers, toasts};
}

test('actual saveState + go preserve A edits when B is edited immediately after navigation', async () => {
  const app = harness();
  app.S.grade.exposure = 1.7;
  app.S.masks = [{id: 'A-mask', opacity: .4}];
  app.saveState();
  await app.go(1);
  app.S.grade.exposure = -.8;
  app.S.masks = [{id: 'B-mask', opacity: .9}];
  await app.saveState(true);
  assert.deepEqual(app.requests.map(({state}) => [state.name, state.grade.exposure, state.masks[0].id]), [
    ['A.raw', 1.7, 'A-mask'], ['B.raw', -.8, 'B-mask'],
  ]);
  assert.deepEqual(app.history.map(item => [item.name, item.state.grade.exposure]), [['A.raw', 1.7], ['B.raw', -.8]]);
  assert.equal(app.historyFlushes.at(-1).recordCount, 2, 'flush includes history recorded after state writes');
  assert.equal(app.queue.getStatus().state, 'saved');
});

test('an immediate save waits for state persistence and then persistent history', async () => {
  const app = harness({manual: true});
  const historyDone = deferred();
  let flushed = false, finished = false;
  app.context.HISTORY.flush = () => {
    flushed = true;
    assert.equal(app.history[0].state.grade.exposure, 1.25);
    return historyDone.promise;
  };
  app.S.grade.exposure = 1.25;
  const save = app.saveState(true).then(result => { finished = true; return result; });
  await settle();
  assert.equal(flushed, false);
  app.requests[0].resolve({ok: true});
  await settle();
  assert.equal(flushed, true);
  assert.equal(finished, false);
  historyDone.resolve(true);
  assert.equal(await save, true);
});

test('marking an outgoing photo serializes with its pending full edit and preserves edit history', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 2.25;
  app.saveState();
  await app.go(1);
  await settle();
  const outgoing = app.S.images[0];
  outgoing.rating = 5;
  outgoing.status = 'approved';
  app.persistMark([outgoing], {rating: 5});
  await settle();
  assert.equal(app.requests.length, 1, 'mark must wait for the earlier A save');
  app.requests[0].resolve({ok: true});
  await settle();
  assert.equal(app.requests[1].state.name, 'A.raw');
  assert.equal(app.requests[1].state.grade.exposure, 2.25);
  assert.equal(app.requests[1].state.rating, 5);
  assert.equal(app.requests[1].state.status, 'approved');
  app.requests[1].resolve({ok: true});
  assert.equal(await app.flushEditSaves(), true);
  assert.ok(app.history.every(item => item.name === 'A.raw' && item.state.grade.exposure === 2.25));
});

test('returning to a photo uses retained edits when a save has failed, then Retry saves the newest edit', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 3;
  const first = app.saveState(true);
  await settle();
  app.requests[0].resolve({error: 'Disk full'});
  assert.equal(await first, false);
  assert.equal(app.nodes.get('editSaveStatus').textContent, 'Edits not saved');
  assert.equal(app.nodes.get('retryEditSave').hidden, false);
  await app.go(1);
  app.S.images[0].grade = {exposure: -10}; // Simulate a stale catalog row.
  await app.go(0);
  assert.equal(app.S.grade.exposure, 3);
  app.S.grade.exposure = 4;
  app.saveState();
  await settle();
  assert.equal(app.requests.length, 1, 'failed edits wait for explicit retry');
  const retry = app.nodes.get('retryEditSave').onclick();
  await settle();
  assert.equal(app.requests[1].state.name, 'A.raw');
  assert.equal(app.requests[1].state.grade.exposure, 4);
  app.requests[1].resolve({ok: true});
  await retry;
  assert.equal(app.nodes.get('editSaveStatus').textContent, 'Saved');
  assert.equal(app.nodes.get('retryEditSave').hidden, true);
});

test('the actual photo navigation, Undo and Redo handlers keep independent stacks', async () => {
  const app = harness();
  app.pushUndo();
  app.pushUndo();
  assert.equal(app.S.undo.length, 1, 'repeated slider starts must share one Undo step');
  app.S.grade.exposure = 2;
  app.saveState();
  await app.go(1);
  app.pushUndo();
  app.S.grade.exposure = 3;
  app.saveState();
  await app.go(0);
  assert.equal(app.S.grade.exposure, 2);
  app.undo();
  assert.equal(app.S.grade.exposure, 0);
  assert.equal(app.S.redo.length, 1);
  await app.go(1);
  assert.equal(app.S.grade.exposure, 3);
  app.undo();
  assert.equal(app.S.grade.exposure, 1);
  await app.go(0);
  app.redo();
  assert.equal(app.S.grade.exposure, 2);
  await app.flushEditSaves();
  const latestA = app.requests.filter(request => request.state.name === 'A.raw').at(-1);
  const latestB = app.requests.filter(request => request.state.name === 'B.raw').at(-1);
  assert.equal(latestA.state.grade.exposure, 2);
  assert.equal(latestB.state.grade.exposure, 1);
});

test('editing is guarded during photo loading while a new mark survives a stale read', async () => {
  const app = harness();
  app.S.grade.exposure = 1.7;
  app.saveState();
  app.S.images[1].stateLoaded = false;
  const navigation = app.go(1);
  assert.equal(app.S.editingName, 'A.raw');
  assert.equal(app.nodes.get('panel').inert, true);
  // An outstanding callback must not write A's still-visible controls into B.
  await app.saveState(true);
  assert.deepEqual(app.requests.map(request => request.state.name), ['A.raw']);
  const incoming = app.S.images[1];
  incoming.rating = 5;
  incoming.status = 'approved';
  await app.saveStateFor(incoming, true);
  assert.equal(app.requests[1].state.name, 'B.raw');
  assert.equal(app.requests[1].state.grade, undefined, 'loading photo gets metadata only');
  assert.equal(app.queue.getStatus().state, 'saved');
  app.stateReads[0].resolve({grade: {exposure: 8}, rating: 0, status: 'pending'});
  await navigation;
  assert.equal(app.S.editingName, 'B.raw');
  assert.equal(app.S.grade.exposure, 8);
  assert.equal(incoming.rating, 5);
  assert.equal(incoming.status, 'approved');
  assert.equal(app.nodes.get('panel').inert, false);
});

test('marks on the already displayed photo survive a concurrent state reload after save completion', async () => {
  const app = harness();
  const current = app.S.images[0];
  current.stateLoaded = false;
  const reload = app.go(0);
  current.rating = 4;
  current.status = 'approved';
  await app.saveStateFor(current, true);
  assert.equal(app.queue.getStatus().state, 'saved');
  app.stateReads[0].resolve({grade: {exposure: -8}, rating: 0, status: 'pending'});
  await reload;
  assert.equal(current.rating, 4);
  assert.equal(current.status, 'approved');
  assert.equal(app.S.grade.exposure, 0);
});
