#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only
/* Measures the production browser grid view (web/library-view.js) at catalog
 * sizes the old whole-catalog paging could not reach. A synthetic zero-delay
 * query stands in for the server so this isolates the view's own bookkeeping
 * (page cache bounds, positions map, eviction) from HTTP and SQLite cost,
 * which bench/dam_benchmark.py already measures for the same query contract.
 * This does not measure DOM creation, thumbnail decode, or browser paint. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { performance } from 'node:perf_hooks';
import { createLibraryView, VIEW_PAGE_SIZE, VIEW_PAGE_LIMIT } from '../web/library-view.js';

function summary(samples) {
  const ordered = [...samples].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return { samples: ordered.length,
    median_ms: +((ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2).toFixed(4)),
    p95_ms: +ordered[Math.ceil(ordered.length * .95) - 1].toFixed(4),
    max_ms: +ordered.at(-1).toFixed(4) };
}

function peakRssMib() {
  return +(process.memoryUsage().rss / 1024 ** 2).toFixed(2);
}

function fakeServer(total) {
  const rows = Array.from({ length: total }, (_, i) => ({
    name: `p${i}`, index: i, rating: i % 6, capturedAt: 1700000000 + i,
  }));
  let queries = 0;
  const query = (spec) => {
    queries += 1;
    let filtered = rows;
    if (spec.filter?.ratingMin) filtered = filtered.filter((r) => r.rating >= spec.filter.ratingMin);
    const ordered = spec.sort?.dir === 'desc' ? [...filtered].reverse() : filtered;
    const page = ordered.slice(spec.offset, spec.offset + spec.limit);
    return Promise.resolve({ total: ordered.length, offset: spec.offset, limit: spec.limit, items: page });
  };
  return { query, queryCount: () => queries };
}

async function measure(count) {
  if (global.gc) global.gc();
  const rssBefore = peakRssMib();
  const server = fakeServer(count);
  const view = createLibraryView({ query: server.query });
  assert.equal(VIEW_PAGE_SIZE, 200);
  assert.equal(VIEW_PAGE_LIMIT, 40);

  const openStart = performance.now();
  await view.setSpec({ sort: { field: 'name', dir: 'asc' } }, { immediate: true });
  const first_page_ms = +(performance.now() - openStart).toFixed(3);
  assert.equal(view.total, count);

  // Simulate a scroll sweep across the entire catalog in viewport-sized
  // strides, as the grid's overscan window would request it.
  const stride = 60;
  const scrollSamples = [];
  let maxPagesResident = view.pageCount;
  for (let offset = 0; offset < count; offset += stride) {
    const started = performance.now();
    await view.ensureRange(offset, Math.min(count, offset + stride));
    // The consumer (web/app.js) evicts after every render, keeping the
    // photo at `offset` (its "current photo") from being dropped mid-sweep.
    view.evict(`p${offset}`);
    scrollSamples.push(performance.now() - started);
    maxPagesResident = Math.max(maxPagesResident, view.pageCount);
  }

  // A filter change mid-scroll must debounce/cancel rather than block input,
  // and must not grow the resident page cache beyond its bound.
  const filterStart = performance.now();
  const filtered = await view.setSpec(
    { sort: { field: 'name', dir: 'asc' }, filter: { ratingMin: 4 } }, { immediate: true });
  const filter_change_ms = +(performance.now() - filterStart).toFixed(3);

  const rssAfter = peakRssMib();
  return {
    images: count,
    page_size: VIEW_PAGE_SIZE,
    page_cache_limit: VIEW_PAGE_LIMIT,
    first_page_ms,
    full_sweep: { ...summary(scrollSamples), strides: scrollSamples.length, stride_photos: stride },
    max_pages_resident_during_sweep: maxPagesResident,
    max_pages_resident_bounded: maxPagesResident <= VIEW_PAGE_LIMIT,
    filter_change_ms,
    filtered_total: filtered.total,
    server_queries: server.queryCount(),
    peak_rss_mib: { before: rssBefore, after: rssAfter, growth: +(rssAfter - rssBefore).toFixed(2) },
  };
}

async function main() {
  const args = process.argv.slice(2);
  const outputIndex = args.indexOf('--output');
  const sizes = [3000, 50000, 100000];
  const runs = [];
  for (const size of sizes) runs.push(await measure(size));
  const output = {
    schema: 1,
    scope: 'library-view: bounded windowed paging over a server-filtered/sorted spec',
    node: process.version,
    runs,
  };
  const json = JSON.stringify(output, null, 2);
  if (outputIndex >= 0 && args[outputIndex + 1]) {
    fs.writeFileSync(args[outputIndex + 1], json);
    console.log(`Wrote ${args[outputIndex + 1]}`);
  } else {
    console.log(json);
  }
}

await main();
