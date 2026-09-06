import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const read = file => readFileSync(new URL(`../web/${file}`, import.meta.url), 'utf8');
const source = read('app.js');
const initialize = source.match(/^async function initializeEditRecovery\([^]*?^}/m)[0];
const moduleFor = async file => import(`data:text/javascript;base64,${Buffer.from(read(file)).toString('base64')}`);
const {createEditSaveQueue} = await moduleFor('edit-save-queue.js');
const {recoveryAcknowledged} = await moduleFor('edit-recovery.js');
const plain = value => JSON.parse(JSON.stringify(value));

async function start({catalog = true, savedExposure = 0, history = null, restore = true,
  removeFails = false, replaceDuringPrompt = false, key = 'revision'} = {}) {
  const state = {name: 'photo.RAW', grade: {exposure: 1}, rating: 5, keywords: ['Trip']};
  let records = [{name: state.name, token: 'draft', payload: {state, sourceKey: key,
    history: {label: 'Exposure', state: {grade: {exposure: 1}}}}}];
  let saved = {grade: {exposure: savedExposure}, rating: 5, keywords: ['Trip'],
    _recoverySourceKey: 'revision', _recoveryHistoryAvailable: catalog, _recoveryHistory: history};
  const removals = [], sends = [], prompts = [], toasts = [];
  const journal = {list: async () => plain(records),
    put: async (name, token, payload) => {records = [{name, token, payload: plain(payload)}];},
    remove: async (name, token) => {if (removeFails) throw new Error('read only');
      removals.push(token); records = records.filter(item => item.token !== token);}};
  const queue = createEditSaveQueue({journal, send: async (name, payload) => {
    sends.push(plain(payload));
    if (payload.expectedRecoverySourceKey !== saved._recoverySourceKey) throw new Error('source changed');
    saved = {...saved, ...plain(payload.state), _recoveryHistory: plain(payload.history?.state)};
  }});
  const S = {catalogEnabled: catalog, images: [{name: state.name, grade: {exposure: 0}, rating: 0,
    keywords: [], stateLoaded: !catalog, hasEdits: false}]};
  const context = {window: {localStorage: {}}, nativeBridge: () => null,
    nativeJournalRequest: () => {}, createEditRecovery: () => journal,
    editRecovery: null, editRecoveryReady: false, updateEditRecoveryHealth: () => {}, recoveryAcknowledged, S,
    getJSON: async () => plain(saved), toast: message => toasts.push(message),
    chooseEditRecovery: async items => {prompts.push(items); if (replaceDuringPrompt) saved._recoverySourceKey = 'replacement'; return restore;},
    editSaveQueue: queue, normalizeLibraryImage: value => value,
    flushEditSaves: async () => {try {await queue.flush(); return true;} catch {return false;}},
  };
  vm.runInNewContext(initialize + '\nglobalThis.initialize = initializeEditRecovery;', context);
  await context.initialize({catalog: catalog ? {path: '/catalog'} : null, folder: '/photos'});
  return {S, saved, records, removals, sends, prompts, toasts, queue};
}
for (const catalog of [false, true]) test(`startup restoration updates displayed edits and metadata (${catalog ? 'catalog' : 'folder'})`, async () => {
  const result = await start({catalog});
  assert.equal(result.S.images[0].grade.exposure, 1);
  assert.equal(result.S.images[0].rating, 5);
  assert.deepEqual(plain(result.S.images[0].keywords), ['Trip']);
  assert.equal(result.S.images[0].stateLoaded, true);
  assert.equal(result.saved.grade.exposure, 1);
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
  result.queue.enqueue('photo.RAW', {state: {name: 'photo.RAW', grade: {exposure: 2}}});
  await assert.rejects(result.queue.retry());
  assert.equal(result.queue.getPending('photo.RAW').expectedRecoverySourceKey, 'revision');
});
test('different source revisions leave the saved image and recovery untouched', async () => {
  const result = await start({key: 'older-revision'});
  assert.equal(result.prompts.length, 0); assert.equal(result.removals.length, 0);
  assert.equal(result.S.images[0].grade.exposure, 0);
});
