/* Behavioural cover for the scope panel and the sampling texture it reads.
 *
 * The scopes draw from a small WebGL "helper" surface. When the render
 * response for a settled preview stopped offering that helper, every scope
 * silently drew an empty canvas. These tests execute the drawing functions
 * against a recording 2D context so a scope that stops putting pixels on the
 * canvas, or maps luminance to the wrong row, fails here.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

const slice = (from, to) => {
  const start = appSource.indexOf(from);
  const end = appSource.indexOf(to);
  assert.ok(start >= 0, `app.js must define ${from}`);
  assert.ok(end > start, `app.js must define ${to} after ${from}`);
  return appSource.slice(start, end);
};

/* A 2D context that keeps the pixels the scopes write, so assertions can look
 * at the canvas rather than at the calls that produced it. */
function recordingCanvas(width, height) {
  const buffer = new Uint8ClampedArray(width * height * 4);
  const noop = () => {};
  const ctx = {
    strokeStyle: '', fillStyle: '', lineWidth: 1, font: '',
    globalCompositeOperation: 'source-over',
    beginPath: noop, closePath: noop, moveTo: noop, lineTo: noop,
    stroke: noop, fill: noop, arc: noop, fillRect: noop, fillText: noop,
    clearRect: noop,
    measureText: () => ({ width: 8 }),
    getImageData: (x, y, w, h) => ({ width: w, height: h, data: buffer.slice() }),
    putImageData: (image) => { buffer.set(image.data); },
  };
  return {
    ctx,
    cv: { width, height },
    /* Rows carrying any drawn density, top row first. */
    litRows() {
      const rows = [];
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          if (buffer[(y * width + x) * 4 + 3]) { rows.push(y); break; }
        }
      }
      return rows;
    },
    litColumns(row) {
      const columns = [];
      for (let x = 0; x < width; x++) {
        if (buffer[(row * width + x) * 4 + 3]) columns.push(x);
      }
      return columns;
    },
  };
}

/* The shape GradeRenderer.sample() returns: tightly packed RGBA. */
function sampleOf(width, height, pixel) {
  const px = new Uint8Array(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const [r, g, b] = pixel(x, y);
      const offset = (y * width + x) * 4;
      px[offset] = r; px[offset + 1] = g; px[offset + 2] = b; px[offset + 3] = 255;
    }
  }
  return { px, w: width, h: height };
}

function scopes({ canvas, sample = null } = {}) {
  const refreshes = [];
  const context = vm.createContext({
    S: { clip: false, gl: sample === null ? null : { sample: () => sample } },
    $: () => ({ ...(canvas ? canvas.cv : {}),
      getContext: () => (canvas ? canvas.ctx : null) }),
    refreshWebGLSamplingSurface: () => refreshes.push(true),
  });
  vm.runInContext(
    `${slice("let scopeMode = 'histogram';", "document.querySelectorAll('[data-scope]')")}
     globalThis.scopeApi = {
       drawWaveformScope, drawVectorscope, drawHistogramScope, drawHistogram,
       setMode(mode) { scopeMode = mode; },
     };`,
    context);
  return { ...context.scopeApi, refreshes };
}

const WIDTH = 300;
const HEIGHT = 110;
/* The waveform maps 0..255 onto the full canvas height, brightest at the top. */
const rowFor = (value) => HEIGHT - 1 - Math.round((value / 255) * (HEIGHT - 1));

test('the waveform draws pixels for a normal sample', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  scopes().drawWaveformScope(surface.ctx, surface.cv,
    sampleOf(64, 48, (x) => [x * 4, x * 4, x * 4]));
  assert.ok(surface.litRows().length > 8,
    'a gradient must light up many waveform rows, not an empty canvas');
});

test('the waveform places luminance at the matching IRE row', () => {
  for (const value of [0, 128, 255]) {
    const surface = recordingCanvas(WIDTH, HEIGHT);
    scopes().drawWaveformScope(surface.ctx, surface.cv,
      sampleOf(32, 24, () => [value, value, value]));
    assert.deepEqual(surface.litRows(), [rowFor(value)],
      `a flat ${value} sample belongs on exactly one row`);
  }
  assert.equal(rowFor(0), HEIGHT - 1, 'black sits at 0 IRE, the bottom row');
  assert.equal(rowFor(255), 0, 'white sits at 100 IRE, the top row');
});

test('the waveform weights luminance rather than averaging channels', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  /* Pure green is much brighter than pure blue under Rec.709 weights. */
  scopes().drawWaveformScope(surface.ctx, surface.cv,
    sampleOf(32, 24, () => [0, 255, 0]));
  assert.deepEqual(surface.litRows(), [rowFor(255 * 0.7152)]);
});

test('the RGB parade separates the channels into three columns', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  scopes().drawWaveformScope(surface.ctx, surface.cv,
    sampleOf(32, 24, () => [255, 0, 0]), true);
  const section = Math.floor(WIDTH / 3);
  const red = surface.litColumns(rowFor(255));
  const dark = surface.litColumns(rowFor(0));
  assert.ok(red.length > 0 && red.every((x) => x < section),
    'a pure red frame lights only the red section at full height');
  assert.ok(dark.every((x) => x >= section),
    'the green and blue sections stay at the bottom of the parade');
});

test('the vectorscope puts a neutral frame at the centre', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  scopes().drawVectorscope(surface.ctx, surface.cv,
    sampleOf(32, 24, () => [128, 128, 128]));
  assert.deepEqual(surface.litRows(), [Math.round(HEIGHT / 2)],
    'a neutral frame carries no chroma, so it lands on the centre row');
});

test('the vectorscope pushes a saturated frame away from the centre', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  scopes().drawVectorscope(surface.ctx, surface.cv,
    sampleOf(32, 24, () => [220, 30, 30]));
  const rows = surface.litRows();
  assert.equal(rows.length, 1);
  assert.ok(rows[0] < HEIGHT / 2 - 5,
    'red carries a positive Cr, which plots above the centre line');
});


/* ------------------------------------------------ scope mode dispatch */

test('each scope mode draws through drawHistogram', () => {
  const modes = ['histogram', 'waveform', 'parade', 'vectorscope'];
  for (const mode of modes) {
    const surface = recordingCanvas(WIDTH, HEIGHT);
    const api = scopes({
      canvas: surface,
      sample: sampleOf(48, 32, (x, y) => [x * 5, y * 7, 128]),
    });
    api.setMode(mode);
    api.drawHistogram();
    assert.equal(api.refreshes.length, 1,
      `${mode} must refresh the sampling surface before reading it`);
    if (mode !== 'histogram') {
      assert.ok(surface.litRows().length > 0,
        `${mode} must put pixels on the canvas`);
    }
  }
});

test('a scope with no sampling surface draws nothing instead of stale pixels', () => {
  const surface = recordingCanvas(WIDTH, HEIGHT);
  const api = scopes({ canvas: surface, sample: null });
  api.setMode('waveform');
  api.drawHistogram();
  assert.deepEqual(surface.litRows(), [],
    'without a helper texture the scope leaves a cleared canvas');
});

/* ----------------------------------------- the sampling helper request */

/* scheduleNativeHelper defers the WebGL sampling texture until interaction
 * settles. It runs against a fake clock so the ordering is deterministic. */
function helperHarness() {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const loaded = [];
  const context = vm.createContext({
    record(url, options) { loaded.push({ url, options: { ...options } }); },
    performance: { now: () => now },
    setTimeout(fn, ms) {
      const id = ++nextTimer;
      timers.set(id, { fn, at: now + (ms || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(
    `let nativeHelperTimer = null;
     let lastContinuousInputAt = 0;
     const S = { seq: 0 };
     function setWebGLBaseImage(url, options) {
       globalThis.record(url, options);
       return Promise.resolve({});
     }
     ${slice('function scheduleNativeHelper(', 'function setNativeBaseImage(')}
     globalThis.api = {
       schedule: scheduleNativeHelper,
       renderStarts() { return ++S.seq; },
     };`,
    context);
  const api = context.api;
  return {
    loaded,
    renderStarts: api.renderStarts,
    schedule: api.schedule,
    advance(ms) {
      const target = now + ms;
      for (;;) {
        const due = [...timers.entries()]
          .filter(([, timer]) => timer.at <= target)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = target;
    },
  };
}

test('the settled render loads the sampling helper the interactive one lost', () => {
  const h = helperHarness();
  const interactive = h.renderStarts();
  h.schedule('/api/render/helper?key=interactive', interactive);
  /* The full-resolution render supersedes it before the deferred fetch runs. */
  h.advance(161);
  const settled = h.renderStarts();
  h.schedule('/api/render/helper?key=settled', settled);
  h.advance(400);
  assert.deepEqual(h.loaded.map((entry) => entry.url),
    ['/api/render/helper?key=settled'],
    'exactly one helper loads, and it belongs to the settled render');
  assert.deepEqual(h.loaded[0].options,
    { preserveCanvasSize: true, forceWebGLDraw: true, generation: settled });
});

test('a superseded helper never overwrites the current sampling texture', () => {
  const h = helperHarness();
  const stale = h.renderStarts();
  h.schedule('/api/render/helper?key=stale', stale);
  h.renderStarts();
  h.advance(400);
  assert.deepEqual(h.loaded, [],
    'the generation guard drops a helper whose render was replaced');
});

test('a render without a helper leaves the pending request armed', () => {
  const h = helperHarness();
  const generation = h.renderStarts();
  h.schedule('/api/render/helper?key=only', generation);
  h.schedule(undefined, generation);
  h.advance(400);
  assert.deepEqual(h.loaded.map((entry) => entry.url),
    ['/api/render/helper?key=only']);
});
