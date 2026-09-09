import assert from 'node:assert/strict';
import test from 'node:test';
import { bindThumbnailErrors } from '../web/thumbnail-errors.js';

class Element {
  constructor() { this.children = []; this.dataset = {}; this.style = {}; this.events = {}; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, fn) { (this.events[key] ??= []).push(fn); }
  emit(key) { return Promise.all((this.events[key] || []).map(fn => fn({stopPropagation() {}}))); }
  append(...children) { children.forEach(c => c.parent = this); this.children.push(...children); }
  remove() { this.parent.children = this.parent.children.filter(c => c !== this); }
  querySelector(selector) { return this.children.find(c => '.' + c.className === selector); }
}
globalThis.document = { createElement: () => new Element() };
const tick = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
function cell(source = '/api/thumb?name=a.jpg', onState) {
  const host = new Element(), image = new Element();
  host.append(image); image.dataset.thumbnailKind = 'source';
  const sync = bindThumbnailErrors(host, image, onState); sync(source);
  return {host, image, sync, panel: () => host.querySelector('.thumbnail-error')};
}

test('shows diagnostic text safely, retries explicitly, and clears on success', async () => {
  const states = [];
  const h = cell('/api/thumb?name=a.jpg', failed => states.push(failed)); let calls = 0;
  globalThis.fetch = async () => { calls++; return {ok: false, json: async () => ({
    error: 'Original is empty (0 bytes). Restore it.', details: {diagnostic: '<img onerror=bad> is not HTML'},
  })}; };
  await h.image.emit('error'); await tick();
  assert.equal(calls, 1);
  assert.equal(h.image.style.visibility, 'hidden');
  assert.equal(h.image.dataset.thumbnailError, '1');
  assert.equal(states.at(-1), true);
  assert.match(h.panel().children[1].textContent, /0 bytes/);
  assert.match(h.panel().children[1].textContent, /<img onerror=bad>/);
  assert.equal(h.panel().children[1].children.length, 0);
  await h.image.emit('error'); assert.equal(calls, 1, 'no automatic repeated decode');
  assert.match(h.panel().title, /0 bytes/, 'repeat errors keep the detailed cause');
  h.panel().children[2].onclick({stopPropagation() {}});
  assert.match(h.image.src, /retry=/);
  assert.equal(h.panel(), undefined);
  await h.image.emit('load'); assert.equal(h.image.style.visibility, '');
  assert.equal(h.image.dataset.thumbnailError, undefined);
  assert.equal(states.at(-1), false);
});

test('late error responses cannot replace a new source or a loaded rendition', async () => {
  const h = cell(); let finish;
  globalThis.fetch = () => new Promise(resolve => finish = resolve);
  const failure = h.image.emit('error');
  h.sync('/api/thumb?name=b.jpg');
  finish({ok:false, json:async () => ({error:'old source failed'})});
  await failure; await tick();
  assert.equal(h.panel(), undefined);
  const next = h.image.emit('error');
  await h.image.emit('load');
  finish({ok:false, json:async () => ({error:'already recovered'})});
  await next; await tick();
  assert.equal(h.panel(), undefined);
});

test('duplicate grid and strip failures share diagnostics and only two requests run at once', async () => {
  const requests = [];
  globalThis.fetch = source => new Promise(resolve => requests.push({source, resolve}));
  const cells = [cell('/api/thumb?name=shared'), cell('/api/thumb?name=shared'),
    cell('/api/thumb?name=second'), cell('/api/thumb?name=third')];
  const errors = cells.map(h => h.image.emit('error'));
  assert.equal(requests.length, 2);
  requests[0].resolve({ok:false,json:async () => ({error:'shared error'})}); await tick();
  assert.equal(requests.length, 3);
  requests.slice(1).forEach(r => r.resolve({ok:false,json:async () => ({error:'other error'})}));
  await Promise.all(errors); await tick();
  assert.equal(cells[0].panel().title, 'shared error');
  assert.equal(cells[1].panel().title, 'shared error');
});

test('network failures and non-JSON responses explain the failure', async () => {
  const h = cell();
  globalThis.fetch = async () => { throw new Error('offline'); };
  await h.image.emit('error'); await tick();
  assert.match(h.panel().title, /Could not reach LightTable/);
  h.sync('/api/thumb?name=other');
  globalThis.fetch = async () => ({ok:false,status:503,json:async () => {throw new Error('HTML');}});
  await h.image.emit('error'); await tick();
  assert.match(h.panel().title, /HTTP 503/);
});

test('a failed edited rendition falls back to the source and recovered diagnostics retry only once', async () => {
  const h = cell(); let calls = 0;
  globalThis.fetch = async () => { calls++; return {ok:true}; };
  h.image.dataset.thumbnailKind = 'edited';
  await h.image.emit('error');
  assert.equal(h.image.src, '/api/thumb?name=a.jpg');
  assert.equal(calls, 0);
  await h.image.emit('error'); await tick();
  assert.match(h.image.src, /retry=/);
  await h.image.emit('error');
  assert.equal(calls, 1);
});
