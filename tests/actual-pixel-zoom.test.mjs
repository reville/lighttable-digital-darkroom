import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createZoomMotion} from '../web/zoom-motion.js';
import {previewDetailLabel} from '../web/preview-detail.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const setup = source.slice(source.indexOf('function zoomView()'), source.indexOf('function zoomAt('));
const helpers = ['zoomAt','zoomReset','toggleActualZoomAt','onViewportResize','applyViewNow']
  .map(name => source.match(new RegExp(`function ${name}\\(.*?\\n\\}`, 's'))[0]).join('\n');
function scene(sourceWidth, fitWidth, reduced = true) {
  let at = 0, frame = null;
  const S = {viewMode:'detail',presentedPhotoName:'photo',zoom:1,zoomMode:'fit',panX:0,panY:0};
  const buttons = Object.fromEntries(['zoomFit','zoom1','zoomVal'].map(id => [id, {
    classList:{toggle(){}},setAttribute(key,value){this[key]=value;}}]));
  const rect = () => ({left:0,top:0,width:fitWidth*S.zoom,height:fitWidth*S.zoom/1.5});
  const cv = {width:300,height:200,getBoundingClientRect:rect};
  const cmp = {getBoundingClientRect:rect,style:{},classList:{toggle(){}}};
  const noop = () => {};
  globalThis.tr = (text) => text;
  const methods = new Function('S','$','displaySourcePixelWidth','clamp','sourceLongEdge',
    'clampPan','syncPreviewDetailStatus','cropViewState','syncCompareView','syncViewerChrome',
    'drawEditOverlayNow','scheduleNativeViewportLayout','scheduleViewportRegionRender','applyView',
    'renderFilm','syncCropPresentationNow','scheduleAutomaticPreview','createZoomMotion','cur','document',
    'viewportRegionEnabled','markContinuousInput','automaticPreviewTimer','viewportRegionTimer',
    `${helpers}\n${setup}\nreturn {toggleActualZoomAt,onViewportResize,applyViewNow,zoomAt,zoomMotion,stopZoomMotion};`)(
      S,id=>({cv,cmp,...buttons})[id],()=>sourceWidth,(v,a,b)=>Math.max(a,Math.min(b,v)),
      ()=>sourceWidth,noop,noop,()=>({}),noop,noop,noop,noop,noop,noop,noop,noop,noop,
      options=>createZoomMotion({...options,now:()=>at,reducedMotion:()=>reduced,
        requestFrame:run=>{frame=run;return 1;},cancelFrame:()=>{frame=null;},
        setTimer:()=>1,clearTimer:noop}),()=>({name:'photo'}),{hidden:false},()=>false,noop,null,null);
  return {S,buttons,...methods,tick(time){at=time;const run=frame;frame=null;run?.(at);},resize(width){fitWidth=width;methods.onViewportResize();}};
}

for (const [sourceWidth, fitWidth] of [[240,960],[6000,960],[48000,960]]) {
  test(`1:1 uses actual ${sourceWidth}px pixels even outside the manual zoom range`, () => {
    const view = scene(sourceWidth,fitWidth);
    view.toggleActualZoomAt(480,320);
    view.applyViewNow();
    assert.equal(view.S.zoom,sourceWidth/fitWidth);
    assert.equal(view.S.zoomMode,'100');
    assert.equal(view.buttons.zoomVal.textContent,'100%');
    assert.equal(view.buttons.zoom1['aria-pressed'],'true');
    assert.equal(view.buttons.zoomFit['aria-pressed'],'false');
    view.resize(1200);
    assert.equal(view.S.zoom,sourceWidth/1200);
    assert.equal(view.buttons.zoomVal.textContent,'100%');
    view.toggleActualZoomAt(600,400);
    assert.equal(view.S.zoomMode,'fit');
    assert.equal(view.S.zoom,1);
  });
}

test('zooming out of a small photo never increases its size', () => {
  const view = scene(240,960);
  view.toggleActualZoomAt(480,320);
  view.zoomAt(0.8,480,320);
  assert.equal(view.S.zoom,0.25);
  view.zoomAt(1.25,480,320);
  assert.equal(view.S.zoomMode,'custom');
  view.resize(1200);
  assert.equal(view.S.zoom,0.25); // same 125% pixel scale after resize
});

test('full-density native tiles finish detail status without claiming the tile is the full frame', () => {
  const detail = {state:'ready',actual:true,source:6000,requested:6000,delivered:1500,
    native:{width:1500,height:1000,viewport:{x:300,y:400,width:1500,height:1000,fullWidth:6000,fullHeight:4000}}};
  assert.equal(previewDetailLabel(detail),'100% detail ready');
  assert.equal(previewDetailLabel({...detail,refining:true}),'Refining RAW detail…');
  assert.equal(previewDetailLabel({...detail,state:'pending'}),'Loading 100% detail…');
  assert.equal(previewDetailLabel({...detail,native:{...detail.native,width:750}}),'Updating preview detail…');
  assert.equal(previewDetailLabel({...detail,native:null}),'Updating preview detail…');
});


test('the actual viewer animates 1:1, keeps its anchor and settles exactly', () => {
  const view = scene(6000,960,false);
  view.toggleActualZoomAt(600,320);
  assert.equal(view.S.zoom,1,'first frame keeps the current view');
  view.tick(120);
  assert.ok(view.S.zoom>1 && view.S.zoom<6.25);
  assert.ok(Math.abs(view.S.panX + 120*view.S.zoom - 120)<1e-8);
  view.tick(240);
  assert.equal(view.S.zoom,6.25);
  assert.equal(view.buttons.zoomVal.textContent,'100%');
  assert.equal(view.zoomMotion.active,false);
});

test('Fit reverses an unfinished 1:1 move from the displayed position', () => {
  const view = scene(6000,960,false);
  view.toggleActualZoomAt(480,320);view.tick(80);
  const visible=view.S.zoom;
  view.toggleActualZoomAt(480,320);
  assert.equal(view.S.zoom,visible);
  view.tick(200);
  assert.ok(view.S.zoom>1 && view.S.zoom<visible);
  view.tick(320);
  assert.equal(view.S.zoom,1);
  assert.equal(view.S.zoomMode,'fit');
});

test('continuous zoom interrupts motion, and resize cannot revive its old target', () => {
  const view=scene(6000,960,false);
  view.toggleActualZoomAt(480,320);view.tick(80);
  const visible=view.S.zoom;
  view.zoomAt(1.1,480,320);
  assert.equal(view.zoomMotion.active,false);
  assert.ok(Math.abs(view.S.zoom-visible*1.1)<1e-8);
  view.toggleActualZoomAt(480,320);view.tick(120);
  view.resize(1200);
  assert.equal(view.zoomMotion.active,false);
  assert.equal(view.S.zoom,5);
  view.tick(1000);
  assert.equal(view.S.zoom,5);
});
