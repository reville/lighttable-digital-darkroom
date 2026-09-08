import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installFirstRunSetup, shouldShowSetup } from '../web/first-run.js';
import { useCatalog } from '../web/i18n.js';

const empty = { total: 0, images: [], catalog: { sources: [] } };
test('waits for both responses, respects durable completion, and spares existing installations', () => {
  assert.equal(shouldShowSetup(null, empty), false);
  assert.equal(shouldShowSetup({}, null), false);
  assert.equal(shouldShowSetup({}, empty), true);
  assert.equal(shouldShowSetup({}, { total: 7000 }), false);
  assert.equal(shouldShowSetup({}, empty, false), false);
  assert.equal(shouldShowSetup({}, { total: 10 }, true), true);
  for (const status of ['completed', 'skipped']) {
    assert.equal(shouldShowSetup({ firstRunSetup: { version: 1, status } }, empty, true), false);
  }
});

function fixture({ native = false, failSave = false, platform = native ? 'macos' : undefined } = {}) {
  const all = new Map();
  class Element {
    hidden = false; disabled = false; value = ''; textContent = ''; inert = false;
    constructor(id) {
      this.id = id;
      const classes = new Set();
      this.classList = { contains: (key) => classes.has(key), toggle: (key, on) => on ? classes.add(key) : classes.delete(key) };
    }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() { document.activeElement = this; }
    querySelector() { return new Element('heading'); }
    querySelectorAll(selector) { return selector === '[data-setup-page]' ? pages : []; }
  }
  const markup = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  for (const [, id] of markup.matchAll(/id="([^"]+)"/g)) all.set(id, new Element(id));
  const pages = [...markup.matchAll(/data-setup-page="([^"]+)"/g)].map(([, name]) => {
    const node = new Element(name); node.dataset = { setupPage: name }; return node;
  });
  const shell = all.get('appShell');
  globalThis.window = { __LIGHTTABLE_PLATFORM__: platform };
  globalThis.document = { activeElement: null, body: new Element('body'), addEventListener() {} };
  document.body.children = [shell, all.get('firstRunDialog')];
  globalThis.requestAnimationFrame = (fn) => fn();
  const writes = [], actions = [];
  let reloads = 0, catalogOpens = 0;
  const controller = installFirstRunSetup({
    el: (id) => all.get(id),
    post: async (path, body) => { writes.push({ path, body }); return failSave ? { error: 'Disk full' } : { ok: true }; },
    nativeBridge: () => native,
    sendNative: (action) => { actions.push(action); return native; },
    openCatalog: () => { catalogOpens++; },
    reloadLibrary: async () => { reloads++; }, toast() {},
  });
  controller.setPrefs({}); controller.setLibrary(empty);
  return { controller, all, writes, actions, shell,
    open: () => all.get('firstRunDialog').classList.contains('on'),
    click: (id) => all.get(id).onclick(),
    counts: () => ({ reloads, catalogOpens }) };
}

test('native startup waits for shell identity; cancelled folder picker never completes setup', async () => {
  const f = fixture({ native: true });
  assert.equal(f.open(), false);
  f.controller.nativeEvent({ type: 'sources', firstRun: true, photosLibraryImportAvailable: true });
  assert.equal(f.open(), true); assert.equal(f.shell.inert, true);
  await f.click('setupFolderChoose');
  assert.ok(f.actions.includes('setupChooseFolder'));
  f.controller.nativeEvent({ type: 'setupFolderCancelled' });
  assert.equal(f.open(), true); assert.equal(f.writes.length, 0);
  f.controller.nativeEvent({ type: 'setupFolderSelected', path: '/fixture' });
  assert.equal(f.writes.length, 0);
  await f.click('setupDone');
  assert.equal(f.writes.at(-1).body.firstRunSetup.source, 'folder');
  assert.equal(f.writes.at(-1).body.includeSubfolders, true);
  assert.equal(f.open(), false); assert.equal(f.shell.inert, false);
  assert.ok(f.actions.includes('completeFirstRun'));
});

test('skip is saved and remains dismissed on subsequent startup events', async () => {
  const f = fixture();
  assert.equal(f.open(), true);
  await f.click('setupLater');
  assert.equal(f.writes[0].body.firstRunSetup.status, 'skipped');
  f.controller.setLibrary(empty);
  assert.equal(f.open(), false);
  await f.click('setupReopen'); assert.equal(f.open(), true);
});

test('failed persistence keeps the panel open with a recoverable error', async () => {
  const f = fixture({ failSave: true });
  await f.click('setupLater');
  assert.equal(f.open(), true);
  assert.match(f.all.get('setupError').textContent, /Disk full/);
  assert.equal(f.all.get('setupLater').disabled, false);
  assert.equal(f.actions.includes('completeFirstRun'), false);
});

test('Lightroom handoff returns on cancellation; marks completion only after successful import', async () => {
  const f = fixture();
  await f.click('setupLightroom'); await f.click('setupCatalogChoose');
  assert.equal(f.open(), false); assert.equal(f.counts().catalogOpens, 1);
  f.controller.catalogClosed(); assert.equal(f.open(), true);
  assert.equal(f.writes.length, 0);
  await f.click('setupCatalogChoose');
  f.controller.catalogCompleted({ images: 5, matched: 3, unmatched: 2 });
  f.controller.catalogClosed();
  assert.match(f.all.get('setupResultMessage').textContent, /2 originals could not be found/);
  await f.click('setupDone');
  assert.equal(f.writes.at(-1).body.firstRunSetup.source, 'lightroom');
});

test('Photos access failure and cancellation are retryable; imported counts stay honest', async () => {
  const f = fixture({ native: true });
  f.controller.nativeEvent({ type: 'sources', firstRun: true, photosLibraryImportAvailable: true });
  await f.click('setupPhotosAll');
  assert.ok(f.actions.includes('importApplePhotosLibrary'));
  assert.equal(f.all.get('setupLater').hidden, true);
  f.controller.nativeEvent({ type: 'photosLibraryImport', state: 'error', message: 'Photos access denied' });
  assert.equal(f.writes.length, 0);
  assert.match(f.all.get('setupError').textContent, /access denied/);
  await f.click('setupPhotosAll');
  f.controller.nativeEvent({ type: 'photosLibraryImport', state: 'running', completed: 4, total: 10 });
  assert.equal(f.all.get('setupPhotosProgress').value, 4);
  await f.click('setupPhotosCancel');
  assert.ok(f.actions.includes('cancelApplePhotosLibraryImport'));
  f.controller.nativeEvent({ type: 'photosLibraryImport', state: 'cancelled' });
  assert.equal(f.writes.length, 0);
  f.controller.nativeEvent({ type: 'photosLibraryImport', state: 'completed', imported: 8, existing: 2, failures: 1 });
  assert.match(f.all.get('setupResultMessage').textContent, /8 originals imported · 2 already added · 1 could not/);
  await f.click('setupDone');
  assert.equal(f.writes.at(-1).body.firstRunSetup.source, 'photos');
});

test('browser folder flow adds source through server before completing', async () => {
  const f = fixture();
  f.all.get('setupFolderPath').value = '/fixture/photos';
  await f.click('setupFolderChoose');
  assert.equal(f.writes[0].path, '/api/catalog/sources');
  assert.equal(f.counts().reloads, 1);
  assert.equal(f.writes.length, 1);
  await f.click('setupDone');
  assert.equal(f.writes[1].body.firstRunSetup.source, 'folder');
});

test('translated folder setup preserves user paths and persisted source identifiers', async () => {
  useCatalog('fr', { messages: {
    'Add folder': 'Ajouter un dossier',
    'Your folder is ready': 'Votre dossier est prêt',
    'Photos stay in their current folder. You can add more folders whenever you like.':
      'Les photos restent dans leur dossier actuel. Vous pouvez en ajouter d’autres.',
    'Export': 'Exporter',
  } });
  try {
    const f = fixture();
    assert.equal(f.all.get('setupFolderChoose').textContent, 'Ajouter un dossier');
    f.all.get('setupFolderPath').value = '/photos/Export {name}';
    await f.click('setupFolderChoose');
    assert.deepEqual(f.writes[0], { path: '/api/catalog/sources',
      body: { action: 'add', path: '/photos/Export {name}' } });
    assert.equal(f.all.get('setupHeading-result').textContent, 'Votre dossier est prêt');
    await f.click('setupDone');
    assert.equal(f.writes[1].body.firstRunSetup.source, 'folder');
    assert.equal(f.writes[1].body.firstRunSetup.status, 'completed');
  } finally { useCatalog('en', { messages: {} }); }
});

test('Photos onboarding uses locale plural categories while preserving native error text', () => {
  const one = '{count} photo imported.';
  useCatalog('ar', { messages: { 'Your photos are ready': 'صورك جاهزة', 'Export': 'تصدير' },
    plurals: { [one]: { one: 'one {count}', two: 'two {count}', few: 'few {count}',
      many: 'many {count}', other: 'other {count}', zero: 'zero {count}' } } });
  try {
    const f = fixture({ native: true });
    f.controller.nativeEvent({ type: 'sources', firstRun: true });
    for (const [count, category] of [[1, 'one'], [2, 'two'], [3, 'few'], [11, 'many']]) {
      f.controller.nativeEvent({ type: 'photosImported', count });
      assert.equal(f.all.get('setupHeading-result').textContent, 'صورك جاهزة');
      assert.match(f.all.get('setupResultMessage').textContent, new RegExp(`^${category} `));
    }
    f.controller.nativeEvent({ type: 'error', message: 'Export' });
    assert.equal(f.all.get('setupError').textContent, 'Export');
  } finally { useCatalog('en', { messages: {} }); }
});


test('Linux empty-library setup does not wait for Mac-only first-run messages', async () => {
  const f = fixture({ native: true, platform: 'linux' });
  assert.equal(f.open(), true);
  f.controller.nativeEvent({ type: 'sources', sources: [] });
  assert.equal(f.open(), true);
  assert.equal(f.all.get('setupPhotosSelected').hidden, true);
  assert.equal(f.all.get('setupPhotosAll').disabled, true);
  await f.click('setupLater');
  f.controller.nativeEvent({ type: 'sources', sources: [] });
  assert.equal(f.open(), false);
});

test('stopped selected import keeps successful photos and reports the stop', () => {
  const f = fixture({ native: true });
  f.controller.nativeEvent({ type: 'sources', firstRun: true, photosLibraryImportAvailable: true });
  f.controller.nativeEvent({ type: 'photosImported', count: 2, failures: 1, cancelled: true });
  assert.equal(f.all.get('setupHeading-result').textContent, 'Import stopped');
  assert.match(f.all.get('setupResultMessage').textContent, /2 photos imported/);
  assert.match(f.all.get('setupResultMessage').textContent, /1 could not be imported/);
  assert.equal(f.open(), true);
});
