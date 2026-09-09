import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createFrameScheduler } from '../web/render-scheduler.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));

test('coalesced visualization uses current pane and threshold even during a pending photo load', () => {
  let callback;
  const calls = [];
  const S = { activePane: 'healPane', previewLoadGeneration: 4 };
  const elements = { healVisualize: { checked: true }, healVisualizeThreshold: { value: '0.55' } };
  const context = { S, $: id => elements[id],
    createFrameScheduler: fn => createFrameScheduler(fn),
    drawGradeNow: () => calls.push('grade'), drawEditOverlayNow: () => calls.push('overlay'),
    nativePreviewActive: () => true, postNative: (type, data) => calls.push({type, data}),
  };
  const previous = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = fn => { callback = fn; return 1; };
  try {
    vm.runInNewContext(between('const previewFrameScheduler =', 'function drawEditOverlay()') +
      between('function spotVisualization()', 'function nativeEditsPayload('), context);
    context.refreshSpotVisualization();
    elements.healVisualizeThreshold.value = '0.8';
    context.refreshSpotVisualization();
    callback();
    assert.equal(calls.length, 3);
    assert.equal(calls[1].type, 'nativeSpotVisualization');
    assert.equal(calls[1].data.enabled, true);
    assert.equal(calls[1].data.threshold, 0.8);
    calls.length = 0;
    context.refreshSpotVisualization();
    S.activePane = 'editPane';
    callback();
    assert.equal(calls[1].data.enabled, false, 'leaving healing clears the diagnostic view');
    S.activePane = 'healPane';
    elements.healVisualize.checked = false;
    assert.equal(context.spotVisualization().enabled, false);
  } finally { globalThis.requestAnimationFrame = previous; }
});
