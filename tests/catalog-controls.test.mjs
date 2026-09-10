import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {t as tr, tn as trn, useCatalog} from '../web/i18n.js';
const source = readFileSync(new URL('../web/catalog-ui.js', import.meta.url), 'utf8');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function harness(kind, {post: send, get: fetch} = {}) {
  const nodes = new Map(), calls = [], timers = new Map();
  let timer = 0;
  const document = {body: {dataset: {}}, activeElement: null};
  class Element {
    constructor(id) { this.id = id; this.value = ''; this.disabled = false; this.hidden = false;
      this.textContent = ''; this.handlers = {}; this.attributes = {}; this.inputs = [];
      const classes = new Set();
      this.classList = {add: c => classes.add(c), remove: c => classes.delete(c),
        contains: c => classes.has(c), toggle: (c, value) => value ? classes.add(c) : classes.delete(c)};
    }
    set innerHTML(value) {
      this.html = value;
      this.inputs = [...value.matchAll(/<input[^>]*data-source="([^"]*)"[^>]*>/g)]
        .map(match => ({dataset: {source: match[1]}, checked: /\schecked(?:\s|>)/.test(match[0]), disabled: false}));
    }
    get innerHTML() { return this.html || ''; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    dispatchEvent(event) { return this.handlers[event.type]?.(event); }
    setAttribute(key, value) { this.attributes[key] = value; }
    querySelectorAll() { return this.inputs; }
    focus() { document.activeElement = this; }
  }
  const ids = kind === 'ingest' ? ['ingestDialog', 'ingestOpen', 'ingestStart2', 'ingestCancel',
    'ingestSource', 'ingestDest', 'ingestFolderTemplate', 'ingestFilenameTemplate', 'ingestCustom',
    'ingestStart', 'ingestBackup', 'ingestVerify', 'ingestDuplicates', 'ingestChoose', 'ingestChooseDest',
    'ingestChooseBackup', 'ingestScan', 'ingestSelectAll', 'ingestSelectNone', 'ingestPrevious', 'ingestNext',
    'ingestGrid', 'ingestStatus', 'ingestSelection', 'ingestPage', 'ingestExample'] : kind === 'watch'
    ? ['watchDialog', 'watchOpen', 'watchDestRow', 'watchMode', 'watchSave', 'watchPath', 'watchDest',
      'watchDelete', 'watchPreset', 'watchId', 'watchExisting', 'watchExistingRow', 'watchName',
      'watchRecursive', 'watchFollow', 'watchStatus', 'watchPill', 'watchCancel', 'watchChoose', 'watchChooseDest']
    : kind === 'sidecar-dialog'
    ? ['importSidecarsBtn', 'sidecarDialog', 'sidecarOptMetadata', 'sidecarOptDevelop', 'sidecarOptCrop',
      'sidecarConflict', 'sidecarCancel', 'sidecarRun', 'catalogResultDialog', 'catalogResultTitle', 'catalogResultBody', 'catalogResultClose']
    : ['catalogBackup', 'catalogDuplicates', 'importSidecarsBtn', 'catalogResultDialog',
      'catalogResultTitle', 'catalogResultBody', 'catalogResultClose', 'localLibraryMenuBtn'];
  for (const id of ids) nodes.set(id, new Element(id));
  const context = vm.createContext({document, tr, trn, Event: class {constructor(type) { this.type = type; }},
    setTimeout(fn) { const id = ++timer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() {}, clearInterval() {},
  });
  vm.runInContext(source.replace(/^import .*;$/gm, '').replace('export function', 'function'), context);
  const ui = context.createCatalogUI({el: id => nodes.get(id), toast() {}, sendNative: () => false,
    post: async (path, body) => { calls.push({path, body: JSON.parse(JSON.stringify(body))}); return send ? send(path, body) : {ok: true}; },
    get: async path => fetch ? fetch(path) : path === '/api/presets' ? [] : {watches: []},
  });
  const h = {ui, calls, nodes, el: id => nodes.get(id), document,
    click: id => nodes.get(id).handlers.click?.({}),
    async timers() { const callbacks = [...timers.values()]; timers.clear(); for (const fn of callbacks) await fn(); },
  };
  return h;
}
const plan = count => ({items: Array.from({length: count}, (_, i) => ({source: `/card/${i}.jpg`,
  destination: `/dest/${i}.jpg`, name: `${i}.jpg`, size: 1000})), total: count, bytes: count * 1000});
const ingestHarness = (count, overrides = {}) => {
  const h = harness('ingest', {
    post: async path => path === '/api/ingest/scan' ? {plan: plan(count)} : {jobId: 'import-123'},
    get: async path => path.startsWith('/api/jobs/') ? {state: 'running', total: count, result: {copied: 0}} : {watches: []},
    ...overrides,
  });
  h.ui.setIngestField('ingestSource', '/card'); h.ui.setIngestField('ingestDest', '/dest');
  return h;
};

test('a 201-photo scan submits the full selection, not just the first preview page', async () => {
  const h = ingestHarness(201); await h.click('ingestScan');
  assert.equal(h.el('ingestGrid').inputs.length, 200);
  assert.match(h.el('ingestStatus').textContent, /201 of 201 photos selected/);
  await h.click('ingestStart2');
  assert.equal(h.calls.at(-1).body.plan.items.length, 201);
  assert.equal(h.calls.at(-1).body.plan.total, 201);
});

test('all pages are reachable and an unchecked photo stays excluded after changing pages', async () => {
  const h = ingestHarness(201); await h.click('ingestScan'); await h.click('ingestNext');
  assert.equal(h.el('ingestGrid').inputs.length, 1);
  const last = h.el('ingestGrid').inputs[0]; last.checked = false;
  h.el('ingestGrid').handlers.change({target: last});
  await h.click('ingestPrevious'); await h.click('ingestNext');
  assert.equal(h.el('ingestGrid').inputs[0].checked, false);
  await h.click('ingestStart2');
  assert.equal(h.calls.at(-1).body.plan.items.length, 200);
  assert.equal(h.calls.at(-1).body.plan.items.some(item => item.source === '/card/200.jpg'), false);
});

test('clearing every photo disables Import and never posts an empty plan', async () => {
  const h = ingestHarness(201); await h.click('ingestScan'); await h.click('ingestSelectNone');
  assert.equal(h.el('ingestStart2').disabled, true);
  assert.match(h.el('ingestStatus').textContent, /0 of 201 photos selected/);
  await h.click('ingestStart2'); assert.equal(h.calls.length, 1);
  await h.click('ingestSelectAll'); assert.equal(h.el('ingestStart2').disabled, false);
});

test('changing the reviewed destination, including native picker results, requires a new scan', async () => {
  const h = ingestHarness(3); await h.click('ingestScan');
  h.ui.setIngestField('ingestDest', '/other-destination');
  assert.equal(h.el('ingestStart2').disabled, true); await h.click('ingestStart2');
  assert.equal(h.calls.length, 1); assert.equal(h.el('ingestGrid').inputs.length, 0);
});

test('failed scans clear stale plans and rejected imports remain visible and retryable', async () => {
  let reject = false;
  const h = ingestHarness(3, {post: async path => path === '/api/ingest/scan'
    ? reject ? {error: 'Card disconnected'} : {plan: plan(3)} : {error: 'Import already running'}});
  await h.click('ingestScan'); await h.click('ingestStart2');
  assert.match(h.el('ingestStatus').textContent, /already running/);
  assert.equal(h.el('ingestStart2').disabled, false);
  reject = true; await h.click('ingestScan');
  assert.match(h.el('ingestStatus').textContent, /disconnected/);
  assert.equal(h.el('ingestStart2').disabled, true);
});

test('Cancel targets the active job and waits for its final verified-copy count', async () => {
  let cancelled = false;
  const h = ingestHarness(3, {get: async path => path.startsWith('/api/jobs/')
    ? {state: cancelled ? 'cancelled' : 'running', total: 3, result: {copied: cancelled ? 1 : 0}}
    : {watches: []}});
  await h.click('ingestOpen'); await h.click('ingestScan'); await h.click('ingestStart2');
  assert.equal(h.el('ingestScan').disabled, true);
  await h.click('ingestCancel');
  assert.equal(h.calls.at(-1).path, '/api/jobs/import-123/cancel');
  assert.equal(h.el('ingestDialog').classList.contains('on'), true);
  assert.equal(h.el('ingestCancel').disabled, true);
  cancelled = true; await h.timers();
  assert.match(h.el('ingestStatus').textContent, /Import cancelled\. Copied 1\/3/);
  assert.equal(h.el('ingestCancel').disabled, false);
  assert.equal(h.el('ingestStart2').disabled, true, 'a completed or cancelled plan must be rescanned');
});

test('backup, duplicate and sidecar outcomes use a visible dialog and preserve filenames as text', async () => {
  const h = harness('results', {post: async path => path.endsWith('backup') ? {archive: '/backup.zip'}
    : {read: 3, applied: 2, missing: 1}, get: async path => path.endsWith('duplicates')
      ? {groups: [{files: [{relpath: '<img src=x>'}, {relpath: 'copy.jpg'}]}]} : {watches: []}});
  await h.click('catalogBackup'); assert.match(h.el('catalogResultBody').textContent, /backup.zip/);
  assert.equal(h.el('catalogResultDialog').attributes['aria-hidden'], 'false');
  await h.click('catalogDuplicates'); assert.match(h.el('catalogResultBody').textContent, /<img src=x> = copy.jpg/);
  assert.equal(h.el('catalogResultBody').innerHTML, '');
  await h.click('importSidecarsBtn'); assert.match(h.el('catalogResultBody').textContent, /Sidecars read: 3\. Applied: 2/);
  await h.click('catalogResultClose'); assert.equal(h.el('catalogResultDialog').attributes['aria-hidden'], 'true');
});

test('catalog API errors appear as errors rather than false success or no duplicates', async () => {
  const h = harness('results', {post: async () => ({error: 'Catalog unavailable'}),
    get: async path => path.endsWith('duplicates') ? {error: 'Could not search'} : {watches: []}});
  await h.click('catalogBackup'); assert.equal(h.el('catalogResultBody').textContent, 'Catalog unavailable');
  await h.click('importSidecarsBtn'); assert.equal(h.el('catalogResultBody').textContent, 'Catalog unavailable');
  await h.click('catalogDuplicates'); assert.equal(h.el('catalogResultBody').textContent, 'Could not search');
});

test('translated result dialogs preserve archive paths and native errors as text', async () => {
  useCatalog('fr', {messages: {
    'Back up catalog': 'Sauvegarder le catalogue',
    'Backed up to {resultArchive}': 'Sauvegarde dans {resultArchive}',
    'Find duplicates': 'Chercher les doublons',
    'Export': 'Exporter',
  }});
  try {
    const h = harness('results', {post: async () => ({archive: '/Export/<img src=x> {resultArchive}.zip'}),
      get: async path => path.endsWith('duplicates') ? {error: 'Export'} : {watches: []}});
    await h.click('catalogBackup');
    assert.equal(h.el('catalogResultTitle').textContent, 'Sauvegarder le catalogue');
    assert.equal(h.el('catalogResultBody').textContent, 'Sauvegarde dans /Export/<img src=x> {resultArchive}.zip');
    assert.equal(h.el('catalogResultBody').innerHTML, '');
    await h.click('catalogDuplicates');
    assert.equal(h.el('catalogResultTitle').textContent, 'Chercher les doublons');
    assert.equal(h.el('catalogResultBody').textContent, 'Export');
  } finally { useCatalog('en', {messages: {}}); }
});

test('blank watched folders and copy destinations cannot be saved; server errors keep the form open', async () => {
  const h = harness('watch', {post: async () => ({error: 'Folder unavailable'})});
  await tick(); await h.click('watchOpen');
  assert.equal(h.el('watchSave').disabled, true); await h.click('watchSave'); assert.equal(h.calls.length, 0);
  h.ui.setIngestField('watchPath', '/camera');
  assert.equal(h.el('watchSave').disabled, false);
  h.el('watchMode').value = 'ingest'; h.el('watchMode').handlers.change();
  assert.equal(h.el('watchSave').disabled, true);
  h.ui.setIngestField('watchDest', '/library'); await h.click('watchSave');
  assert.equal(h.el('watchDialog').classList.contains('on'), true);
  assert.equal(h.el('watchStatus').textContent, 'Folder unavailable');
  assert.equal(h.el('watchSave').disabled, false);
});

test('every watch is reachable and the watching pill edits the enabled watch it names', async () => {
  const watches = [{id: 'paused', name: 'Paused', path: '/paused', enabled: false},
    {id: 'second', name: 'Camera', path: '/camera', enabled: true},
    {id: 'third', name: 'Studio', path: '/studio', enabled: true}];
  const h = harness('watch', {get: async path => path === '/api/presets' ? [] : {watches, status: []},
    post: async (_path, body) => ({ok: true, watches: watches.filter(watch => watch.id !== body.id)})});
  await tick(); await h.click('watchOpen');
  assert.equal(h.el('watchExistingRow').hidden, false);
  assert.match(h.el('watchExisting').innerHTML, /value="third"/);
  h.el('watchExisting').value = 'third'; h.el('watchExisting').handlers.change();
  assert.equal(h.el('watchPath').value, '/studio');
  await h.click('watchDelete'); assert.equal(h.calls.at(-1).body.id, 'third');
  await h.click('watchPill'); assert.equal(h.el('watchId').value, 'second');
});

test('sidecar dialog allows selecting develop and crop settings and conflict strategy', async () => {
  const h = harness('sidecar-dialog', {
    post: async (path, body) => ({ ok: true, read: 5, applied: 5, missing: 0 })
  });
  h.el('sidecarOptMetadata').checked = true;
  h.el('sidecarOptDevelop').checked = false;
  h.el('sidecarOptCrop').checked = false;
  h.el('sidecarConflict').value = 'skip-existing';

  await h.click('importSidecarsBtn');
  assert.equal(h.el('sidecarDialog').classList.contains('on'), true);

  h.el('sidecarOptDevelop').checked = true;
  h.el('sidecarOptCrop').checked = true;
  h.el('sidecarConflict').value = 'overwrite';

  await h.click('sidecarRun');
  assert.equal(h.el('sidecarDialog').classList.contains('on'), false);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].path, '/api/import/sidecars');
  assert.deepEqual(h.calls[0].body, {
    apply: { metadata: true, develop: true, crop: true },
    conflict: 'overwrite'
  });
});
