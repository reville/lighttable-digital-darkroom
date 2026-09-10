import assert from 'node:assert/strict';
import test from 'node:test';
import {editOverlayCursor, radialHandleCursor, ROTATE_CURSOR, healHandleAt, installCanvasHandleCursor} from '../web/edit-cursor.js';
import {radialHandles,radialHandleAt} from '../web/mask-shape.js';
const rect={left:0,top:0,width:900,height:600};
const mask={type:'radial',center:[.5,.5],radiusX:.2,radiusY:.15,angle:0};
const state={activePane:'maskPane',localPinsVisible:true,overlayHoverPoint:[.5,.5]};

test('radial center, axes and rotation show their actual operations',()=>{
 const locations=radialHandles(mask,rect.width,rect.height);
 for(const [handle,expected] of Object.entries({center:'grab',x:'ew-resize',y:'ns-resize',rotate:ROTATE_CURSOR})){
  const point=locations[handle];assert.equal(radialHandleAt(mask,point,rect),handle);
  assert.equal(editOverlayCursor({...state,overlayHoverPoint:point},mask,rect),expected);
  assert.equal(editOverlayCursor({...state,editGesture:{type:'radial',handle}},mask,rect),handle==='center'?'grabbing':expected);
 }
 assert.equal(editOverlayCursor({...state,overlayHoverPoint:[.1,.1]},mask,rect),'crosshair');
});
test('resize arrows follow rotated axes at all photo aspect ratios and zoom levels',()=>{
 for(const angle of [0,45,90,135,180,-45])for(const dimensions of [{width:900,height:600},{width:600,height:900},{width:7200,height:4800}]){
  const rotated={...mask,angle};const handles=radialHandles(rotated,dimensions.width,dimensions.height);
  for(const handle of ['x','y']){
   const hit=[handles[handle][0],handles[handle][1]+10/dimensions.height];
   assert.equal(radialHandleAt(rotated,hit,dimensions),handle);
   assert.equal(editOverlayCursor({...state,overlayHoverPoint:hit},rotated,dimensions),radialHandleCursor(handle,angle));
  }
 }
 assert.equal(radialHandleCursor('x',45),'nwse-resize');assert.equal(radialHandleCursor('y',45),'nesw-resize');
});
test('hidden pins, sampling and Option/refine painting override hover; captured gestures retain their cursor',()=>{
 assert.equal(editOverlayCursor({...state,localPinsVisible:false},mask,rect),'crosshair');
 assert.equal(editOverlayCursor({...state,maskColorPick:true},mask,rect),'crosshair');
 for(const mode of [{maskRefineMode:'subtract'},{overlayAltKey:true}]){
  assert.equal(editOverlayCursor({...state,...mode},mask,rect),'none');
  assert.equal(editOverlayCursor({...state,...mode,editGesture:{type:'radial',handle:'center'}},mask,rect),'grabbing');
 }
 assert.equal(editOverlayCursor({...state,overlayHoverPoint:null},mask,rect),'crosshair');
 assert.equal(editOverlayCursor({...state,activePane:'editPane'},mask,rect),'');
 assert.equal(editOverlayCursor(state,{type:'subject'},rect),'default');
});
test('healing source and target use matching hit areas; hidden pins and placement do not suggest dragging',()=>{
 const spot={id:'a',mode:'heal',radius:.05,source:[.2,.2],target:[.6,.6]};
 const healing={...state,activePane:'healPane',heals:[spot],selectedHealId:'a'};
 for(const handle of ['source','target']){
  const point=spot[handle];assert.equal(healHandleAt([spot],'a',point,rect).handle,handle);
  assert.equal(editOverlayCursor({...healing,overlayHoverPoint:point},null,rect),'grab');
  assert.equal(editOverlayCursor({...healing,overlayHoverPoint:point,localPinsVisible:false},null,rect),'none');
  assert.equal(editOverlayCursor({...healing,editGesture:{type:`heal-move-${handle}`}},null,rect),'grabbing');
 }
 assert.equal(editOverlayCursor({...healing,overlayHoverPoint:spot.target,editGesture:{type:'heal-place-target'}},null,rect),'none');
 assert.equal(healHandleAt([{...spot,mode:'remove'}],'a',spot.source,rect),null);
});
test('canvas hover stays consistent through dragging, leaving, cancellation and geometry changes',()=>{
 const listeners={};let dragging=false,bounds={left:0,top:0,width:100,height:100};
 const canvas={style:{},getBoundingClientRect:()=>bounds,addEventListener:(type,fn)=>listeners[type]=fn};
 const refresh=installCanvasHandleCursor(canvas,{hitCursor:point=>point.clientX===50?'grab':'crosshair',dragCursor:()=>dragging?'grabbing':null});
 const at=(type,x=50)=>listeners[type]({clientX:x,clientY:50});
 at('pointermove');assert.equal(canvas.style.cursor,'grab');
 dragging=true;at('pointerdown');assert.equal(canvas.style.cursor,'grabbing');
 at('pointerleave');assert.equal(canvas.style.cursor,'grabbing');
 dragging=false;at('pointerup',150);assert.equal(canvas.style.cursor,'');
 at('pointermove');assert.equal(canvas.style.cursor,'grab');
 bounds={...bounds,left:100};refresh();assert.equal(canvas.style.cursor,'');
 bounds={...bounds,left:0};at('pointermove');at('pointercancel');assert.equal(canvas.style.cursor,'');
});
