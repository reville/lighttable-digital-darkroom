import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {automaticPreviewWidth} from '../web/view-performance.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const helpers = ['requestedPreviewWidth', 'displaySourcePixelWidth',
  'cropSourceSize', 'viewportSourceGeometryKey'].map(name =>
  source.match(new RegExp(`function ${name}\\(.*?\\n\\}`, 's'))[0]).join('\n');

function scene(image) {
  const state = {params:{rotate:0},zoom:1,zoomMode:'fit'};
  const canvas = {width:300,height:150};
  const elements = {pw:{value:'auto'},cv:canvas,
    zoomwrap:{clientWidth:1028,clientHeight:700}};
  const methods = new Function('S','cur','$','window','automaticPreviewWidth','previewCrop',
    `${helpers}\nreturn {requestedPreviewWidth,displaySourcePixelWidth,viewportSourceGeometryKey};`)(
      state, () => image, id => elements[id], {devicePixelRatio:2}, automaticPreviewWidth, () => null);
  return {...methods,state,canvas};
}

test('Auto never mistakes the initial canvas, helper, or outgoing photo for the source size', () => {
  const view = scene({name:'street.RW2'});
  for (const [width,height] of [[300,150],[256,171],[1100,733],[8000,6000]]) {
    Object.assign(view.canvas,{width,height});
    assert.equal(view.requestedPreviewWidth(),2200);
    assert.equal(view.displaySourcePixelWidth(),0,'unknown dimensions cannot yield an actual pixel percentage');
  }
  view.state.viewportSourceGeometry = {key:'another photo',width:6000,height:4000};
  assert.equal(view.displaySourcePixelWidth(),0);
});

test('recovered catalog dimensions restore pixel zoom independently of the preview', () => {
  const image = {name:'street.RW2'};
  const view = scene(image);
  Object.assign(image,{width:6008,height:4008});
  assert.equal(view.displaySourcePixelWidth(),6008);
  assert.equal(view.requestedPreviewWidth(),2200);
  view.state.params.rotate = 90;
  assert.equal(view.displaySourcePixelWidth(),4008);
  assert.equal(view.requestedPreviewWidth(),1400);
  view.state.zoomMode = '100';
  assert.equal(view.requestedPreviewWidth(),6008);
});
