import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {t as tr} from '../web/i18n.js';

const source = readFileSync(new URL('../web/film-browser.js', import.meta.url), 'utf8');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function harness() {
  const nodes = new Map(), observers = [], requests = [], created = [], revoked = [], applied = [], timers = new Map();
  const document = {activeElement: {focus() {}}, body: {append() {}}};
  class Element {
    constructor(tag = 'div') {
      this.tag = tag; this.children = []; this.attributes = {}; this.handlers = {};
      this.value = ''; this.clientWidth = 600; this.clientHeight = 410; this.offsetTop = 500;
      const classes = new Set();
      this.classList = {add: v => classes.add(v), remove: v => classes.delete(v)};
    }
    set innerHTML(html) {
      for (const [, id] of html.matchAll(/id="([^"]+)"/g)) nodes.set(id, new Element());
    }
    append(...children) { this.children.push(...children); }
    replaceChildren() { this.children = []; }
    querySelector(selector) { return nodes.get(selector.slice(1)); }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return this.attributes[key]; }
    addEventListener(key, fn) { this.handlers[key] = fn; }
    focus() { document.activeElement = this; }
  }
  document.createElement = tag => new Element(tag);
  class Observer {
    constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
    observe(target) { this.targets.push(target); }
    disconnect() { this.disconnected = true; }
    visible(...indices) {
      this.callback(this.targets.map((target, index) => ({target, isIntersecting: indices.includes(index)})));
    }
  }
  const state = {params: {stock: 'stock-1', profile_enabled: true}, grade: {exposure: 0.5}};
  const snapshot = {name: 'test.jpg', label: 'test.jpg', engine: 'rs', state,
    stocks: Array.from({length: 23}, (_, i) => ({id: `stock-${i}`, label: `Film ${i}`})), profiles: []};
  const runtime = vm.createContext({document, tr, AbortController, IntersectionObserver: Observer,
    window: {devicePixelRatio: 2},
    URL: {createObjectURL(blob) { const url = `blob:${created.length}`; created.push({url, blob}); return url; },
      revokeObjectURL(url) { revoked.push(url); }},
    setTimeout(fn) { timers.set(fn, fn); return fn; }, clearTimeout(id) { timers.delete(id); },
    fetch(path, options) {
      return new Promise(resolve => requests.push({path, options, body: JSON.parse(options.body),
        finish(ok = true) { resolve({ok, headers: {get: () => 'image/jpeg'}, blob: async () => ({image: true})}); }}));
    },
  });
  vm.runInContext(source.replace(/^import .*;$/gm, '').replaceAll('export function', 'function'), runtime);
  const ui = runtime.createFilmBrowser({context: () => snapshot, apply: (...args) => applied.push(args)});
  return {ui, nodes, observers, requests, created, revoked, applied, state,
    cards: () => nodes.get('filmPreviewGrid').children,
    search(query) {
      const input = nodes.get('filmBrowserSearch'); input.value = query; input.handlers.input();
      for (const fn of timers.values()) fn(); timers.clear();
    }};
}

test('all stocks are reachable, while rendering stays serial and follows the visible cards', async () => {
  const h = harness(); h.ui.open();
  assert.equal(h.cards().length, 23);
  assert.equal(h.requests.length, 0);
  assert.equal(h.nodes.has('filmBrowserNext'), false);
  assert.equal(h.nodes.has('filmBrowserPrevious'), false);
  const observer = h.observers[0]; observer.visible(0, 1, 2, 3);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].body.w, 1440);
  assert.equal(h.requests[0].body.state.grade.exposure, 0.5);
  // A fast scroll skips the old unstarted cards and can reach the last stock.
  observer.visible(22); h.requests[0].finish(); await tick();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].body.state.params.stock, 'stock-22');
  h.requests[1].finish(); await tick();
  observer.visible(0, 1); await tick();
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].body.state.params.stock, 'stock-1');
  assert.equal(h.created.length, 2, 'returning to a rendered card reuses its image');
  assert.equal(h.state.params.stock, 'stock-1', 'browsing must not change the snapshot');
  h.cards()[22].onclick();
  assert.deepEqual(h.applied, [['stock-22', 'test.jpg']]);
  assert.equal(h.requests[2].options.signal.aborted, true);
  assert.equal(h.revoked.length, 2);
});

test('search and close discard late previews and disconnect stale observers', async () => {
  const h = harness(); h.ui.open(); h.observers[0].visible(0, 1);
  const old = h.requests[0]; h.search('Film 22');
  assert.equal(old.options.signal.aborted, true);
  assert.equal(h.observers[0].disconnected, true);
  assert.equal(h.cards().length, 1);
  assert.equal(h.nodes.get('filmPreviewGrid').scrollTop, 0);
  h.observers[1].visible(0); old.finish(); await tick();
  assert.equal(h.created.length, 0);
  assert.equal(h.requests[1].body.state.params.stock, 'stock-22');
  h.ui.close(); h.requests[1].finish(); h.observers[0].visible(2); await tick();
  assert.equal(h.created.length, 0);
  assert.equal(h.requests.length, 2);
  assert.equal(h.observers[1].disconnected, true);
});

test('failed previews do not block other stocks, and an empty search can be cleared', async () => {
  const h = harness(); h.ui.open(); h.observers[0].visible(0, 1);
  h.requests[0].finish(false); await tick();
  assert.match(h.cards()[0].children[2].textContent, /Preview unavailable/);
  assert.equal(h.requests.length, 2);
  h.search('no such film');
  assert.equal(h.cards().length, 0);
  assert.equal(h.nodes.get('filmBrowserEmpty').hidden, false);
  h.search('');
  assert.equal(h.cards().length, 23);
  assert.equal(h.nodes.get('filmBrowserEmpty').hidden, true);
  h.ui.close();
});
