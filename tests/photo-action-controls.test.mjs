import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

for (const scope of ['panel', 'film', 'stages']) {
  for (const enabled of [true, false]) {
    test(`Film ${scope} Reset restores sliders and keeps options with Film ${enabled ? 'on' : 'off'}`, () => {
      const profileSliders = ['wb_temperature', 'wb_tint', 'exposure_ev', 'print_exposure', 'gamma'];
      const stageSliders = ['couplers_amount', 'halation_amount', 'grain_amount', 'glare_amount',
        'camera_diffusion_strength', 'print_preflash', 'print_y_filter_shift', 'print_m_filter_shift',
        'scan_softness', 'scan_sharpness'];
      const defaults = Object.fromEntries([...profileSliders, ...stageSliders].map(key => [key, 1]));
      Object.assign(defaults, { profile_enabled: !enabled, stock: 'default-stock', film_format: '35mm',
        paper: 'default-paper', output_recipe: 'default-output', workflow_mode: 'default-workflow',
        wb_mode: 'camera', development_time: 0, print_development_time: 0, auto_exposure: true,
        paper_locked: false, couplers_on: true, halation_on: true, grain_on: true, glare_on: true,
        scan_sharpen: true, raw_profile: 'default-raw' });
      const params = Object.fromEntries([...profileSliders, ...stageSliders].map(key => [key, 2]));
      Object.assign(params, { profile_enabled: enabled, stock: 'chosen-stock', film_format: '6x6',
        paper: 'chosen-paper', output_recipe: 'chosen-output', workflow_mode: 'chosen-workflow',
        wb_mode: 'custom', development_time: 8, print_development_time: 2, auto_exposure: false,
        paper_locked: true, couplers_on: false, halation_on: false, grain_on: false, glare_on: false,
        scan_sharpen: false, raw_profile: 'chosen-raw' });
      const before = structuredClone(params);
      const grade = { exposure: 0.7, contrast: 0.3 };
      let handler, undo, saved, rendered = 0;
      const button = { dataset: { reset: scope }, addEventListener: (_, callback) => { handler = callback; } };
      const context = vm.createContext({
        S: { params, filmDefaults: defaults, grade }, GRADE_DEFAULTS: { exposure: 0, contrast: 0 },
        $: () => button, cur: () => ({ name: 'photo.jpg' }), tr: text => text, toast() {},
        document: { querySelectorAll: () => [button] },
        pushUndo: () => { undo = structuredClone(params); },
        saveState: () => { saved = structuredClone(params); },
        renderFilm: () => { rendered++; }, syncControls() {}, syncGrade() {}, drawGrade() {},
      });
      vm.runInContext(source.slice(source.indexOf('const RESET_GROUPS ='), source.indexOf('const S =')), context);
      if (scope === 'panel') {
        vm.runInContext(source.slice(source.indexOf("$('resetFilm').onclick ="), source.indexOf("$('zoomIn').onclick")), context);
        button.onclick();
      } else {
        vm.runInContext(source.slice(source.indexOf("document.querySelectorAll('a.reset')"), source.indexOf("$('stars').addEventListener")), context);
        handler({ preventDefault() {}, stopPropagation() {} });
      }
      const resetKeys = scope === 'film' ? profileSliders : scope === 'stages' ? stageSliders : [...profileSliders, ...stageSliders];
      const expected = { ...before, ...Object.fromEntries(resetKeys.map(key => [key, defaults[key]])) };
      assert.deepEqual(params, expected);
      assert.deepEqual(saved, expected);
      assert.deepEqual(undo, before);
      assert.deepEqual(grade, { exposure: 0.7, contrast: 0.3 });
      assert.equal(rendered, 1);
    });
  }
}

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

test('pressing J triggers clipping warning when not disabled', () => {
  const s = source;
  const start = s.indexOf("document.addEventListener('keydown', (e) => {");
  const registration = s.slice(start, s.indexOf("document.addEventListener('keyup'", start));
  let handler;
  let clicked = 0;
  const clipBtn = { disabled: false, click() { clicked++; } };
  const context = vm.createContext({
    document: { querySelector: () => null, addEventListener(_type, fn) { handler = fn; } },
    cur: () => ({ name: 'test.raw' }),
    S: { editingName: 'test.raw', viewMode: 'detail' },
    KEYS: { speed: {}, pick: [], reject: [], unflag: [] },
    LABEL_KEYS: {},
    PHOTO_TOOL_PANES: [],
    $: (id) => id === 'clipBtn' ? clipBtn : null,
  });
  vm.runInContext(registration, context);
  let prevented = false;
  handler({ key: 'j', target: { tagName: 'DIV' }, preventDefault() { prevented = true; } });
  assert.equal(clicked, 1);
  assert.equal(prevented, true);

  // When clipBtn is disabled, J does nothing
  clipBtn.disabled = true;
  prevented = false;
  handler({ key: 'j', target: { tagName: 'DIV' }, preventDefault() { prevented = true; } });
  assert.equal(clicked, 1);
  assert.equal(prevented, false);
});

