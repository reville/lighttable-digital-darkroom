// SPDX-License-Identifier: GPL-3.0-only
/* The server-backed grid view: pages arrive on demand, the cache stays
 * bounded, stale answers are dropped, and whole-view actions never need rows. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createLibraryView, specKey } from '../web/library-view.js';

function fakeServer(total, { delay = 0 } = {}) {
  const calls = [];
  const rows = Array.from({ length: total }, (_, i) => ({ name: `p${i}`, index: i }));
  const query = (spec) => {
    calls.push(spec);
    const filtered = spec.filter?.even ? rows.filter((row) => row.index % 2 === 0) : rows;
    const ordered = spec.sort?.dir === 'desc' ? [...filtered].reverse() : filtered;
    const located = spec.locate ? ordered.findIndex((row) => row.name === spec.locate) : undefined;
    const page = ordered.slice(spec.offset, spec.offset + spec.limit);
    const body = { total: ordered.length, offset: spec.offset, limit: spec.limit,
      located: located === -1 ? null : located,
      items: spec.namesOnly || spec.idsOnly ? [] : page,
      names: spec.namesOnly ? page.map((row) => row.name) : undefined };
    return delay ? new Promise((resolve) => setTimeout(() => resolve(body), delay)) : Promise.resolve(body);
  };
  return { query, calls, rows };
}

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

test('a spec loads page 0 only and reports the total; scrolling loads the pages in view', async () => {
  const server = fakeServer(1000);
  const changes = [];
  const view = createLibraryView({ query: server.query, pageSize: 100, onChange: (e) => changes.push(e) });
  const result = await view.setSpec({ sort: { field: 'name', dir: 'asc' } }, { immediate: true });
  assert.deepEqual(result, { total: 1000, located: null });
  assert.equal(view.list.length, 1000);
  assert.equal(view.list[0].name, 'p0');
  assert.equal(view.list[500], undefined, 'unrequested rows are holes');
  assert.equal(view.loadedCount, 100);
  await view.ensureRange(480, 620);
  assert.equal(view.list[500].name, 'p500');
  assert.equal(view.indexOf('p619'), 619);
  assert.equal(view.pageCount, 4);
  assert.equal(server.calls.length, 4, 'page 0 plus one request per missing page');
  await view.ensureRange(480, 620);
  assert.equal(server.calls.length, 4, 'loaded pages are not fetched again');
});

test('the cache is bounded: least recently used pages leave, the renderer windows and the current photo stay', async () => {
  const server = fakeServer(2000);
  const view = createLibraryView({ query: server.query, pageSize: 100, pageLimit: 3 });
  await view.setSpec({}, { immediate: true });
  await view.ensureRange(300, 400, 'grid');       // page 3
  await view.ensureRange(1500, 1600, 'strip');    // page 15
  await view.ensureIndex(900);                    // page 9
  const evicted = view.evict('p0');
  assert.equal(view.pageCount, 3);
  assert.ok(evicted.every((image) => image.index >= 900 && image.index < 1000), 'the unprotected page went');
  assert.equal(view.list[900], undefined);
  assert.equal(view.indexOf('p900'), -1);
  assert.equal(view.list[350].name, 'p350', 'grid window kept');
  assert.equal(view.list[1550].name, 'p1550', 'filmstrip window kept');
  assert.equal(view.list[0].name, 'p0', 'current photo page kept');
});

test('a stale answer is dropped and only the latest spec resolves', async () => {
  const server = fakeServer(500, { delay: 10 });
  const view = createLibraryView({ query: server.query, pageSize: 100, debounce: 5 });
  const first = view.setSpec({ filter: { even: false } });
  const second = view.setSpec({ filter: { even: true } });
  assert.equal(await first, null, 'superseded before it ran');
  const result = await second;
  assert.equal(result.total, 250);
  assert.equal(view.list[1].name, 'p2');
  assert.equal(server.calls.filter((call) => !call.filter?.even).length, 0, 'the typed-past spec was never sent');
});

test('a spec change keeps the old list on screen and refuses to page it with the new spec', async () => {
  const server = fakeServer(600, { delay: 10 });
  const view = createLibraryView({ query: server.query, pageSize: 100, debounce: 5 });
  await view.setSpec({}, { immediate: true });
  const old = view.list;
  const next = view.setSpec({ sort: { dir: 'desc' } });
  assert.equal(view.list, old, 'previous rows remain until the answer arrives');
  await view.ensureRange(300, 400);
  assert.equal(server.calls.length, 1, 'no page requests while the new view is pending');
  await next;
  assert.equal(view.list[0].name, 'p599');
});

test('locate reports where the current photo sits and its page arrives with page 0', async () => {
  const server = fakeServer(1000);
  const view = createLibraryView({ query: server.query, pageSize: 100 });
  const result = await view.setSpec({}, { immediate: true, locate: 'p742' });
  assert.equal(result.located, 742);
  assert.equal(view.list[742].name, 'p742', 'the located page is loaded');
  assert.equal(view.pageCount, 2);
  const gone = await view.setSpec({ filter: { even: true } }, { immediate: true, locate: 'p741' });
  assert.equal(gone.located, null, 'a photo the filter dropped has no position');
  assert.equal(await view.locate('p740'), 370);
});

test('a total that moved under a page request restarts the view instead of trusting positions', async () => {
  const server = fakeServer(300);
  const view = createLibraryView({ query: server.query, pageSize: 100 });
  await view.setSpec({}, { immediate: true });
  server.rows.splice(0, 1);
  await view.ensureRange(200, 300);
  await tick();
  assert.equal(view.total, 299);
  assert.equal(view.list[0].name, 'p1');
});

test('allNames walks the names pages without loading rows, and invalidate clears everything', async () => {
  const server = fakeServer(250);
  const view = createLibraryView({ query: server.query, pageSize: 100 });
  await view.setSpec({ filter: { even: true } }, { immediate: true });
  const names = await view.allNames();
  assert.equal(names.length, 125);
  assert.equal(names[124], 'p248');
  assert.ok(server.calls.some((call) => call.namesOnly));
  view.invalidate();
  assert.equal(view.total, 0);
  assert.equal(view.ready, false);
  assert.equal(view.list.length, 0);
  assert.equal(specKey({ a: 1 }), '{"a":1}');
});

test('materialize keeps one object per photo across pages and refreshes', async () => {
  const server = fakeServer(150);
  const pool = new Map();
  const view = createLibraryView({ query: server.query, pageSize: 100,
    materialize: (item) => {
      const existing = pool.get(item.name);
      if (existing) return Object.assign(existing, item);
      pool.set(item.name, { ...item });
      return pool.get(item.name);
    } });
  await view.setSpec({}, { immediate: true });
  const before = view.list[5];
  await view.refresh({ locate: 'p5' });
  assert.equal(view.list[5], before, 'a refresh reuses the host object');
});
