import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createZoomMotion} from '../web/zoom-motion.js';

function clock(reduced=false) {
  let at=0, next=0, done=0, latest=null;
  const frames=new Map(),timers=new Map(),painted=[];
  const motion=createZoomMotion({now:()=>at,reducedMotion:()=>reduced,
    requestFrame:fn=>{frames.set(++next,fn);return next;},cancelFrame:id=>frames.delete(id),
    setTimer:fn=>{timers.set(++next,fn);return next;},clearTimer:id=>timers.delete(id),
    paint:v=>{latest=v;painted.push(v);},settled:()=>done++});
  return {motion,painted,frames,timers,get latest(){return latest;},get done(){return done;},
    reduce(){reduced=true;},tick(time){at=time;const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn(time));},
    deadline(){[...timers.values()].forEach(fn=>fn());}};
}
const fit={zoom:1,panX:0,panY:0},actual={zoom:8,panX:-700,panY:350};

test('easing is monotonic, anchored, bounded, and lands exactly at the target',()=>{
  const view=clock();view.motion.start(fit,actual);
  assert.deepEqual(view.latest,fit);
  for(let at=16;at<240;at+=16) {
    const before=view.latest.zoom;view.tick(at);
    assert.ok(view.latest.zoom>before && view.latest.zoom<8);
    assert.ok(Math.abs(view.latest.panX+100*view.latest.zoom-100)<1e-8);
    assert.equal(view.frames.size,1);assert.equal(view.timers.size,1);
  }
  view.tick(240);
  assert.deepEqual(view.latest,actual);assert.equal(view.done,1);
  assert.equal(view.frames.size,0);assert.equal(view.timers.size,0);
});

test('Reduce Motion applies the exact endpoint immediately and also stops an active move',()=>{
  const view=clock(true);view.motion.start(actual,fit);
  assert.deepEqual(view.painted,[fit]);assert.equal(view.done,1);assert.equal(view.frames.size,0);
  const active=clock();active.motion.start(fit,actual);active.tick(32);active.reduce();active.tick(48);
  assert.deepEqual(active.latest,actual);assert.equal(active.done,1);
});

test('hidden-window deadline completes without RAF and cancellation leaves no later work',()=>{
  const view=clock();view.motion.start(fit,actual);view.deadline();
  assert.deepEqual(view.latest,actual);assert.equal(view.done,1);assert.equal(view.frames.size,0);
  view.motion.start(actual,fit);view.tick(30);const stopped=view.latest;
  view.motion.cancel();view.tick(1000);view.deadline();
  assert.deepEqual(view.latest,stopped);assert.equal(view.done,1);assert.equal(view.timers.size,0);
});

test('retargeting starts at the visible frame and replaces both scheduled callbacks',()=>{
  const view=clock();view.motion.start(fit,actual);view.tick(80);
  const visible=view.latest;view.motion.start(visible,fit);
  assert.deepEqual(view.latest,visible);assert.equal(view.frames.size,1);assert.equal(view.timers.size,1);
  view.tick(320);assert.deepEqual(view.latest,fit);assert.equal(view.done,1);
});

test('native viewport layout follows each animation frame without a second RAF delay',()=>{
  const source=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const funcs=source.slice(source.indexOf('function flushNativeViewportLayout()'),source.indexOf('function scheduleNativeHelper('));
  const sent=[],queued=[],cancelled=[];
  let x=0;const motion={active:true};
  const schedule=new Function('NATIVE_PREVIEW','zoomMotion','nativeViewportPayload','postNative','requestAnimationFrame','cancelAnimationFrame',
    `let nativeLayoutFrame=4,lastNativeViewportKey='';${funcs};return scheduleNativeViewportLayout;`)(
      true,motion,()=>({x}),(name,value)=>sent.push(value),fn=>{queued.push(fn);return 9;},id=>cancelled.push(id));
  schedule();x=20;schedule();
  assert.deepEqual(sent,[{x:0},{x:20}]);assert.deepEqual(cancelled,[4]);assert.equal(queued.length,0);
  motion.active=false;x=30;schedule();schedule();
  assert.equal(queued.length,1);queued[0]();assert.deepEqual(sent.at(-1),{x:30});
});
