import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
test('photo actions follow empty, loaded, navigating, and emptied library states', () => {
  const nodes = new Map();
  const state = { editingName: null, masks: [], heals: [] };
  let photo = null;
  const context = vm.createContext({
    S: state, cur: () => photo, ENHANCE: null,
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
  });
  vm.runInContext(source.slice(source.indexOf('function photoReadyForEditing()'),
    source.indexOf('function updateTransferActions()')), context);
  const check = (disabled) => {
    context.syncPhotoActions();
    for (const id of ['resetEdit', 'autoBtn', 'zoomFit', 'zoom1', 'beforeBtn', 'wbBtn', 'clipBtn', 'versionCreate']) {
      assert.equal(nodes.get(id).disabled, disabled, id);
    }
    for (const id of ['editPane', 'filmPane', 'cropPane', 'maskPane', 'healPane']) {
      assert.equal(nodes.get(id).inert, disabled, id);
    }
  };
  check(true);
  photo = { name: 'A' }; state.editingName = 'A';
  check(false);
  assert.equal(nodes.get('maskReset').disabled, true);
  assert.equal(nodes.get('healReset').disabled, true);
  state.masks = [{}]; state.heals = [{}];
  check(false);
  assert.equal(nodes.get('maskReset').disabled, false);
  assert.equal(nodes.get('healReset').disabled, false);
  photo = { name: 'B' };
  check(true);
  state.editingName = 'B';
  check(false);
  photo = null;
  check(true);
  assert.equal(nodes.get('maskReset').disabled, true);
  assert.equal(nodes.get('healReset').disabled, true);
});

test('a second overflow trigger click closes its menu while a context click can reopen it', () => {
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { attrs: {}, style: {}, offsetWidth: 240, offsetHeight: 180,
        classList: { contains: name => classes.has(name), add: name => classes.add(name), remove: name => classes.delete(name) },
        setAttribute(name, value) { this.attrs[name] = value; },
      });
    }
    return nodes.get(id);
  };
  const context = vm.createContext({ $: element, closeFolderMenu() {}, updateTransferActions() {}, innerWidth: 1000, innerHeight: 800 });
  vm.runInContext(source.slice(source.indexOf('function closeActionMenus()'),
    source.indexOf("$('localLibraryMenuBtn').onclick")), context);
  const anchor = { getBoundingClientRect: () => ({ right: 500, bottom: 100 }) };
  context.openActionMenu('localLibraryMenu', anchor);
  assert.equal(element('localLibraryMenuBtn').attrs['aria-expanded'], 'true');
  context.openActionMenu('localLibraryMenu', anchor);
  assert.equal(element('localLibraryMenuBtn').attrs['aria-expanded'], 'false');
  assert.equal(element('localLibraryMenu').attrs['aria-hidden'], 'true');
  context.openActionMenu('libraryMenu', null, { clientX: 100, clientY: 200 });
  context.openActionMenu('libraryMenu', null, { clientX: 150, clientY: 250 });
  assert.equal(element('libraryMenu').attrs['aria-hidden'], 'false');
  assert.equal(element('libraryMenu').style.top, '250px');
});
