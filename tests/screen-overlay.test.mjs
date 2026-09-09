import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { radialHandles } from '../web/mask-shape.js';
import { screenOverlayGeometry, prepareScreenOverlay } from '../web/screen-overlay.js';

const viewport = { left: 120, top: 80, width: 900, height: 600 };
const rect = (left, top, width, height) => ({ left, top, width, height });
const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

for (const dpr of [1, 1.25, 2, 3]) {
  test(`overlay stays at display density and viewport size at 1–32x, DPR ${dpr}`, () => {
    for (const zoom of [1, 2, 4, 16, 32]) {
      const image = rect(120 - 450 * (zoom - 1), 80 - 300 * (zoom - 1), 900 * zoom, 600 * zoom);
      const g = screenOverlayGeometry(image, image, viewport, dpr);
      assert.equal(g.pixelWidth, 900 * dpr);
      assert.equal(g.pixelHeight, 600 * dpr);
      // The center of the photo stays at the center of the visible bitmap.
      assert.equal(g.sourceX + g.sourceWidth / 2, 450);
      assert.equal(g.sourceY + g.sourceHeight / 2, 300);
    }
  });
}

test('committed crops clip the overlay without changing source coordinates', () => {
  const image = rect(-600, -400, 2400, 1600);
  const frame = rect(200, 130, 600, 400);
  const g = screenOverlayGeometry(image, frame, viewport, 2);
  assert.deepEqual([g.left, g.top, g.width, g.height], [80, 50, 600, 400]);
  assert.deepEqual([g.sourceX, g.sourceY, g.sourceWidth, g.sourceHeight], [-800, -530, 2400, 1600]);
  assert.deepEqual([g.pixelWidth, g.pixelHeight], [1200, 800]);
});

test('portrait letterboxing and offscreen photos do not produce oversized canvases', () => {
  const image = rect(370, 80, 400, 600);
  const g = screenOverlayGeometry(image, image, viewport, 2);
  assert.deepEqual([g.left, g.top, g.pixelWidth, g.pixelHeight], [250, 0, 800, 1200]);
  assert.equal(screenOverlayGeometry(rect(1200, 0, 100, 100), image, viewport), null);
  assert.equal(screenOverlayGeometry(rect(0, 0, 0, 0), image, viewport), null);
});

function recordingCanvas() {
  const calls = [];
  const ctx = new Proxy({}, { get: (object, key) => key in object ? object[key] : (...args) => {
    calls.push({ method: key, args, lineWidth: ctx.lineWidth, fillStyle: ctx.fillStyle });
  } });
  return { calls, ctx, style: {}, width: 0, height: 0, getContext: () => ctx };
}

test('redraw clears in bitmap coordinates and handles fractional layout and density changes', () => {
  const canvas = recordingCanvas();
  const image = rect(-100.25, -80.5, 2400, 1600);
  const frame = rect(120.5, 80.25, 800.25, 500.5);
  for (const dpr of [1, 2, 1.25]) {
    const g = screenOverlayGeometry(image, frame, viewport, dpr);
    const surface = prepareScreenOverlay(canvas, g);
    const [reset, clear, transform] = canvas.calls.slice(-3);
    assert.deepEqual(reset.args, [1, 0, 0, 1, 0, 0]);
    assert.deepEqual(clear.args, [0, 0, canvas.width, canvas.height]);
    assert.equal(transform.args[0], canvas.width / g.width);
    assert.equal(transform.args[5], g.sourceY * canvas.height / g.height);
    assert.equal(surface.width, image.width);
  }
  assert.equal(prepareScreenOverlay(canvas, null), null);
  assert.equal(canvas.width, 1);
  assert.equal(canvas.style.width, '0px');
});

test('photo zoom enlarges correction areas while line widths, dashes and pins stay constant', () => {
  const overlay = recordingCanvas();
  let image;
  const elements = {
    editOverlay: overlay,
    cv: { width: 256, height: 171, getBoundingClientRect: () => image },
    cmp: { getBoundingClientRect: () => image },
    zoomwrap: { getBoundingClientRect: () => viewport },
    healVisualize: { checked: true },
    healVisualizeThreshold: { value: '0.55' },
  };
  const S = { activePane: 'healPane', localPinsVisible: true, selectedHealId: 'spot',
    heals: [{ id: 'spot', target: [0.5, 0.5], source: [0.55, 0.5], radius: 0.1, mode: 'clone' }],
    baseImg: { complete: true, naturalWidth: 256 },
    overlayHoverPoint: null, healBrush: { radius: 0.1, feather: 0.5 } };
  const draw = new Function('S', '$', 'window', 'screenOverlayGeometry', 'prepareScreenOverlay',
    'syncOverlayCursorClass', source.slice(source.indexOf('function drawBrushCursor('),
      source.indexOf('function syncOverlayCursorClass(')) + '\n' +
    source.slice(source.indexOf('function drawEditOverlayNow()'), source.indexOf('const previewFrameScheduler')) +
    '\nreturn drawEditOverlayNow;')(
    S, id => elements[id], { devicePixelRatio: 2 }, screenOverlayGeometry, prepareScreenOverlay, () => {});
  for (const zoom of [1, 4, 32]) {
    image = rect(120 - 450 * (zoom - 1), 80 - 300 * (zoom - 1), 900 * zoom, 600 * zoom);
    overlay.calls.length = 0;
    draw();
    assert.ok(!overlay.calls.some(c => c.method === 'drawImage'),
      'Visualize Spots must not cover the sharp preview with the sampling helper');
    const arcs = overlay.calls.filter(c => c.method === 'arc');
    assert.deepEqual(arcs.map(c => c.args[2]), [60 * zoom, 4.5, 60 * zoom, 4.5]);
    assert.ok(overlay.calls.filter(c => c.method === 'stroke').every(c => c.lineWidth === 2.2));
    assert.deepEqual(overlay.calls.find(c => c.method === 'setLineDash').args, [[4, 4]]);
    assert.equal(overlay.width, 1800, 'small native sampling helpers must not lower overlay resolution');
  }
});

test('pointer placement and handle hit tests use the whole photo after cropping and pan', () => {
  const image = rect(-600, -400, 2400, 1600);
  const helpers = new Function('$', 'clamp', source.slice(source.indexOf('function overlayPoint('),
    source.indexOf('function healHandleAt(')) + '\nreturn {overlayPoint, overlayDistance};')(
    id => { assert.equal(id, 'cv'); return {getBoundingClientRect: () => image}; },
    (v, lo, hi) => Math.max(lo, Math.min(hi, v)));
  assert.deepEqual(helpers.overlayPoint({clientX: 600, clientY: 400}), [0.5, 0.5]);
  assert.ok(Math.abs(helpers.overlayDistance([0.5, 0.5], [0.5, 0.6]) - 160) < 1e-9);
});

test('radial and linear mask pins and brush outlines retain screen size', () => {
  const overlay = recordingCanvas();
  let image = viewport;
  const mask = { type: 'radial', center: [0.5, 0.5], radius: 0.1, radiusX: 0.1, radiusY: 0.15, angle: 45,
    start: [0.4, 0.5], end: [0.6, 0.5] };
  const S = { activePane: 'maskPane', localPinsVisible: true, brushSize: 0.2,
    brushFeather: 0.5, overlayHoverPoint: [0.5, 0.5] };
  const elements = {
    editOverlay: overlay, maskShowOverlay: { checked: false },
    cv: { width: 256, height: 171, getBoundingClientRect: () => image },
    cmp: { getBoundingClientRect: () => image },
    zoomwrap: { getBoundingClientRect: () => viewport },
  };
  const draw = new Function('S', '$', 'window', 'screenOverlayGeometry', 'prepareScreenOverlay',
    'syncOverlayCursorClass', 'selectedMask', 'radialHandles', source.slice(source.indexOf('function drawBrushCursor('),
      source.indexOf('function syncOverlayCursorClass(')) + '\n' +
    source.slice(source.indexOf('function drawEditOverlayNow()'), source.indexOf('const previewFrameScheduler')) +
    '\nreturn drawEditOverlayNow;')(
    S, id => elements[id], { devicePixelRatio: 2 }, screenOverlayGeometry, prepareScreenOverlay,
    () => {}, () => mask, radialHandles);
  for (const zoom of [1, 8, 32]) {
    image = rect(120 - 450 * (zoom - 1), 80 - 300 * (zoom - 1), 900 * zoom, 600 * zoom);
    for (const type of ['radial', 'linear', 'brush']) {
      mask.type = type;
      overlay.calls.length = 0;
      draw();
      const radii = overlay.calls.filter(c => c.method === 'arc').map(c => c.args[2]);
      assert.deepEqual(radii, type === 'radial' ? [5, 5, 5, 5] :
        type === 'linear' ? [6, 6] : [60 * zoom, 30 * zoom]);
      if (type === 'radial') {
        const ellipse = overlay.calls.find(c => c.method === 'ellipse');
        assert.deepEqual(ellipse.args.slice(2, 5), [60 * zoom, 90 * zoom, Math.PI / 4]);
      }
      const strokes = overlay.calls.filter(c => c.method === 'stroke');
      assert.ok(strokes.every(c => c.lineWidth === (type === 'brush' ? 1 : 1.5)));
    }
  }
});
