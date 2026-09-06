import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const helpers = source.slice(source.indexOf('function cropSourceSize()'),
  source.indexOf('function previewCrop()'));
const fitting = source.slice(source.indexOf('function cropForRatio('),
  source.indexOf('function syncCropPanel()'));

function scene(image, rotation, canvas) {
  const state = {params:{rotate:rotation},cropRatio:'1',cropLocked:true};
  const methods = new Function('S','cur','$','clamp', helpers + fitting +
    '\nreturn {cropSourceSize,cropImageAspect,cropLayerRatio,cropForRatio};')(
    state, () => image, () => canvas, (v,lo,hi) => Math.min(hi,Math.max(lo,v)));
  return {...methods,state};
}

test('a square crop stays square through quarter turns before and after the render arrives', () => {
  const canvas = {width:1100,height:733};
  const view = scene({width:4128,height:2752}, 0, canvas);
  let crop = view.cropForRatio(null,view.cropLayerRatio());
  for (const rotation of [90,180,270,0]) {
    view.state.params.rotate = rotation;
    crop = view.cropForRatio(crop,view.cropLayerRatio());
    const size = view.cropSourceSize();
    assert.ok(Math.abs(crop.w*size.width-crop.h*size.height) < 1e-6);
    const before = view.cropImageAspect();
    canvas.width = rotation%180 ? 733 : 1100;
    canvas.height = rotation%180 ? 1100 : 733;
    assert.equal(view.cropImageAspect(),before,'render completion must not reinterpret the crop');
  }
});

test('navigating from a rotated landscape to a portrait uses the incoming source geometry', () => {
  const view=scene({width:1632,height:2448},0,{width:1100,height:733});
  assert.deepEqual(view.cropSourceSize(),{width:1632,height:2448});
  const crop=view.cropForRatio(null,view.cropLayerRatio());
  assert.equal(Math.round(crop.w*1632),Math.round(crop.h*2448));
});

test('metadata-free photos can still use the presented canvas dimensions', () => {
  const view=scene({},90,{width:600,height:900});
  assert.deepEqual(view.cropSourceSize(),{width:600,height:900});
});
