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

function exifScene() {
  let image = {name:'portrait.RAF',fileKey:'same-header',recoverySourceKey:'first',width:4416,height:2944};
  const state = {zoomMode:'100',viewMode:'detail'};
  const requests = [];
  const shown = [];
  const fn = source.match(/async function showExif\(.*?\n\}/s)[0];
  const showExif = new Function('S','cur','$','_exifCache','fetch','updateLoupeInfoOverlay',
    '_gridEls','layoutPhotoGrid','onViewportResize','scheduleAutomaticPreview','renderFilm',
    'tr','i18nHTML','broadcastToLoupe',`${fn}\nreturn showExif;`)(
      state,()=>image,()=>({}),new Map(),()=>new Promise(resolve=>requests.push(value=>resolve({json:()=>value}))),
      ()=>shown.push(state.exif),new Map(),()=>{},()=>{},()=>{},()=>{},text=>text,text=>text,()=>{});
  return {state,requests,shown,showExif,get image(){return image;},replace(next){image=next;}};
}

test('metadata recovery uses oriented source dimensions instead of the embedded JPEG', async () => {
  const scene = exifScene();
  const pending = scene.showExif('portrait.RAF');
  scene.requests.shift()({ImageWidth:4416,ImageHeight:2944,SourceWidth:5178,SourceHeight:7752});
  await pending;
  assert.deepEqual([scene.image.width,scene.image.height],[5178,7752]);
});

test('metadata from a replaced file cannot overwrite current geometry or reuse the old cache', async () => {
  const scene = exifScene();
  const stale = scene.showExif('portrait.RAF');
  scene.replace({name:'portrait.RAF',fileKey:'same-header',recoverySourceKey:'replacement'});
  scene.requests.shift()({SourceWidth:6000,SourceHeight:4000});
  await stale;
  assert.equal(scene.shown.length,0);
  const current = scene.showExif('portrait.RAF');
  assert.equal(scene.requests.length,1);
  scene.requests.shift()({SourceWidth:5178,SourceHeight:7752});
  await current;
  assert.deepEqual([scene.image.width,scene.image.height],[5178,7752]);
});
