#!/usr/bin/env node
// Production viewport geometry only. This does not measure DOM/image paint or
// end-to-end browser scrolling. A separate subprocess bounds memory per size.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const geometry = fs.readFileSync(new URL('../web/view-performance.js', import.meta.url));
// Load this dependency-free module explicitly as ESM without changing the
// application's package settings or inheriting a parent directory's settings.
const { createGridLayout, visibleGridPositions } = await import(
  `data:text/javascript;base64,${geometry.toString('base64')}`);

const script = fileURLToPath(import.meta.url);
const args = process.argv.slice(2);

function summary(samples) {
  const ordered = [...samples].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return { samples: ordered.length,
    median_ms: +((ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2).toFixed(4)),
    p95_ms: +ordered[Math.ceil(ordered.length * .95) - 1].toFixed(4),
    max_ms: +ordered.at(-1).toFixed(4) };
}

function measure(count) {
  const images = Array.from({ length: count }, (_, index) => ({
    width: [6000, 4000, 6000, 0][index % 4],
    height: [4000, 6000, 2000, 0][index % 4],
  }));
  const modes = {};
  for (const photo of [false, true]) {
    const started = performance.now();
    const layout = createGridLayout(images, { width: 1400, cell: 180, photo });
    const layoutMs = performance.now() - started;
    const samples = [];
    let maxVisible = 0;
    for (let i = 0; i < 1000; i++) {
      // Deterministic jumps throughout the library, including its last page.
      const top = Math.max(0, layout.height - 900) * ((i * 7919) % 1000) / 999;
      const queryStarted = performance.now();
      const visible = visibleGridPositions(layout, top, 900);
      samples.push(performance.now() - queryStarted);
      maxVisible = Math.max(maxVisible, visible.length);
      assert.ok(visible.length > 0 && visible.length < 250, 'Viewport work must stay bounded');
      // Validate coverage periodically outside the timed window. This oracle
      // scans all positions and would catch a fast but incomplete viewport.
      if (i % 100 === 0 || i === 999) {
        const ids = new Set(visible.map(position => position.index));
        for (const position of layout.positions) {
          if (position.bottom >= top && position.top <= top + 900) {
            assert.ok(ids.has(position.index), 'Visible photograph missing');
          }
        }
      }
    }
    modes[photo ? 'photo' : 'square'] = {
      layout_ms: +layoutMs.toFixed(3), viewport_lookup: summary(samples),
      max_visible_positions: maxVisible, total_positions: layout.positions.length,
    };
  }
  return { images: count, modes,
    peak_rss_mib: +(process.resourceUsage().maxRSS / 1024).toFixed(2) };
}

if (args[0] === '--worker') {
  process.stdout.write(JSON.stringify(measure(Number(args[1]))) + '\n');
} else {
  const outputIndex = args.indexOf('--output');
  const outputPath = outputIndex >= 0 ? args[outputIndex + 1] : null;
  if (outputIndex >= 0) args.splice(outputIndex, 2);
  const sizes = args.length ? args.map(Number) : [50000, 100000];
  assert.ok(sizes.every(n => Number.isInteger(n) && n > 0 && n <= 1000000), 'Sizes must be 1..1000000');
  const runs = sizes.map(count => {
    const child = spawnSync(process.execPath, [script, '--worker', String(count)],
      { encoding: 'utf8', timeout: 60000 });
    assert.equal(child.status, 0, child.stderr || child.error?.message);
    return JSON.parse(child.stdout);
  });
  const serialized = JSON.stringify({ schema: 1, node: process.version,
    view_performance_sha256: createHash('sha256').update(geometry).digest('hex'),
    scope: 'Production viewport geometry only; no DOM, thumbnail decoding or browser paint', runs }, null, 2) + '\n';
  if (outputPath) fs.writeFileSync(outputPath, serialized);
  process.stdout.write(serialized);
}
