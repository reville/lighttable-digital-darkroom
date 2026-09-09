import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {photoHasEdits} from '../web/library-filters.js';
import {createPhotoDisplayStatus} from '../web/photo-display-status.js';

const source = name => readFileSync(new URL(`../web/${name}`, import.meta.url), 'utf8');
const app = source('app.js');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};

function libraryHarness() {
  const values = {filter: 'all', ratingFilter: 'all', kindFilter: 'all', labelFilter: 'all',
    editFilter: 'all', search: 'New', sort: 'name', keywordInput: 'New'};
  const nodes = new Map();
  const element = id => {
    if (id === 'keywordSuggestions') return null;
    if (!nodes.has(id)) nodes.set(id, {children: [], get value() { return values[id] || ''; },
      set value(value) { values[id] = value; },
      append(...items) { this.children.push(...items); }, setAttribute() {},
      appendChild(item) { this.children.push(item); },
      replaceChildren(...items) { this.children = items; }});
    return nodes.get(id);
  };
  const photo = {name: 'photo.jpg', keywords: []};
  const state = {images: [photo], library: {stacks: [], collections: []}, activeCollection: '',
    activeFolder: '', includeSubfolders: true, cull: {review: 'all', on: {}, revision: 0}};
  const saves = [];
  const pending = new Map();
  const context = vm.createContext({CULL_BATCH: {noteFlagChange() {}}, S: state, $: element, cur: () => photo, APP_PREFS: {},
    LIBRARY_FILTERS: {types: () => [], metadata: () => ({}), hideUndisplayable: () => false},
    PHOTO_DISPLAY_STATUS: createPhotoDisplayStatus(), CULL_SELECT: [], CULL_REJECT: [],
    pairViewPreference: () => 'both', collapsePairs: list => list, photoMatchesRules: () => true,
    matchesCullReview: () => true, matchesLibraryFilters: () => true,
    photoHasEdits,
    photoMatchesQuery: (image, query) => !query || image.keywords.some(keyword => keyword.includes(query)),
    inFolderScope: () => true, cleanLabel: value => value || 'none',
    linkedMetadataTargets: images => images, tr: text => text, KEYWORD_BATCH: null,
    document: {createElement: () => element(Symbol())},
    saveState: () => { saves.push(structuredClone(photo)); },
    editSaveQueue: {getPending: name => pending.get(name)},
    enqueuePhotoPatch(image, patch) { pending.set(image.name, {...pending.get(image.name), ...patch}); },
    refreshLists() {}, go() { throw new Error('Unexpected navigation'); }});
  vm.runInContext(app.slice(app.indexOf('function activeCollection()'), app.indexOf('const pairOverrides ='))
    + '\nconst pairOverrides = new Map();\n'
    + app.slice(app.indexOf('function visible()'), app.indexOf('function inFolderScope('))
    + '\nlet _stripKey = "", _gridKey = "";\n'
    + app.slice(app.indexOf('function refreshFilteredView()'), app.indexOf('function setViewMode('))
    + app.slice(app.indexOf('function renderKeywords()'), app.indexOf("$('keywordAdd').onclick"))
    + app.slice(app.indexOf('function applyKeywordChanges('), app.indexOf('KEYWORD_BATCH = installKeywordBatch(')), context);
  return {context, photo, element, saves, pending, names: () => Array.from(context.visible(), image => image.name)};
}

test('single-photo keyword add and remove update active search membership immediately', () => {
  const h = libraryHarness();
  assert.deepEqual(h.names(), []);
  h.context.addKeyword();
  assert.deepEqual(h.names(), ['photo.jpg']);
  assert.deepEqual(h.saves.at(-1).keywords, ['New']);
  h.element('keywordList').children[0].children[1].onclick();
  assert.deepEqual(h.names(), []);
  assert.deepEqual(h.saves.at(-1).keywords, []);
});

test('the first saved edit refreshes Edited membership without navigating or interrupting its save', () => {
  const h = libraryHarness(), c = h.context;
  h.element('filter').value = 'edited'; h.element('search').value = '';
  const saves = []; let paints = 0;
  Object.assign(c, {window: {}, readControls() {}, cloneValue: structuredClone,
    reconcilePresetAdjustment: () => null, presetEditState: () => ({}),
    editHistorySnapshot: () => JSON.stringify({grade: c.S.grade}),
    PANE_STEP_LABELS: {editPane: 'Edit'}, refreshLists: () => { paints++; },
    flushEditSaves: () => Promise.resolve(true)});
  c.editSaveQueue.enqueue = (name, payload) => saves.push({name, payload});
  Object.assign(c.S, {editingName: h.photo.name, activePane: 'editPane', grade: {exposure: 1}});
  vm.runInContext('let _lastHistorySnapshot = "";\n'
    + app.slice(app.indexOf('function saveState('), app.indexOf('/* ------------------------------------------------------------- filmstrip */')), c);
  assert.deepEqual(h.names(), []);
  c.saveState();
  assert.deepEqual(h.names(), ['photo.jpg']);
  assert.equal(paints, 1);
  assert.equal(saves[0].payload.state.grade.exposure, 1);
  assert.equal(saves[0].payload.history.state.grade.exposure, 1);
  c.S.grade = {exposure: 2}; c.saveState();
  assert.equal(paints, 1, 'later edits with unchanged membership do not repaint the library');
  assert.equal(saves[1].payload.state.grade.exposure, 2);
});

function renameHarness({flush = async () => true, post, changed} = {}) {
  const tree = {handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; }};
  const notices = [], calls = [];
  const context = vm.createContext({tr: text => text, setTimeout, clearTimeout});
  vm.runInContext(source('metadata-panel.js').replace(/^import .*;$/gm, '')
    .replace('export function', 'function'), context);
  context.createMetadataPanel({el: id => id === 'keywordTree' ? tree : null,
    post: async (path, body) => { calls.push({path, body}); return post?.(path, body)
      || {ok: true, changes: [{name: 'photo.jpg', keywords: ['New > Rome']}]}; },
    get: async () => ({keywords: [{id: 1, name: 'New', path: 'New', count: 1}]}),
    toast: text => notices.push(text), askName: async () => 'New', flush,
    onKeywordsChanged: changed});
  const node = {dataset: {id: '1'}, querySelector: () => ({textContent: 'Trip'})};
  return {notices, calls, tree, rename: () => tree.handlers.dblclick({target: {closest: () => node}})};
}

test('rename waits for pending saves and reconciles keywords into edits made while it runs', async () => {
  const library = libraryHarness();
  library.photo.keywords = ['Trip > Rome'];
  library.photo.stateLoadEdits = {keywords: ['Trip > Rome']};
  assert.deepEqual(library.names(), []);
  const saving = deferred(), response = deferred();
  const h = renameHarness({flush: () => saving.promise, post: () => response.promise,
    changed: changes => library.context.applyKeywordChanges(changes)});
  const rename = h.rename(); await tick();
  assert.equal(h.calls.length, 0);
  saving.resolve(true); await tick();
  assert.equal(h.calls.length, 1);
  library.pending.set('photo.jpg', {grade: {exposure: 1}, keywords: ['Trip > Rome']});
  response.resolve({ok: true, changes: [{name: 'photo.jpg', keywords: ['New > Rome']}]});
  await rename;
  assert.deepEqual(Array.from(library.photo.keywords), ['New > Rome']);
  assert.deepEqual(Array.from(library.photo.stateLoadEdits.keywords), ['New > Rome']);
  assert.deepEqual(Array.from(library.pending.get('photo.jpg').keywords), ['New > Rome']);
  assert.deepEqual(library.pending.get('photo.jpg').grade, {exposure: 1});
  assert.deepEqual(library.names(), ['photo.jpg']);
  assert.match(h.tree.innerHTML, /New/);
});

test('a failed pending save prevents rename and remains retryable', async () => {
  let saved = false;
  const h = renameHarness({flush: async () => saved});
  await h.rename();
  assert.equal(h.calls.length, 0);
  assert.match(h.notices.at(-1), /Save pending edits/);
  saved = true; await h.rename();
  assert.equal(h.calls.length, 1);
});

test('failed rename reports the server error without applying photo changes', async () => {
  let applied = 0;
  const h = renameHarness({post: async () => ({error: 'Keyword already exists'}),
    changed: () => { applied++; }});
  await h.rename();
  assert.equal(applied, 0);
  assert.equal(h.notices.at(-1), 'Keyword already exists');
});

test('authoritative keyword events refresh other windows search membership', async () => {
  const h = libraryHarness();
  assert.deepEqual(h.names(), []);
  Object.assign(h.context, {CLIENT_ID: 'viewer', cloneValue: structuredClone,
    invalidateEditedThumbnail() {}, renderVersions() {}});
  vm.runInContext(app.slice(app.indexOf('async function applyServerStateEvent('),
    app.indexOf('async function executeUICommand(')), h.context);
  await h.context.applyServerStateEvent({origin: 'keywords', names: ['photo.jpg'],
    patches: {'photo.jpg': {keywords: ['New > Rome']}}});
  assert.deepEqual(h.names(), ['photo.jpg']);
  assert.equal(h.saves.length, 0, 'an accepted external patch needs no redundant save');
});
