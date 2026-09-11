// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
function install(context, names) {
  for (const name of names) {
    vm.runInNewContext(source.match(new RegExp(`^function ${name}\\([^]*?^}$`, 'm'))[0], context);
  }
}

function scene() {
  const style = {setProperty(name, value) { this[name] = value; }};
  const canvas = {width:1200, height:800};
  const frame = {style, classList:{toggle() {}}};
  const context = {
    S:{params:{rotate:0}},
    $:id => ({cv:canvas, cmp:frame,
      zoomwrap:{getBoundingClientRect:() => ({width:900, height:600})}}[id]),
    cur:() => ({name:'landscape.jpg', width:6000, height:4000}),
    previewCrop:() => null, cropViewState:() => ({}), scheduleNativeViewportLayout() {},
  };
  install(context, ['cropSourceSize', 'cropViewportSize', 'syncCropPresentationNow']);
  const aspect = () => parseFloat(style.width) / parseFloat(style.height);
  return {context, canvas, aspect};
}

test('rotation keeps displayed pixels proportional while each quarter-turn render is pending', () => {
  const {context, canvas, aspect} = scene();
  for (const rotation of [90,180,270,0,270,180,90,0]) {
    context.S.params.rotate = rotation;
    context.syncCropPresentationNow();
    assert.equal(aspect(), canvas.width / canvas.height, 'pending pixels must not stretch');
    canvas.width = rotation % 180 ? 800 : 1200;
    canvas.height = rotation % 180 ? 1200 : 800;
    context.syncCropPresentationNow();
    assert.equal(aspect(), canvas.width / canvas.height, 'new frame must fit the uploaded orientation');
  }
});

test('cropped and actively cropping frames retain the displayed source aspect during rotation', () => {
  const {context, canvas, aspect} = scene();
  context.S.params.rotate = 90;
  context.previewCrop = () => ({x:0.2,y:0.1,w:0.5,h:0.75});
  context.syncCropPresentationNow();
  assert.equal(aspect(), (canvas.width * 0.5) / (canvas.height * 0.75));
  context.previewCrop = () => null;
  context.S.cropping = true;
  context.S.zoom = 1.4;
  context.syncCropPresentationNow();
  assert.equal(aspect(), canvas.width / canvas.height);
});

test('native geometry waits for its texture and the swap carries the final crop layout', async () => {
  const {context, canvas} = scene();
  const events = [], pending = new Map();
  let displayed = {width:1200,height:800};
  let frozen = false;
  let cropScheduled = false;
  Object.assign(context, {
    performance:{now:() => 0}, window:{}, nativePreviewPending:pending,
    packedMaskData:{}, GRADE_DEFAULTS:{},
    postNative(command, payload) {
      events.push({command,payload});
      if (command === 'nativeNavigate') frozen = true;
    },
    applyCropVisual() { cropScheduled = true; },
    cropFrameScheduler:{flush() { if (cropScheduled) context.syncCropPresentationNow(); }},
    drawGrade() {}, buildMaskTexture:() => ({}), nativeMaskPayload:() => ({}),
    nativeEditsPayload:() => ({}), scheduleNativeHelper() {}, originalPreviewURL:() => '/original',
    nativeViewportPayload:() => ({canvas:{width:parseFloat(context.$('cmp').style.width),
      height:parseFloat(context.$('cmp').style.height)}}),
    scheduleNativeViewportLayout() {
      if (!frozen) displayed = {width:canvas.width,height:canvas.height};
    },
  });
  install(context, ['setNativeBaseImage']);
  context.syncCropPresentationNow();
  context.S.params.rotate = 90;
  const result = context.setNativeBaseImage({native:{url:'/rotated',width:800,height:1200}}, 7);
  assert.deepEqual(displayed, {width:1200,height:800}, 'old drawable stays at its old dimensions while loading');
  assert.equal(events[0].command, 'nativeNavigate');
  const swap = events.find(event => event.command === 'nativePreview');
  assert.equal(swap.payload.canvas.width / swap.payload.canvas.height, 2/3);
  assert.equal(swap.payload.generation, 7);
  pending.get(7).resolve({presentation:'native-metal'});
  assert.equal((await result).presentation, 'native-metal');
});

test('browser texture upload and frame orientation change in the same callback', async () => {
  const {context, canvas, aspect} = scene();
  const img = {naturalWidth:800,naturalHeight:1200};
  context.syncCropPresentationNow();
  context.S.params.rotate = 90;
  context.S.seq = 8;
  context.S.gl = {
    cachedImage:() => img,
    setImage(image) {
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      return {textureCacheHit:true};
    },
  };
  Object.assign(context, {
    performance:{now:() => 0},
    installBrowserOriginal() {}, installBrowserReference() {}, drawGrade() {},
    scheduleHistogram() {}, applyCropVisual() {},
    cropFrameScheduler:{flush:() => context.syncCropPresentationNow()},
  });
  install(context, ['setWebGLBaseImage']);
  await context.setWebGLBaseImage('/rotated', {generation:8});
  assert.equal(aspect(), 2/3, 'frame must match new pixels without waiting for another animation frame');
});
