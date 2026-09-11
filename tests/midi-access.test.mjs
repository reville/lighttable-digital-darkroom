// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {t as tr} from '../web/i18n.js';

const midiSource = readFileSync(new URL('../web/midi.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const wiring = appSource.slice(appSource.indexOf('\ninitMidi();'),
  appSource.indexOf("\n$('secondaryLoupeBtn')?.addEventListener"));

function harness({supported = true} = {}) {
  const requests = [], events = [], stored = new Map();
  const classes = new Set();
  const pill = {textContent: '', classList: {
    add: (...names) => names.forEach(name => classes.add(name)),
    remove: (...names) => names.forEach(name => classes.delete(name)),
  }};
  const hud = {hidden: true, textContent: ''};
  const buttons = new Map();
  for (const id of ['midiPill', 'midiLearnBtn', 'midiResetBtn']) {
    const button = id === 'midiPill' ? pill : {};
    button.addEventListener = (type, handler) => { button[type] = handler; };
    buttons.set(id, button);
  }
  const slider = {
    min: '-3', max: '3', step: '0.01', value: '0',
    getAttribute: name => name === 'data-g' ? 'exposure' : null,
    hasAttribute: name => name === 'data-g',
    closest: selector => selector === 'input[type="range"]' ? slider : null,
    dispatchEvent: event => events.push(event.type),
  };
  const document = {
    getElementById: id => id === 'speedHud' ? hud : buttons.get(id),
    querySelector: selector => selector === '[data-g="exposure"]' ? slider : null,
    addEventListener: (type, handler) => { document[type] = handler; },
  };
  const input = {name: 'Test controller'};
  const access = {inputs: new Map([['controller', input]])};
  const navigator = supported ? {requestMIDIAccess(options) {
    return new Promise((resolve, reject) => requests.push({options, resolve, reject}));
  }} : {};
  const context = vm.createContext({
    document, navigator, tr, $: id => buttons.get(id), toast() {},
    localStorage: {
      getItem: key => stored.get(key),
      setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key),
    },
    console: {warn() {}}, setTimeout: () => 1, clearTimeout() {},
    Event: class {constructor(type) { this.type = type; }},
  });
  // Run the shipped module and its actual startup/button/slider wiring.
  vm.runInContext(midiSource.replace(/^import .*from ['"]\.\/i18n\.js['"];?\n/m, '')
    .replace(/^export /gm, '') + wiring, context);
  return {requests, events, stored, classes, pill, hud, input, access, slider,
    click: id => buttons.get(id).click(),
    selectSlider: () => document.pointerdown({target: slider})};
}

test('startup, ordinary sliders, and Reset MIDI never request access or enter Learn', () => {
  const h = harness();
  assert.equal(h.pill.textContent, 'MIDI: Off');
  h.selectSlider();
  h.click('midiResetBtn');
  assert.equal(h.requests.length, 0);
  assert.equal(h.hud.hidden, true);
  assert.equal(h.classes.has('learning'), false);
});

for (const button of ['midiPill', 'midiLearnBtn']) {
  test(`${button} requests access on click, then maps and controls a slider`, async () => {
    const h = harness();
    const pending = h.click(button);
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].options.sysex, false);
    h.selectSlider();
    assert.equal(h.hud.hidden, true);
    h.requests[0].resolve(h.access);
    await pending;
    assert.equal(h.pill.textContent, 'MIDI Learn: Click slider');
    h.selectSlider();
    h.input.onmidimessage({data: [0xb0, 99, 64]});
    assert.equal(h.classes.has('learning'), false);
    assert.equal(JSON.parse(h.stored.get('lighttable-midi-mappings'))[99].control, 'exposure');
    h.input.onmidimessage({data: [0xb0, 99, 127]});
    assert.equal(h.slider.value, '3.00');
    assert.deepEqual(h.events, ['input']);
    h.selectSlider();
    assert.equal(h.classes.has('learning'), false);
    await h.click(button);
    await h.click(button);
    assert.equal(h.classes.has('learning'), false);
    assert.equal(h.requests.length, 1);
  });
}

test('repeated clicks share a pending request and denial permits an explicit retry', async () => {
  const h = harness();
  const pending = h.click('midiLearnBtn');
  await h.click('midiPill');
  assert.equal(h.requests.length, 1);
  h.requests[0].reject(new Error('Permission denied'));
  await pending;
  assert.equal(h.pill.textContent, 'MIDI: Off');
  h.selectSlider();
  assert.equal(h.hud.hidden, true);
  assert.equal(h.requests.length, 1);
  const retry = h.click('midiLearnBtn');
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve(h.access);
  await retry;
  assert.equal(h.pill.textContent, 'MIDI Learn: Click slider');
  const reloaded = harness();
  assert.equal(reloaded.requests.length, 0);
  assert.equal(reloaded.pill.textContent, 'MIDI: Off');
});

test('unsupported browsers remain unavailable without activating Learn', async () => {
  const h = harness({supported: false});
  await h.click('midiLearnBtn');
  h.selectSlider();
  assert.equal(h.pill.textContent, 'MIDI: Unavailable');
  assert.equal(h.hud.hidden, true);
  assert.equal(h.requests.length, 0);
});
