import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = (name) => readFileSync(new URL(`../web/${name}`, import.meta.url), 'utf8');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function metadataHarness({ get, post } = {}) {
  const elements = new Map();
  const timers = new Map();
  const writes = [];
  let timerId = 0;
  for (const id of ['iptcTitle', 'iptcCaption']) {
    elements.set(id, { value: '', disabled: false,
      addEventListener(type, fn) { this[type] = fn; } });
  }
  elements.set('infoPane', { classList: { contains: () => true } });
  const context = vm.createContext({
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(source('metadata-panel.js').replace('export function', 'function'), context);
  const panel = context.createMetadataPanel({
    el: (id) => elements.get(id), toast() {},
    get: get || (async () => ({ iptc: {} })),
    post: async (path, body) => {
      writes.push(JSON.parse(JSON.stringify(body)));
      return post ? post(path, body) : { ok: true };
    },
  });
  return { panel, elements, writes,
    input(value) { elements.get('iptcTitle').value = value; elements.get('iptcTitle').input(); },
    async fireTimers() {
      const callbacks = [...timers.values()]; timers.clear();
      callbacks.forEach((fn) => fn()); await tick();
    },
  };
}

test('metadata typed just before navigation is saved to its original photo', async () => {
  const h = metadataHarness();
  await h.panel.refresh('A');
  h.input('Title for A');
  await h.panel.refresh('B');
  h.input('Title for B');
  await h.fireTimers();
  assert.deepEqual(h.writes, [
    { name: 'A', fields: { title: 'Title for A', caption: null } },
    { name: 'B', fields: { title: 'Title for B', caption: null } },
  ]);
});

test('metadata writes stay ordered and returning waits for the pending save', async () => {
  const first = deferred();
  let title = '';
  const h = metadataHarness({
    get: async () => ({ iptc: { title } }),
    post: async (_path, body) => {
      if (body.fields.title === 'first') await first.promise;
      title = body.fields.title; return { ok: true };
    },
  });
  await h.panel.refresh('A');
  h.input('first'); await h.fireTimers();
  h.input('latest'); await h.fireTimers();
  assert.equal(h.writes.length, 1, 'a newer write must not overtake a slow save');
  const returning = h.panel.refresh('A', true);
  first.resolve(); await returning;
  assert.equal(title, 'latest');
  assert.equal(h.elements.get('iptcTitle').value, 'latest');
});

test('late metadata from the previous photo cannot overwrite the current fields', async () => {
  const a = deferred(), b = deferred();
  const h = metadataHarness({ get: (path) => path.endsWith('A') ? a.promise : b.promise });
  const loadingA = h.panel.refresh('A'); await tick();
  const loadingB = h.panel.refresh('B'); await tick();
  assert.equal(h.elements.get('iptcTitle').disabled, true);
  b.resolve({ iptc: { title: 'B title' } }); await loadingB;
  a.resolve({ iptc: { title: 'A title' } }); await loadingA;
  assert.equal(h.elements.get('iptcTitle').value, 'B title');
  assert.equal(h.elements.get('iptcTitle').disabled, false);
});

function selectionHarness() {
  const images = ['A', 'B', 'C'].map((name) => ({ name }));
  const state = { images, idx: 0, msel: new Set() };
  const start = source('app.js').indexOf('async function selectPhotoFromPointer(');
  const implementation = source('app.js').slice(start).split('\n/* -------------------------------------------------------------- prefs */')[0];
  const context = vm.createContext({
    S: state, selectionAnchorName: null, cur: () => images[state.idx],
    visible: () => images, paintSelectionState() {},
    go: async (index) => { state.idx = index; },
  });
  vm.runInContext(implementation, context);
  return { state, click: (index, modifiers = {}) => context.selectPhotoFromPointer(images[index], modifiers) };
}

for (const modifier of ['metaKey', 'ctrlKey']) {
  test(`${modifier} click expands a plain-click selection`, async () => {
    const h = selectionHarness();
    await h.click(0); await h.click(1, { [modifier]: true });
    assert.deepEqual([...h.state.msel], ['A', 'B']);
    await h.click(2, { [modifier]: true });
    assert.deepEqual([...h.state.msel], ['A', 'B', 'C']);
    await h.click(1, { [modifier]: true });
    assert.deepEqual([...h.state.msel], ['A', 'C']);
  });
}

test('plain clicks reset and Shift-click still selects the visible range', async () => {
  const h = selectionHarness();
  await h.click(0); await h.click(2, { shiftKey: true });
  assert.deepEqual([...h.state.msel], ['A', 'B', 'C']);
  await h.click(1);
  assert.equal(h.state.msel.size, 0);
  assert.equal(h.state.idx, 1);
});

function renameHarness() {
  const elements = new Map();
  const document = { activeElement: { focus() {} } };
  const writes = [];
  let selection = ['A'];
  for (const id of ['renameDialog', 'renameTemplate', 'renameCustom', 'renameStart',
    'renamePreview', 'renameCancel', 'renameApply']) {
    elements.set(id, { value: id === 'renameTemplate' ? '{filename}' : '', disabled: false,
      offsetParent: {}, classList: { toggle() {} }, setAttribute() {},
      focus() { document.activeElement = this; }, select() {},
      querySelectorAll() { return ['renameTemplate', 'renameCustom', 'renameStart', 'renameCancel', 'renameApply'].map((key) => elements.get(key)).filter((e) => !e.disabled); },
      addEventListener(type, fn) { this[type] = fn; } });
  }
  const s = source('catalog-ui.js');
  const implementation = s.slice(s.indexOf('  function bindRename() {'), s.indexOf('\n  bindSources();'));
  const context = vm.createContext({ document, el: (id) => elements.get(id),
    ctx: { selection: () => selection }, toast() {},
    post: async (_path, body) => { writes.push(JSON.parse(JSON.stringify(body))); return { ok: true, preview: [], renamed: 1 }; },
  });
  vm.runInContext(implementation, context);
  return { rename: context.bindRename(), elements, document, writes,
    selection(names) { selection = names; },
    key(key, shiftKey = false) {
      const event = { key, shiftKey, stopped: false, prevented: false,
        stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } };
      elements.get('renameDialog').keydown?.(event);
      return event;
    },
  };
}

test('Rename keeps its opening selection even if library selection changes', async () => {
  const h = renameHarness(); h.rename.open(); await tick();
  h.selection(['B']); await h.elements.get('renameApply').click();
  assert.deepEqual(h.writes.map((body) => body.names), [['A'], ['A']]);
});

test('Rename captures shortcuts and wraps focus inside the dialog', async () => {
  const h = renameHarness(); h.rename.open(); await tick();
  assert.equal(h.document.activeElement, h.elements.get('renameTemplate'));
  h.elements.get('renameCancel').focus();
  assert.equal(h.key('5').stopped, true);
  h.elements.get('renameApply').focus();
  assert.equal(h.key('Tab').prevented, true);
  assert.equal(h.document.activeElement, h.elements.get('renameTemplate'));
  h.key('Tab', true);
  assert.equal(h.document.activeElement, h.elements.get('renameApply'));
  assert.equal(h.key('Escape').prevented, true);
});

test('editor shortcuts ignore an open modal even if focus is outside it', () => {
  const s = source('app.js');
  const start = s.indexOf("document.addEventListener('keydown', (e) => {");
  const registration = s.slice(start, s.indexOf("document.addEventListener('keyup'", start));
  let handler, ratings = 0;
  const context = vm.createContext({
    document: { querySelector: () => ({}), addEventListener(_type, fn) { handler = fn; } },
    cur: () => null, S: {}, KEYS: { pick: [], reject: [], unflag: [] },
    setRating() { ratings++; }, LABEL_KEYS: {}, SURVEY: null,
  });
  vm.runInContext(registration, context);
  handler({ key: '5', target: { tagName: 'BUTTON' }, preventDefault() {} });
  assert.equal(ratings, 0);
});
