import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const read = file => readFileSync(new URL(`../web/${file}`, import.meta.url), 'utf8');
const source = read('app.js');
const initialize = source.match(/^async function initializeEditRecovery\([^]*?^}/m)[0];
const go = source.match(/^async function go\([^]*?^}/m)[0];
// Exercise actual navigation and the pending-state merge before renderer setup.
const showCurrent = source.match(/^function showCurrentImage\([^]*?  const hadSavedParams/m)[0]
  .replace(/  const hadSavedParams$/, '}');
const reconcile = source.match(/^async function reconcilePeerSave\([^]*?^}/m)[0];
const retryStart = source.indexOf("$('retryEditSave').onclick = async () => {");
const retryHandler = source.slice(retryStart, source.indexOf('\n};', retryStart) + 3);
const refreshRecovery = source.match(/^async function refreshDeferredEditRecovery\([^]*?^}/m)?.[0] || '';
const moduleFor = async file => import(`data:text/javascript;base64,${Buffer.from(read(file)).toString('base64')}`);
const {createEditSaveQueue} = await moduleFor('edit-save-queue.js');
const {recoveryAcknowledged} = await moduleFor('edit-recovery.js');
const plain = value => JSON.parse(JSON.stringify(value));

async function start({catalog = true, savedExposure = 0, history = null, restore = true,
  removeFails = false, replaceDuringPrompt = false, key = 'revision', legacyKey = null,
  failSends = 0, failReads = 0} = {}) {
  const state = {name: 'photo.RAW', grade: {exposure: 1}, rating: 5, keywords: ['Trip']};
  let records = [{name: state.name, token: 'draft', payload: {state, sourceKey: key,
    history: {label: 'Exposure', state: {grade: {exposure: 1}}}}}];
  let saved = {grade: {exposure: savedExposure}, rating: 5, keywords: ['Trip'],
    _recoverySourceKey: 'revision', _recoveryLegacySourceKey: legacyKey,
    _recoveryHistoryAvailable: catalog, _recoveryHistory: history};
  const removals = [], sends = [], prompts = [], toasts = [], reconciliations = [];
  const journal = {list: async () => plain(records),
    put: async (name, token, payload) => {records = [{name, token, payload: plain(payload)}];},
    remove: async (name, token) => {if (removeFails) throw new Error('read only');
      removals.push(token); records = records.filter(item => item.token !== token);}};
  const queue = createEditSaveQueue({journal, send: async (name, payload) => {
    sends.push(plain(payload));
    if (failSends-- > 0) throw new Error('temporary save failure');
    if (payload.expectedRecoverySourceKey && payload.expectedRecoverySourceKey !== saved._recoverySourceKey) throw new Error('source changed');
    saved = {...saved, ...plain(payload.state),
      _recoveryHistory: payload.history ? plain(payload.history.state) : saved._recoveryHistory};
  }});
  const S = {catalogEnabled: catalog, idx: 0, seq: 0, images: [{name: state.name, grade: {exposure: 0}, rating: 0,
    keywords: [], stateLoaded: !catalog, hasEdits: false}]};
  const retryButton = {};
  const context = {window: {localStorage: {}}, nativeBridge: () => null,
    nativeJournalRequest: () => {}, createEditRecovery: () => journal,
    editRecovery: null, editRecoveryReady: false, editRecoveryIssue: null,
    deferredEditRecovery: new Map(), deferredRecoveryRefreshIssue: null,
    updateEditRecoveryHealth: error => {context.editRecoveryIssue = error;}, recoveryAcknowledged, S,
    getJSON: async () => plain(saved), toast: message => toasts.push(message),
    chooseEditRecovery: async items => {prompts.push(items); if (replaceDuringPrompt) saved._recoverySourceKey = 'replacement'; return restore;},
    editSaveQueue: queue, normalizeLibraryImage: value => value,
    flushEditSaves: async () => {try {await queue.flush(); return true;} catch {return false;}},
    $: name => name === 'retryEditSave' ? retryButton : {},
    cur: () => S.images[S.idx], cropSession: null, lastNavigationDirection: 1,
    navigationGeneration: 0, renderTimer: null, settleRenderTimer: null,
    clearTimeout: () => {}, NATIVE_PREVIEW: false, CAPTURE_TIME: null, HISTORY: null,
    syncPhotoActions: () => {}, setRenderPresentation: () => {}, isStateLoaded: () => true, prefetch: () => {},
    snapshot: () => JSON.stringify(S.images[S.idx].grade),
    fetch: async () => {
      if (failReads-- > 0) throw new Error('temporary read failure');
      return {json: async () => plain(saved)};
    },
    applyServerStateEvent: async event => {
      reconciliations.push(plain(event)); Object.assign(S.images[0], plain(event.patch));
    },
  };
  vm.runInNewContext([initialize, go, showCurrent, reconcile, refreshRecovery, retryHandler,
    'globalThis.initialize = initializeEditRecovery;'].join('\n'), context);
  await context.initialize({catalog: catalog ? {path: '/catalog'} : null, folder: '/photos'});
  return {S, get saved() {return saved;}, get records() {return records;}, removals, sends, prompts, toasts, queue,
    go: context.go, retry: retryButton.onclick, reconciliations,
    get recoveryIssue() {return context.editRecoveryIssue;},
    failNextSave: () => {failSends = 1;}};
}
for (const catalog of [false, true]) test(`startup restoration updates displayed edits and metadata (${catalog ? 'catalog' : 'folder'})`, async () => {
  const result = await start({catalog});
  assert.equal(result.S.images[0].grade.exposure, 1);
  assert.equal(result.S.images[0].rating, 5);
  assert.deepEqual(plain(result.S.images[0].keywords), ['Trip']);
  assert.equal(result.S.images[0].stateLoaded, true);
  assert.equal(result.saved.grade.exposure, 1);
  assert.equal(result.S.images[0].recoverySourceKey, 'revision');
});
test('equal saved pixels do not discard unacknowledged history', async () => {
  const result = await start({savedExposure: 1});
  assert.equal(result.prompts.length, 1); assert.equal(result.sends.length, 1);
  assert.deepEqual(plain(result.saved._recoveryHistory), {grade: {exposure: 1}});
});
test('fully acknowledged pixels and history clean up without replay', async () => {
  const result = await start({savedExposure: 1, history: {grade: {exposure: 1}}});
  assert.equal(result.prompts.length, 0); assert.equal(result.sends.length, 0);
  assert.equal(result.removals.length, 1);
});
test('failed discard cleanup still opens saved edits and retains the journal', async () => {
  const result = await start({restore: false, removeFails: true});
  assert.equal(result.S.images[0].grade.exposure, 0);
  assert.equal(result.records.length, 1); assert.match(result.toasts[0], /cleanup/);
});
test('source changes during the dialog cannot become unguarded writes', async () => {
  const result = await start({replaceDuringPrompt: true});
  assert.equal(result.saved.grade.exposure, 0); assert.equal(result.records.length, 1);
  assert.equal(result.S.images[0].grade.exposure, 0);
  assert.equal(result.S.images[0].hasEdits, false);
  result.queue.enqueue('photo.RAW', {state: {name: 'photo.RAW', grade: {exposure: 2}}});
  await assert.rejects(result.queue.retry());
  assert.equal(result.queue.getPending('photo.RAW').expectedRecoverySourceKey, 'revision');
});
test('different source revisions leave the saved image and recovery untouched', async () => {
  const result = await start({key: 'older-revision'});
  assert.equal(result.prompts.length, 0); assert.equal(result.removals.length, 0);
  assert.equal(result.S.images[0].grade.exposure, 0);
});
test('legacy drafts require explicit recovery and use the new complete identity guard', async () => {
  const result = await start({key: 'legacy-revision', legacyKey: 'legacy-revision'});
  assert.equal(result.prompts.length, 1);
  assert.equal(result.prompts[0][0].legacyIdentity, true);
  assert.equal(result.sends[0].expectedRecoverySourceKey, 'revision');
  assert.equal(result.saved.grade.exposure, 1);
});
test('legacy recovery cannot apply a draft after replacement during the dialog', async () => {
  const result = await start({key: 'legacy-revision', legacyKey: 'legacy-revision', replaceDuringPrompt: true});
  assert.equal(result.saved.grade.exposure, 0);
  assert.equal(result.S.images[0].grade.exposure, 0);
  assert.equal(result.S.images[0].hasEdits, false);
  assert.equal(result.records.length, 1);
  assert.equal(result.sends[0].expectedRecoverySourceKey, 'revision');
});
test('declining a legacy recovery keeps saved edits unchanged', async () => {
  const result = await start({key: 'legacy-revision', legacyKey: 'legacy-revision', restore: false});
  assert.equal(result.sends.length, 0);
  assert.equal(result.saved.grade.exposure, 0);
});
test('navigation cannot apply rejected recovery state to the replacement original', async () => {
  const result = await start({replaceDuringPrompt: true});
  await result.go(0);
  assert.equal(result.S.images[0].grade.exposure, 0);
  assert.equal(result.S.images[0].hasEdits, false);
  assert.equal(result.queue.getPending('photo.RAW').expectedRecoverySourceKey, 'revision');
});
test('retry refreshes recovered controls only after the retained draft is acknowledged', async () => {
  const result = await start({failSends: 1, key: 'legacy-revision', legacyKey: 'legacy-revision'});
  await result.go(0);
  assert.equal(result.S.images[0].grade.exposure, 0);
  await result.retry();
  assert.equal(result.saved.grade.exposure, 1);
  assert.equal(result.S.images[0].grade.exposure, 1);
  assert.equal(result.S.images[0].stateLoaded, true);
  assert.equal(result.S.images[0].hasEdits, true);
  assert.equal(result.S.images[0].recoverySourceKey, 'revision');
  assert.equal(result.reconciliations.length, 1);
  assert.equal(result.records.length, 0);
  assert.equal(result.queue.getPending('photo.RAW'), null);
});
test('retry of an ordinary optimistic edit does not run recovery reconciliation', async () => {
  const result = await start({restore: false});
  result.S.images[0].grade = {exposure: 3};
  result.failNextSave();
  result.queue.enqueue('photo.RAW', {state: {name: 'photo.RAW', grade: {exposure: 3}}}, {immediate: true});
  await assert.rejects(result.queue.flush());
  await result.go(0);
  assert.equal(result.S.images[0].grade.exposure, 3);
  await result.retry();
  assert.equal(result.saved.grade.exposure, 3);
  assert.equal(result.S.images[0].grade.exposure, 3);
  assert.equal(result.reconciliations.length, 0);
});
test('failed refresh after recovery acknowledgement stays retryable without resending the draft', async () => {
  const result = await start({failSends: 1, failReads: 1});
  await result.retry();
  assert.equal(result.saved.grade.exposure, 1);
  assert.equal(result.S.images[0].grade.exposure, 0);
  assert.equal(result.records.length, 0);
  assert.match(result.recoveryIssue.message, /display could not refresh/);
  const sent = result.sends.length;
  await result.retry();
  assert.equal(result.S.images[0].grade.exposure, 1);
  assert.equal(result.sends.length, sent);
  assert.equal(result.recoveryIssue, null);
});
