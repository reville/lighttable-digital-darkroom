// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {t as tr} from '../web/i18n.js';
import {labelSwatch} from '../web/labels.js';

const source = (name) => readFileSync(new URL(`../web/${name}`, import.meta.url), 'utf8');

function metadataHarness(get = async () => ({ iptc: {} })) {
  const opened = [], notices = [], elements = new Map();
  let visible = true;
  for (const id of ['iptcLat', 'iptcLon', 'iptcMapLink']) {
    elements.set(id, {
      _value: '', disabled: false,
      get value() { return this._value; },
      set value(value) { this._value = String(value); },
      addEventListener(type, handler) { this[type] = handler; },
    });
  }
  elements.set('infoPane', { classList: { contains: () => visible } });
  const context = vm.createContext({
    tr, setTimeout() { return 1; }, clearTimeout() {},
    window: { open: (...args) => opened.push(args) },
  });
  vm.runInContext(source('metadata-panel.js').replace(/^import .*;$/gm, '').replace('export function', 'function'), context);
  const panel = context.createMetadataPanel({
    el: (id) => elements.get(id), get, post: async () => ({ ok: true }),
    toast: (message) => notices.push(message),
  });
  return {
    panel, opened, notices, map: elements.get('iptcMapLink'),
    visible(value) { visible = value; },
    coordinates(lat, lon) {
      elements.get('iptcLat').value = lat;
      elements.get('iptcLon').value = lon;
      elements.get('iptcLat').input();
    },
  };
}

test('Maps requires a selected photo and loaded metadata, including after hidden-panel navigation', async () => {
  const h = metadataHarness(async () => ({ iptc: { gps_lat: 40, gps_lon: -70 } }));
  assert.equal(h.map.disabled, true);
  h.coordinates('40', '-70');
  h.map.click();
  assert.equal(h.opened.length, 0);
  await h.panel.refresh('first.jpg');
  assert.equal(h.map.disabled, false);
  h.visible(false);
  await h.panel.refresh('second.jpg');
  assert.equal(h.map.disabled, true, 'coordinates from the previous photo must not be usable');
  h.map.click();
  assert.equal(h.opened.length, 0);
  h.visible(true);
  await h.panel.refresh('second.jpg');
  assert.equal(h.map.disabled, false);
  await h.panel.refresh(null);
  assert.equal(h.map.disabled, true);
});

test('Maps rejects blanks, non-finite values, and out-of-range coordinates without opening a window', async () => {
  const h = metadataHarness();
  await h.panel.refresh('photo.jpg');
  for (const [lat, lon] of [
    ['', ''], [' ', '0'], ['0', ''], ['NaN', '0'], ['Infinity', '0'],
    ['0', '-Infinity'], ['90.01', '0'], ['-90.01', '0'], ['0', '180.01'], ['0', '-180.01'],
  ]) {
    h.coordinates(lat, lon);
    assert.equal(h.map.disabled, true, `${JSON.stringify([lat, lon])} must be unavailable`);
    h.map.click();
  }
  assert.equal(h.opened.length, 0);
});

test('Maps accepts zero and coordinate boundaries, and clearing a coordinate disables it immediately', async () => {
  const h = metadataHarness();
  await h.panel.refresh('photo.jpg');
  for (const [lat, lon] of [['0', '0'], ['90', '180'], ['-90', '-180'], [' 40.5 ', ' -73.25 ']]) {
    h.coordinates(lat, lon);
    assert.equal(h.map.disabled, false);
    h.map.click();
    assert.deepEqual(h.opened.at(-1), [
      `https://maps.apple.com/?ll=${Number(lat)},${Number(lon)}&q=Photo`, '_blank', 'noopener',
    ]);
  }
  h.coordinates('40.5', '');
  assert.equal(h.map.disabled, true);
});

test('Maps stays unavailable while another photo loads and after a failed metadata request', async () => {
  let rejectRequest;
  const h = metadataHarness((path) => path.endsWith('first.jpg')
    ? Promise.resolve({ iptc: { gps_lat: 10, gps_lon: 20 } })
    : new Promise((resolve, reject) => { rejectRequest = reject; }));
  await h.panel.refresh('first.jpg');
  const loading = h.panel.refresh('second.jpg');
  assert.equal(h.map.disabled, true);
  await Promise.resolve();
  rejectRequest(new Error('Metadata unavailable'));
  await loading;
  assert.equal(h.map.disabled, true);
  h.map.click();
  assert.equal(h.opened.length, 0);
});

function surveyHarness() {
  let images = ['A', 'B', 'C'].map((name) => ({ name }));
  const grid = {
    children: [], style: { setProperty() {} }, addEventListener() {},
    get firstElementChild() { return this.children[0] || null; },
    insertBefore(cell, cursor) {
      cell.remove();
      const index = cursor ? this.children.indexOf(cursor) : this.children.length;
      this.children.splice(index, 0, cell);
      cell.parent = this;
    },
  };
  const elements = new Map([
    ['survey', { hidden: true }], ['surveyTitle', {}], ['surveyGrid', grid],
    ['surveySwap', { hidden: false, disabled: false }],
  ]);
  const document = {
    body: { classList: { add() {}, remove() {} } },
    createElement() {
      const children = new Map([
        ['img', { getAttribute() { return this.src; }, setAttribute(name, value) { this[name] = value; } }],
        ['.survey-remove', { dataset: {} }], ['.survey-name', {}], ['.survey-marks', {}],
      ]);
      return {
        dataset: {}, parent: null,
        querySelector: (selector) => children.get(selector),
        get nextElementSibling() {
          return this.parent?.children[this.parent.children.indexOf(this) + 1] || null;
        },
        remove() {
          if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
          this.parent = null;
        },
      };
    },
  };
  const context = vm.createContext({ document, tr, labelSwatch });
  vm.runInContext(source('survey.js').replace(/^import .*;$/gm, '').replace(/export function/g, 'function'), context);
  const survey = context.createSurvey({ el: (id) => elements.get(id), images: () => images });
  return {
    survey, swap: elements.get('surveySwap'),
    removeImage(name) { images = images.filter((image) => image.name !== name); survey.render(); },
    order: () => grid.children.map((cell) => decodeURIComponent(cell.dataset.name)),
  };
}

test('Swap is hidden and disabled in normal Survey, while Compare swaps the two displayed photos', () => {
  const h = surveyHarness();
  h.survey.open(['A', 'B', 'C']);
  assert.equal(h.swap.hidden, true);
  assert.equal(h.swap.disabled, true);
  h.survey.swap();
  assert.deepEqual(h.order(), ['A', 'B', 'C']);
  h.survey.open(['A', 'B', 'C'], 'compare');
  assert.equal(h.swap.hidden, false);
  assert.equal(h.swap.disabled, false);
  h.survey.swap();
  assert.deepEqual(h.order(), ['B', 'A']);
  h.survey.open(['A', 'B']);
  assert.equal(h.swap.hidden, true);
  assert.equal(h.swap.disabled, true);
});

test('Compare disables Swap when removing a photo or when a photo is no longer available', () => {
  const h = surveyHarness();
  h.survey.open(['A', 'B'], 'compare');
  h.survey.remove('B');
  assert.equal(h.swap.disabled, true);
  h.survey.swap();
  assert.deepEqual(h.order(), ['A']);
  h.survey.open(['A', 'B'], 'compare');
  h.removeImage('B');
  assert.equal(h.swap.disabled, true);
  h.survey.swap();
  assert.deepEqual(h.order(), ['A']);
});
