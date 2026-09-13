// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {GradeRenderer} from '../web/gl.js';

function renderer({bytes = 256 * 1024 * 1024, count = 6} = {}) {
  const uploads = [], deleted = [];
  let next = 0;
  const noop = () => {};
  const gl = {TEXTURE_2D: 1, TEXTURE0: 2, RGB: 3, RGBA: 4,
    createTexture: () => ({id: ++next}), deleteTexture: t => deleted.push(t),
    activeTexture: noop, bindTexture: noop, texParameteri: noop, pixelStorei: noop,
    viewport: noop, useProgram: noop, uniform2f: noop,
    texImage2D: (...args) => { if (args.length === 6) uploads.push(args[5]); }};
  const r = Object.assign(Object.create(GradeRenderer.prototype), {
    gl, canvas: {width: 1, height: 1}, imageTextures: new Map(), imageTextureBytes: 0,
    maxImageTextureBytes: bytes, maxImageTextures: count, sampleWidth: 1, sampleHeight: 1,
  });
  return {r, uploads, deleted};
}
const photo = (width, height) => ({naturalWidth: width, naturalHeight: height, complete: true});

test('returning to a cached photo reuses its texture, decoded image and geometry', () => {
  const {r, uploads} = renderer();
  const a = photo(6216, 4136), b = photo(4032, 3012);
  r.setImage(a, {cacheKey: 'A'}); const textureA = r.tex;
  r.setImage(b, {cacheKey: 'B'}); assert.notEqual(r.tex, textureA);
  assert.equal(r.cachedImage('A'), a);
  assert.deepEqual(r.setImage(a, {cacheKey: 'A'}), {textureCacheHit: true});
  assert.equal(r.tex, textureA);
  assert.deepEqual([r.canvas.width, r.canvas.height], [6216, 4136]);
  assert.deepEqual(uploads, [a, b]);
  assert.deepEqual([r.sampleWidth, r.sampleHeight], [128, 85]);
});

test('a cached interactive preview can preserve the current canvas size', () => {
  const {r} = renderer(); const draft = photo(1100, 733);
  r.setImage(draft, {cacheKey: 'draft'});
  r.setImage(photo(3000, 2000), {cacheKey: 'full'});
  r.setImage(draft, {cacheKey: 'draft', resizeCanvas: false});
  assert.deepEqual([r.canvas.width, r.canvas.height], [3000, 2000]);
  assert.deepEqual([r.sampleWidth, r.sampleHeight], [128, 85]);
});

test('byte and entry limits evict least recently used textures and decoded images', () => {
  const {r, deleted, uploads} = renderer({bytes: 800, count: 2});
  const a = photo(10, 10), b = photo(10, 10), c = photo(10, 10);
  r.setImage(a, {cacheKey: 'A'}); const textureA = r.tex;
  r.setImage(b, {cacheKey: 'B'}); const textureB = r.tex;
  r.setImage(a, {cacheKey: 'A'});
  r.setImage(c, {cacheKey: 'C'});
  assert.equal(r.cachedImage('B'), undefined);
  assert.equal(r.cachedImage('A'), a);
  assert.deepEqual(deleted, [textureB]);
  assert.equal(r.imageTextureBytes, 800);
  r.setImage(b, {cacheKey: 'B'});
  assert.deepEqual(deleted, [textureB, textureA]);
  assert.deepEqual(uploads, [a, b, c, b]);
});

test('an oversized current photo evicts everything else and is evicted on leaving', () => {
  const {r, deleted} = renderer({bytes: 800});
  r.setImage(photo(10, 10), {cacheKey: 'small'});
  const large = photo(30, 30);
  r.setImage(large, {cacheKey: 'large'});
  assert.equal(r.imageTextures.size, 1);
  assert.equal(r.imageTextureBytes, 3600);
  assert.equal(deleted.length, 1);
  r.setImage(photo(10, 10), {cacheKey: 'next'});
  assert.equal(r.imageTextureBytes, 400);
  assert.equal(r.cachedImage('large'), undefined);
});

test('new render identities and mutable unkeyed sources cannot reuse old pixels', () => {
  const {r, uploads} = renderer(); const image = photo(12, 8);
  r.setImage(image, {cacheKey: 'photo:old-edit'});
  r.setImage(image, {cacheKey: 'photo:new-edit'});
  r.setImage(image); r.setImage(image);
  assert.equal(uploads.length, 4);
});

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
function originalHarness() {
  const requested = [], uploaded = [], cleared = [];
  const S = {compareActive: false, holdBefore: false, originalImageName: null,
    gl: {setOriginalImage: image => uploaded.push(image), drawCompare: () => {},
      clearOriginalImage: () => cleared.push(url)}};
  let url = 'photo-A@1400', native = false;
  class Image {
    complete = false; naturalWidth = 0;
    set src(value) {requested.push(this); this.url = value;}
    finish() {this.complete = true; this.naturalWidth = 1400; this.onload();}
  }
  const context = {S, Image, $: () => ({removeAttribute: () => {}}),
    originalPreviewURL: () => url, requestedPreviewWidth: () => 1400,
    nativePreviewActive: () => native, previewSourceX: x => x, renderedComparePosition: () => 0.5};
  vm.runInNewContext(between('let browserOriginal = null;', 'function rememberPresentedRender(') +
    '\nglobalThis.sync = syncBrowserOriginal;', context);
  return {S, requested, uploaded, cleared, sync: context.sync,
    photo: value => {url = value;}, native: value => {native = value;}};
}

test('ordinary browsing neither fetches nor uploads Original', () => {
  const h = originalHarness(); h.sync(); h.photo('photo-B@6000'); h.sync();
  assert.equal(h.requested.length, 0); assert.equal(h.uploaded.length, 0);
});

test('Compare and held Original fetch on demand and reuse a finished Original', () => {
  const h = originalHarness(); h.S.compareActive = true; h.sync();
  assert.equal(h.requested.length, 1); h.requested[0].finish();
  assert.equal(h.uploaded.length, 1);
  h.S.compareActive = false; h.sync(); h.S.holdBefore = true; h.sync();
  assert.equal(h.requested.length, 1); assert.equal(h.uploaded.length, 1);
});

test('Original arriving after dismissal is not uploaded until it is needed', () => {
  const h = originalHarness(); h.S.holdBefore = true; h.sync();
  h.S.holdBefore = false; h.requested[0].finish();
  assert.equal(h.uploaded.length, 0);
  h.S.compareActive = true; h.sync(); assert.equal(h.uploaded.length, 1);
});

test('an old photo Original cannot replace the newly requested one', () => {
  const h = originalHarness(); h.S.compareActive = true; h.sync();
  h.photo('photo-B@6000'); h.sync();
  h.requested[0].finish(); assert.equal(h.uploaded.length, 0);
  h.requested[1].finish(); assert.deepEqual(h.uploaded, [h.requested[1]]);
});

test('a sharper Original of the same photo keeps the loaded one until it arrives', () => {
  const h = originalHarness(); h.S.compareActive = true;
  h.photo('/api/orig?name=A&w=1400&rot=0'); h.sync(); h.requested[0].finish();
  const loaded = h.cleared.length;
  h.photo('/api/orig?name=A&w=6000&rot=0'); h.sync();
  assert.equal(h.cleared.length, loaded, 'zooming cleared the loaded Original');
  h.requested[1].finish();
  assert.deepEqual(h.uploaded, [h.requested[0], h.requested[1]]);
  h.photo('/api/orig?name=A&w=6000&rot=90'); h.sync();
  assert.equal(h.cleared.length, loaded + 1, 'a rotated Original kept the previous pixels');
  h.requested[2].finish();
  h.photo('/api/orig?name=B&w=6000&rot=90'); h.sync();
  assert.equal(h.cleared.length, loaded + 2, 'another photo kept the previous Original');
});

test('native presentation never fetches or installs browser Original', () => {
  const h = originalHarness(); h.S.compareActive = true; h.native(true); h.sync();
  assert.equal(h.requested.length, 0);
  h.native(false); h.sync(); h.native(true); h.requested[0].finish();
  assert.equal(h.uploaded.length, 0);
});
