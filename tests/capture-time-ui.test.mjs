import assert from 'node:assert/strict';
import test from 'node:test';
import {installCaptureTime,captureSortValue} from '../web/capture-time.js';
const change={name:'a.jpg',fileId:1,original:'2026-01-01T12:00:00',before:'2026-01-01T12:00:00',beforeOverride:null,after:'2026-01-01T13:00:00'};
function harness(flush=async()=>true) {
  const nodes=new Map(),calls=[],notifications=[];
  const el=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,disabled:false,handlers:{},addEventListener(type,fn){this.handlers[type]=fn;}});return nodes.get(id);};
  let selection=['a.jpg'],transport=async body=>body.action==='preview'?{ok:true,count:1,changes:[structuredClone(change)],skipped:[]}:{ok:true,count:1,names:['a.jpg']};
  const controller=installCaptureTime({el,selection:()=>selection,post:async(path,body)=>{calls.push({path,body});return transport(body);},toast:message=>notifications.push(message),flush,changed:async()=>{}});
  return {nodes,el,calls,notifications,controller,transport:value=>{transport=value;},select:value=>{selection=value;controller.selectionChanged();}};
}
test('preview is read-only and changing settings invalidates the exact plan',async()=>{
  const h=harness();h.el('captureShiftHours').value='1';
  await h.el('captureTimePreview').onclick();
  assert.deepEqual(h.calls.map(c=>c.body.action),['preview']);assert.equal(h.el('captureTimeApply').disabled,false);
  h.el('captureShiftHours').value='2';h.el('captureShiftHours').handlers.input();
  await h.el('captureTimeApply').onclick();
  assert.equal(h.calls.length,1);assert.equal(h.el('captureTimeApply').disabled,true);
});
test('a selection change while preview is in flight cannot apply its old result',async()=>{
  const h=harness();let resolve;h.transport(()=>new Promise(r=>resolve=r));
  const pending=h.el('captureTimePreview').onclick();h.select(['b.jpg']);
  resolve({ok:true,count:1,changes:[change],skipped:[]});await pending;
  await h.el('captureTimeApply').onclick();assert.equal(h.calls.length,1);assert.equal(h.el('captureTimeApply').disabled,true);
});
test('apply uses reviewed file identities and Undo restores only their former overrides',async()=>{
  const h=harness();await h.el('captureTimePreview').onclick();await h.el('captureTimeApply').onclick();
  assert.deepEqual(h.calls[1].body.changes,[change]);assert.equal(h.el('captureTimeUndo').disabled,false);
  h.select(['other.jpg']);await h.el('captureTimeUndo').onclick();
  const restored=h.calls[2].body.changes[0];
  assert.equal(restored.name,'a.jpg');assert.equal(restored.beforeOverride,change.after);assert.equal(restored.after,null);
  assert.equal(h.el('captureTimeUndo').disabled,true);
});
test('a rejected save keeps the reviewed proposal available and reports failure',async()=>{
  const h=harness();await h.el('captureTimePreview').onclick();h.transport(async()=>({error:'Capture time changed since preview'}));
  await h.el('captureTimeApply').onclick();assert.match(h.el('captureTimeStatus').textContent,/changed since preview/);
  assert.equal(h.el('captureTimeApply').disabled,false);assert.equal(h.el('captureTimeUndo').disabled,true);
});
test('pending edit flush blocks duplicate corrections and a failed flush keeps the plan',async()=>{
  let resolve;
  const h=harness(()=>new Promise(r=>resolve=r));
  await h.el('captureTimePreview').onclick();
  const first=h.el('captureTimeApply').onclick();
  await h.el('captureTimeApply').onclick();
  assert.equal(h.calls.length,1);
  resolve(false);await first;
  assert.equal(h.calls.length,1);
  assert.equal(h.el('captureTimeApply').disabled,false);
  assert.match(h.el('captureTimeStatus').textContent,/pending edits/);
});

test('date sorting keeps explicit time zones in capture order and accepts legacy camera timestamps',()=>{
  assert.ok(captureSortValue({date:'2026-01-01T12:00:00+05:00'}) < captureSortValue({date:'2026-01-01T09:00:00Z'}));
  assert.equal(captureSortValue({date:'2026:01:01 12:00:00'}),captureSortValue({captureTime:'2026-01-01T12:00:00Z'}));
  assert.equal(captureSortValue({mtime:100}),100000);
});
