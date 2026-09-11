// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function harness() {
  const elements = [], requests = [];
  let observerCallback;
  const context = vm.createContext({
    S: {images: [], viewMode: 'photo'}, window: {},
    document: {querySelectorAll: () => elements},
    IntersectionObserver: class {constructor(callback) {observerCallback = callback;} observe() {} unobserve() {}},
    URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
    bindThumbnailErrors: () => () => {}, recordPhotoDisplayState() {},
    cur: () => null, tr: value => value, encodeURIComponent,
    setTimeout, fetch: url => new Promise(resolve => requests.push({url, resolve})),
  });
  const urlFunction = source.slice(source.indexOf('function thumbnailURL('), source.indexOf('\nlet displayStatusFrame'));
  const queue = source.slice(source.indexOf('const EDITED_THUMB_CONCURRENCY'), source.indexOf('\nfunction imageForLibraryElement'));
  vm.runInContext(urlFunction + '\n' + queue, context);
  function add(name) {
    const im = {name, fileKey: name}; context.S.images.push(im);
    const node = {dataset: {thumbnailName: name, thumbnailVisible: '1'},
      getAttribute(key) {return this[key];}, setAttribute(key, value) {this[key] = value;}};
    elements.push(node); return {im, node};
  }
  return {context, requests, add, enter: node => observerCallback([{target:node,isIntersecting:true}])};
}

test('the first grid image requests the large tier; strip stays small', () => {
  const h = harness();
  const {im, node} = h.add('a.jpg');
  const host = {querySelector: () => node, classList: {contains: name => name === 'cell'}};
  node.dataset.thumbnailVisible = '0';
  h.context.syncThumbnailImage(host, im);
  assert.match(node.src, /\/api\/thumb\?.*&w=1024$/);
  assert.equal(node.dataset.thumbnailKind, 'source');
  host.classList.contains = () => false;
  h.context.syncThumbnailImage(host, im);
  assert.match(node.src, /&w=240$/);
});

test('scrolling away removes waiting work before it reaches the renderer', async () => {
  const h = harness();
  const items = ['a.jpg', 'b.jpg', 'c.jpg'].map(h.add);
  for (const {im} of items) h.context.queueEditedThumbnail(im);
  assert.equal(h.requests.length, 2);
  assert.ok(h.requests.every(r => r.url.endsWith('&w=1024')));
  items[2].node.dataset.thumbnailVisible = '0';
  h.requests[0].resolve({ok: false, status: 404});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.length, 2, 'offscreen c.jpg should never be fetched');
  items[2].node.dataset.thumbnailVisible = '1';
  h.context.S.viewMode = 'detail';
  h.context.queueEditedThumbnail(items[2].im);
  assert.equal(h.requests.length, 3);
  assert.ok(h.requests[2].url.endsWith('&w=320'));
});


test('reused filmstrip nodes switch rendition identity on entering Detail', async () => {
  const h = harness();
  const {im, node} = h.add('a.jpg');
  node.dataset.thumbnailIdentity = h.context.editedThumbnailIdentity(im);
  h.context.S.viewMode = 'detail';
  h.enter(node);
  assert.match(node.dataset.thumbnailIdentity, /\|320$/);
  h.requests[0].resolve({ok:true, status:200, blob:async () => ({})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(node.dataset.thumbnailKind, 'edited');
  assert.equal(node.src, 'blob:test');
});
