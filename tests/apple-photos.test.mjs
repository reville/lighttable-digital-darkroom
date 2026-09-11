// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installApplePhotosBrowser, photosBrowserAvailable } from '../web/apple-photos.js';

const markup = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
function fixture(platform = 'macos', bridge = true) {
  class Element {
    hidden = false; disabled = false; value = ''; textContent = ''; inert = false; children = [];
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase();
      const classes = new Set();
      this.classList = {contains: key => classes.has(key), add: key => classes.add(key), remove: key => classes.delete(key)};
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, fn) { this[`${name}Listener`] = fn; }
    focus() { document.activeElement = this; }
    getClientRects() { return this.hidden ? [] : [{}]; }
    querySelector(selector) {
      if (selector === 'img') return this.children[0].children.find(child => child.tagName === 'IMG');
      if (selector === '.apple-photos-preview > span') return this.children[0].children[0];
      return null;
    }
    querySelectorAll() { return []; }
  }
  const all = new Map([...markup.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)]
    .map(([, tag, attrs, id]) => {
      const node = new Element(tag); node.hidden = /\bhidden(?:\s|>|$)/.test(attrs); return [id, node];
    }));
  const listeners = new Map();
  globalThis.document = {activeElement: all.get('applePhotosOpen'),
    body: {children: [all.get('appShell'), all.get('applePhotosDialog')]},
    createElement: tag => new Element(tag), addEventListener: (name, fn) => listeners.set(name, fn)};
  globalThis.window = {__LIGHTTABLE_PLATFORM__: platform};
  const actions = [], registered = [], viewed = [];
  const controller = installApplePhotosBrowser({el: id => all.get(id), nativeBridge: () => bridge,
    sendNative: (action, detail) => { actions.push({action, ...detail}); return bridge; },
    onImported: async path => { registered.push(path); }, onViewImported: async path => { viewed.push(path); }});
  return {controller, all, actions, registered, viewed, listeners,
    click: id => all.get(id).onclick(),
    capabilities: () => controller.nativeEvent({type: 'sources', photosBrowserAvailable: true}),
    page: (items, detail = {}) => controller.nativeEvent({type: 'applePhotosPage',
      requestId: actions.findLast(action => action.action === 'browseApplePhotos').requestId,
      items, offset: 0, total: items.length, ...detail}),
    cards: () => all.get('applePhotosGrid').children};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('Photos is hidden by default and only the supported native shell reveals it', () => {
  for (const platform of ['windows', 'linux', undefined]) {
    const f = fixture(platform, platform !== undefined);
    assert.equal(f.all.get('applePhotosOpen').hidden, true);
    f.capabilities();
    assert.equal(f.all.get('applePhotosOpen').hidden, true);
    assert.equal(f.all.get('importPhotosBtn').hidden, true);
    f.controller.open(); assert.equal(f.actions.length, 0);
  }
  assert.equal(photosBrowserAvailable({}, 'macos', true), false);
  const f = fixture(); f.capabilities();
  assert.equal(f.all.get('applePhotosOpen').hidden, false);
  f.controller.close();
});

test('selection follows asset IDs across pages; stale pages and thumbnails are discarded', () => {
  const f = fixture(); f.capabilities(); f.controller.open();
  const first = f.actions.at(-1).requestId;
  f.page([{id: 'a', name: '<A>.jpg'}], {hasMore: true, total: 61});
  f.cards()[0].onclick();
  f.click('applePhotosNext');
  f.controller.nativeEvent({type: 'applePhotosPage', requestId: first, items: [{id: 'wrong', name: 'late.jpg'}]});
  assert.equal(f.cards().length, 0);
  f.page([{id: 'b', name: 'B.jpg'}], {offset: 60, total: 61});
  f.cards()[0].onclick();
  f.controller.nativeEvent({type: 'applePhotosThumbnail', requestId: first, id: 'b', data: 'data:image/jpeg;base64,YQ=='});
  assert.equal(f.cards()[0].querySelector('img').hidden, true);
  f.click('applePhotosImport');
  assert.deepEqual(f.actions.at(-1), {action: 'importBrowsedApplePhotos', assetIds: ['a', 'b']});
  // Closing or Escape cannot abandon an active transfer.
  f.controller.close(); assert.equal(f.all.get('applePhotosDialog').classList.contains('on'), true);
  f.click('applePhotosStop'); assert.equal(f.actions.at(-1).action, 'cancelBrowsedApplePhotosImport');
  f.controller.nativeEvent({type: 'applePhotosImport', state: 'cancelled'});
  f.controller.close();
  assert.equal(f.all.get('appShell').inert, false);
});

test('partial import registers completed copies once and preserves selection for retry', async () => {
  const f = fixture(); f.capabilities(); f.controller.open();
  f.page([{id: 'a', name: 'A.raw'}, {id: 'b', name: 'B.raw'}]); f.click('applePhotosSelectPage');
  f.click('applePhotosImport');
  const completion = {type: 'applePhotosImport', importId: 'one', state: 'cancelled',
    imported: 1, failures: 1, path: '/fixture/Photos'};
  f.controller.nativeEvent(completion); await tick();
  f.controller.nativeEvent(completion); await tick();
  assert.deepEqual(f.registered, ['/fixture/Photos']);
  assert.match(f.all.get('applePhotosSelection').textContent, /2/);
  f.click('applePhotosImport');
  assert.deepEqual(f.actions.at(-1).assetIds, ['a', 'b']);
  f.controller.nativeEvent({...completion, importId: 'two', state: 'completed', imported: 1, existing: 1, failures: 0});
  await tick();
  assert.equal(f.all.get('applePhotosImport').disabled, true);
  await f.click('applePhotosViewImported');
  assert.deepEqual(f.viewed, ['/fixture/Photos']);
});

test('permission failure can be refreshed and late replies after close cannot reopen the browser', () => {
  const f = fixture(); f.capabilities(); f.controller.open();
  const first = f.actions.at(-1).requestId;
  f.page([], {error: 'Photos permission denied'});
  assert.equal(f.all.get('applePhotosStatus').textContent, 'Photos permission denied');
  assert.equal(f.all.get('applePhotosRefresh').disabled, false);
  f.click('applePhotosRefresh'); const current = f.actions.at(-1).requestId;
  assert.ok(current > first);
  f.controller.close();
  f.controller.nativeEvent({type: 'applePhotosPage', requestId: current, items: [{id: 'late', name: 'late.jpg'}]});
  assert.equal(f.cards().length, 0);
  assert.equal(f.all.get('applePhotosDialog').classList.contains('on'), false);
});

test('selection remains capped at 500 across pages', () => {
  const f = fixture(); f.capabilities(); f.controller.open();
  for (let i = 0; i < 9; i++) {
    f.page(Array.from({length: 60}, (_, j) => ({id: `${i}-${j}`, name: `${j}.jpg`})), {hasMore: true});
    f.click('applePhotosSelectPage');
    if (i < 8) f.click('applePhotosNext');
  }
  f.click('applePhotosImport');
  assert.equal(f.actions.at(-1).assetIds.length, 500);
  f.controller.nativeEvent({type: 'applePhotosImport', state: 'error', message: 'retry'});
  f.controller.close();
});
