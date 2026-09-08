import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { t as tr, tn as trn } from '../web/i18n.js';
import { localToolLabel } from '../web/editor-panels.js';
import {presetEditState, reconcilePresetAdjustment} from '../web/preset-amount.js';

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
function harness({manual = false, client = 'test-window'} = {}) {
  const nodes = new Map(), timers = new Map(), requests = [], stateReads = [], history = [], toasts = [];
  const historyFlushes = [];
  let nextTimer = 0;
  const noop = () => {};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, style: {}, width: 0, height: 0, focus: noop,
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
    console, structuredClone, Promise, AggregateError, S, tr, trn, localToolLabel,
    presetEditState, reconcilePresetAdjustment,
    transferRunning: false, transferCancelled: false, linkedMetadataTargets: images => images,
    $: node, cur: () => S.images[S.idx],
    window: {addEventListener: noop, confirm: () => true},
    CLIENT_ID: client,
    SURVEY: {active: 'B.raw', names: ['A.raw', 'B.raw']},
    cullResults: () => S.images, chosenCull: () => ['sharp'], CULL_LABELS: {sharp: 'Sharp'},
    NATIVE_PREVIEW: false, GRADE_DEFAULTS: {},
    PRESET_BROWSER: null, METADATA: null, CAPTURE_TIME: null, ENHANCE: null,
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
    'stopZoomMotion', 'setRenderPresentation', 'scheduleNativeViewportLayout', 'applyView', 'setCropMode',
    'setCompareActive', 'updateLoupeInfoOverlay', 'syncAIPhoto', 'loadLensProfile',
    'loadRawCameraDefault', 'showExif', 'presentVideo', 'broadcastToLoupe', 'prefetch',
    'setEditorLoading', 'syncPairControls', 'beginCropSession', 'syncPreviewDetailStatus',
    'invalidateVisibleCache', 'syncCullPanel', 'confirmTransfer', 'showTransferDialog', 'closeTransferDialog',
  ]) context[name] = noop;
  const stateStart = appSource.indexOf("let _lastHistorySnapshot = '';");
  const stateEnd = appSource.indexOf('function photoMatchesQuery(', stateStart);
  assert.ok(stateStart >= 0 && stateEnd > stateStart);
  const code = [
    read('edit-save-queue.js').replace('export function ', 'function '),
    read('edit-transfer.js').replaceAll('export ', ''),
    read('close-barrier.js').replace('export function ', 'function '),
    read('photo-undo.js').replace('export function ', 'function '),
    'const photoUndo = createPhotoUndoHistory();',
    'const _pendingStateFetches = new Map();',
    'let navigationGeneration = 0, lastNavigationDirection = 1, cropSession = null;',
    'let presetAmountGesture = null;',
    "let _stripKey = '', _gridKey = '';",

    'let renderTimer, refineTimer, settleRenderTimer, browserOriginal, browserOriginalTextureURL;',
    ...['snapshot', 'filmRenderFingerprint', 'baseEditsFingerprint', 'updateUndoRedoButtons',
      'pushUndoState', 'pushUndo', 'restore', 'undo', 'redo', 'isStateLoaded',
      'normalizeLibraryImage', 'prefetchState',
      'showCurrentImage', 'go', 'photoReadyForEditing', 'syncPhotoActions',
      'persistMark', 'saveStateFor', 'enqueuePhotoPatch',
      'pasteSettingsTo', 'applyCullFlags', 'keepSurveySelection', 'reconcilePeerSave', 'applyServerStateEvent'].map(appFunction),
    appSource.slice(stateStart, stateEnd),
    'globalThis.app = {saveState, saveStateFor, persistMark, go, showCurrentImage, pushUndo, undo, redo, flushEditSaves, pasteSettingsTo, applyCullFlags, keepSurveySelection, applyServerStateEvent, queue: editSaveQueue, photoUndo};',
  ].join('\n').replace(/^import .*;\r?\n/gm, '');
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
  app.persistMark([outgoing], {rating: 5, status: 'approved'});
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

test('a mark-only recovery draft retains the identity of its original', async () => {
  const app = harness({manual: true});
  const image = app.S.images[1];
  image.recoverySourceKey = 'original-content-revision';
  app.persistMark([image], {rating: 5});
  assert.equal(app.queue.getPending(image.name).sourceKey, image.recoverySourceKey);
  await settle();
  app.requests[0].resolve({ok: true});
  await app.flushEditSaves();
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
  for (const request of app.requests.slice(1)) request.resolve({ok: true});
  await retry;
  assert.equal(app.nodes.get('editSaveStatus').textContent, 'Saved');
  assert.equal(app.nodes.get('retryEditSave').hidden, true);
});

test('actual go and editor controls never display a recovery rejected for a replaced original', async () => {
  const app = harness({manual: true});
  app.queue.enqueue('A.raw', {
    state: {name: 'A.raw', grade: {exposure: 9}},
    sourceKey: 'old-original', expectedRecoverySourceKey: 'old-original',
  }, {immediate: true});
  await settle();
  app.requests[0].resolve({error: 'The original changed; the draft was kept'});
  await assert.rejects(app.queue.flush());
  await app.go(1);
  await app.go(0);
  assert.equal(app.S.images[0].grade.exposure, 0);
  assert.equal(app.S.grade.exposure, 0);
  assert.equal(app.queue.getPending('A.raw').state.grade.exposure, 9);
  assert.equal(app.nodes.get('retryEditSave').hidden, false);
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


test('a rating action does not write unrelated divergent pair metadata', async () => {
  const app = harness();
  const companion = app.S.images[1];
  companion.rating = 4;
  companion.status = 'skipped';
  companion.label = 'blue';
  app.persistMark([companion], {rating: 4});
  await app.flushEditSaves();
  assert.deepEqual(app.requests[0].state, {name: 'B.raw', rating: 4});
});

function clipboard(exposure) {
  return {params: {profile_enabled: false}, grade: {exposure}, masks: [], heals: [], optics: {}};
}

test('paste joins a pending full save and later slider input cannot be reverted by its completion', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 1;
  app.S.crop = {x: .1, y: .2, w: .7, h: .6};
  app.saveState();
  app.S.clipboard = clipboard(4);
  const paste = app.pasteSettingsTo([app.S.images[0]]);
  await settle();
  assert.equal(app.S.grade.exposure, 4, 'paste is visible before its transport settles');
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].path, '/api/state');
  assert.equal(app.requests[0].state.grade.exposure, 4);
  assert.deepEqual(app.requests[0].state.crop, {x: .1, y: .2, w: .7, h: .6});
  app.S.grade.exposure = 5;
  app.saveState();
  app.requests[0].resolve({ok: true});
  await paste;
  assert.equal(app.S.grade.exposure, 5, 'paste completion must not reapply stale controls');
  const saved = app.flushEditSaves();
  await settle();
  assert.equal(app.requests[1].state.grade.exposure, 5);
  app.requests[1].resolve({ok: true});
  assert.equal(await saved, true);
});

test('paste supersedes a failed noncurrent recipe and retry keeps its crop and new adjustments', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 1;
  app.S.crop = {x: .1};
  const failed = app.saveState(true);
  await settle();
  app.requests[0].resolve({error: 'Disk full'});
  assert.equal(await failed, false);
  await app.go(1);
  app.S.clipboard = clipboard(4);
  await app.pasteSettingsTo([app.S.images[0]]);
  assert.equal(app.requests.length, 1, 'failed state waits for explicit retry');
  assert.equal(app.queue.getPending('A.raw').state.grade.exposure, 4);
  assert.deepEqual(plain(app.queue.getPending('A.raw').state.crop), {x: .1});
  assert.ok(!app.toasts.includes('Settings pasted'));
  const retry = app.nodes.get('retryEditSave').onclick();
  await settle();
  assert.equal(app.requests[1].state.grade.exposure, 4);
  for (const request of app.requests.slice(1)) request.resolve({ok: true});
  await retry;
});

test('assisted culling serializes new flags with pending full recipes', async () => {
  const app = harness();
  app.S.grade.exposure = 2;
  app.saveState();
  await app.applyCullFlags([], 'approved');
  assert.ok(app.requests.every(request => request.path === '/api/state'));
  assert.deepEqual(app.requests.map(request => [request.state.name, request.state.status]), [
    ['A.raw', 'approved'], ['B.raw', 'approved'],
  ]);
  assert.equal(app.requests[0].state.grade.exposure, 2);
  assert.equal(app.queue.getStatus().state, 'saved');
});

test('survey keep waits behind an outgoing full save and preserves its reject flag', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 2;
  app.saveState();
  await app.go(1);
  await settle();
  const keep = app.keepSurveySelection();
  await settle();
  assert.deepEqual(app.requests.map(request => request.state.name), ['A.raw', 'B.raw']);
  assert.equal(app.requests[1].state.status, 'approved');
  app.requests[1].resolve({ok: true});
  app.requests[0].resolve({ok: true});
  await settle();
  assert.equal(app.requests[2].state.name, 'A.raw');
  assert.equal(app.requests[2].state.status, 'skipped');
  assert.equal(app.requests[2].state.grade.exposure, 2);
  app.requests[2].resolve({ok: true});
  await keep;
});

test('accepted external patch repairs an older in-flight save without replacing unrelated edits', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 2;
  app.S.crop = {x: .15};
  const oldSave = app.saveState(true);
  await settle();
  await app.applyServerStateEvent({client: 'cli', names: ['A.raw'], origin: 'cli', patch: {grade: {exposure: 8}}});
  assert.equal(app.S.grade.exposure, 8);
  assert.equal(app.stateReads.length, 0, 'accepted fields must not depend on a stale GET');
  assert.equal(app.requests.length, 1, 'repair is serialized behind the older request');
  app.requests[0].resolve({ok: true});
  await settle();
  assert.equal(app.requests[1].state.grade.exposure, 8);
  assert.deepEqual(app.requests[1].state.crop, {x: .15});
  app.requests[1].resolve({ok: true});
  await oldSave;
  assert.equal(await app.flushEditSaves(), true);
  app.undo();
  assert.equal(app.S.grade.exposure, 2, 'the external patch remains undoable');
  const undoSave = app.flushEditSaves();
  await settle();
  app.requests[2].resolve({ok: true});
  await undoSave;
});

test('accepted external patch updates a failed outgoing recipe before explicit retry', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 2;
  const oldSave = app.saveState(true);
  await settle();
  app.requests[0].resolve({error: 'Offline'});
  await oldSave;
  await app.go(1);
  await app.applyServerStateEvent({client: 'cli', names: ['A.raw'], patch: {grade: {exposure: 9}, rating: 5}});
  assert.equal(app.requests.length, 1);
  assert.equal(app.queue.getPending('A.raw').state.grade.exposure, 9);
  await app.go(0);
  assert.equal(app.S.grade.exposure, 9);
  assert.equal(app.S.images[0].rating, 5);
  const retry = app.nodes.get('retryEditSave').onclick();
  await settle();
  assert.equal(app.requests[1].state.grade.exposure, 9);
  app.requests[1].resolve({ok: true});
  await retry;
});

test('metadata-only external events leave edit controls and queued recipes intact', async () => {
  const app = harness();
  app.S.grade.exposure = 2;
  app.saveState();
  await app.applyServerStateEvent({names: ['A.raw'], fields: ['metadata'], origin: 'metadata'});
  assert.equal(app.S.grade.exposure, 2);
  assert.equal(app.queue.getPending('A.raw').state.grade.exposure, 2);
  assert.equal(app.stateReads.length, 0);
  await app.flushEditSaves();
});

test('selective paste waits for destination loading and preserves unchecked edits', async () => {
  const app = harness();
  app.S.images[1].stateLoaded = false;
  const navigation = app.go(1);
  app.S.clipboard = clipboard(4);
  const paste = app.pasteSettingsTo([app.S.images[1]]);
  await settle();
  assert.equal(app.requests.length, 0, 'do not overwrite unknown unchecked edits');
  app.stateReads[0].resolve({grade: {exposure: -3}, crop: {x: .1, y: .2, w: .8, h: .7}});
  await navigation;
  await paste;
  assert.equal(app.requests[0].state.grade.exposure, 4);
  assert.equal(app.queue.getStatus().state, 'saved');
  assert.equal(app.S.grade.exposure, 4);
  assert.deepEqual(plain(app.S.crop), {x: .1, y: .2, w: .8, h: .7});
});

test('a failed history flush prevents save success and Retry waits for history', async () => {
  const app = harness();
  app.context.HISTORY.flush = async () => false;
  assert.equal(await app.saveState(true), false);
  let retried = false;
  app.context.HISTORY.retry = async () => { retried = true; return false; };
  await app.nodes.get('retryEditSave').onclick();
  assert.equal(retried, true);
  assert.ok(!app.toasts.includes('Edits saved'));
  assert.match(app.toasts.at(-1), /Still unable to save/);
});


test('an external patch on an idle photo is displayed without echoing another state write', async () => {
  const app = harness();
  await app.applyServerStateEvent({client: 'other-window', names: ['A.raw'], patch: {grade: {exposure: 6}}});
  assert.equal(app.S.grade.exposure, 6);
  assert.equal(app.S.images[0].grade.exposure, 6);
  assert.equal(app.requests.length, 0);
  assert.equal(app.queue.getStatus().state, 'saved');
});

test('two peer windows converge after concurrent saves without echoing repair writes', async () => {
  const a = harness({manual: true, client: 'window-a'});
  const b = harness({manual: true, client: 'window-b'});
  a.S.grade.exposure = 2;
  b.S.grade.exposure = 3;
  const saves = [a.saveState(true), b.saveState(true)];
  await settle();
  // The server accepts A then B, publishing each event before its HTTP ack.
  for (const source of [a, b]) {
    const event = {client: source.context.CLIENT_ID, origin: 'window',
      names: ['A.raw'], patch: plain(source.requests[0].state)};
    await a.applyServerStateEvent(event);
    await b.applyServerStateEvent(event);
  }
  for (const app of [a, b]) app.requests[0].resolve({ok: true});
  await Promise.all(saves);
  await settle();
  for (const app of [a, b]) {
    assert.equal(app.stateReads.length, 1);
    app.stateReads[0].resolve(plain(b.requests[0].state));
  }
  await settle();
  for (const app of [a, b]) {
    assert.equal(app.requests.length, 1, 'no reciprocal repair POSTs');
    assert.equal(app.S.grade.exposure, 3, 'both display the last accepted recipe');
    assert.deepEqual(plain(app.queue.getStatus().pendingNames), []);
  }
});

test('peer reconciliation cannot overwrite controls edited while its read was pending', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 1;
  const saved = app.saveState(true);
  await settle();
  await app.applyServerStateEvent({client: 'peer', origin: 'window', names: ['A.raw'], patch: {grade: {exposure: 2}}});
  app.requests[0].resolve({ok: true});
  await saved;
  await settle();
  app.S.grade.exposure = 4;
  const newer = app.saveState(true);
  await settle();
  app.requests[1].resolve({ok: true});
  await newer;
  app.stateReads[0].resolve({grade: {exposure: 2}});
  await settle();
  assert.equal(app.S.grade.exposure, 4);
});

function toneAndMasksClipboard() {
  return {...clipboard(4), sourceName: 'A.raw', masks: [{type: 'subject'}], choices: {
    film: false, raw: false, tone: true, color: false, detail: false,
    optics: false, crop: false, masks: true, heals: false,
  }};
}

test('selective paste preserves unchecked color changed while AI detection is pending', async () => {
  const app = harness();
  app.S.images[1].grade = {exposure: 1, temp: 0};
  app.S.clipboard = toneAndMasksClipboard();
  const detection = deferred(), originalApi = app.context.api;
  let detecting = false;
  app.context.api = (path, body) => {
    if (path !== '/api/mask/semantic') return originalApi(path, body);
    detecting = true; return detection.promise;
  };
  const paste = app.pasteSettingsTo([app.S.images[1]]);
  await settle();
  assert.equal(detecting, true);
  await app.applyServerStateEvent({client: 'cli', names: ['B.raw'],
    patch: {grade: {exposure: 1, temp: 20}}});
  detection.resolve({bitmap: {width: 1, height: 1, data: 'AA=='}, provider: 'fixture'});
  await paste;
  const delivered = app.requests.find(request => request.state.name === 'B.raw');
  assert.equal(delivered.state.grade.exposure, 4);
  assert.equal(delivered.state.grade.temp, 20);
  assert.equal(app.S.images[1].grade.temp, 20);
  assert.equal(app.history.find(step => step.name === 'B.raw').state.grade.temp, 20);
});

test('selective paste refuses AI masks when effective target geometry changes during detection', async () => {
  const app = harness();
  app.S.clipboard = toneAndMasksClipboard();
  const detection = deferred(), originalApi = app.context.api;
  app.context.api = (path, body) => path === '/api/mask/semantic'
    ? detection.promise : originalApi(path, body);
  const paste = app.pasteSettingsTo([app.S.images[1]]);
  await settle();
  await app.applyServerStateEvent({client: 'cli', names: ['B.raw'],
    patch: {params: {profile_enabled: false, rotate: 90}}});
  detection.resolve({bitmap: {width: 1, height: 1, data: 'AA=='}, provider: 'fixture'});
  await paste;
  assert.equal(app.requests.some(request => request.state.name === 'B.raw'), false);
  assert.equal(app.S.images[1].params.rotate, 90);
  assert.equal(app.S.images[1].grade.exposure, 1);
  assert.equal(app.S.images[1].masks.length, 0);
  assert.match(app.nodes.get('transferStatus').textContent, /geometry changed/);
});

test('selective paste refuses AI masks if the source identity changes during detection', async () => {
  const app = harness();
  app.S.images[1].recoverySourceKey = 'original';
  app.S.clipboard = toneAndMasksClipboard();
  const detection = deferred(), originalApi = app.context.api;
  app.context.api = (path, body) => path === '/api/mask/semantic'
    ? detection.promise : originalApi(path, body);
  const paste = app.pasteSettingsTo([app.S.images[1]]);
  await settle();
  app.S.images[1] = {...app.S.images[1], recoverySourceKey: 'replacement'};
  detection.resolve({bitmap: {width: 1, height: 1, data: 'AA=='}, provider: 'fixture'});
  await paste;
  assert.equal(app.requests.some(request => request.state.name === 'B.raw'), false);
  assert.equal(app.S.images[1].grade.exposure, 1);
  assert.match(app.nodes.get('transferStatus').textContent, /photo or its geometry changed/);
});

test('selective paste never replaces unchecked edits with defaults after a library row reload', async () => {
  const app = harness();
  app.S.clipboard = toneAndMasksClipboard();
  const detection = deferred(), originalApi = app.context.api;
  app.context.api = (path, body) => path === '/api/mask/semantic'
    ? detection.promise : originalApi(path, body);
  const paste = app.pasteSettingsTo([app.S.images[1]]);
  await settle();
  app.S.images[1] = {name: 'B.raw', stateLoaded: false, hasEdits: true};
  detection.resolve({bitmap: {width: 1, height: 1, data: 'AA=='}, provider: 'fixture'});
  await paste;
  assert.equal(app.requests.some(request => request.state.name === 'B.raw'), false);
  assert.match(app.nodes.get('transferStatus').textContent, /latest existing settings are not loaded/);
});

test('peer reconciliation cannot interrupt a slider gesture before its change event saves', async () => {
  const app = harness({manual: true});
  app.S.grade.exposure = 1;
  const saved = app.saveState(true);
  await settle();
  await app.applyServerStateEvent({client: 'peer', origin: 'window', names: ['A.raw'], patch: {grade: {exposure: 2}}});
  app.requests[0].resolve({ok: true});
  await saved;
  await settle();
  app.S.grade.exposure = 4; // input event, before pointer-up/change enqueues it
  app.stateReads[0].resolve({grade: {exposure: 2}});
  await settle();
  assert.equal(app.S.grade.exposure, 4);
});
