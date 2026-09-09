import assert from 'node:assert/strict';
import test from 'node:test';
import {cullFlagTargets, createCullBatch} from '../web/cull-batch.js';

function harness() {
  const images = [
    {name:'unflagged.raw', status:'pending', grade:{exposure:2}},
    {name:'picked.jpg', status:'approved'}, {name:'rejected.jpg', status:'skipped'},
  ];
  let saved = true, wait = null;
  const writes = [];
  const batch = createCullBatch({imageFor:name=>images.find(im=>im.name===name),
    enqueue(image, patch) { batch.noteFlagChange(image.name, patch); Object.assign(image,patch); writes.push([image.name,patch.status]); },
    flush:async()=>wait ? await wait : saved, changed() {},
  });
  return {images,batch,writes,setSaved(value){saved=value;},setWait(value){wait=value;}};
}

test('protect existing flags after expanding and deduplicating linked RAW/JPEG targets',()=>{
  const {images}=harness();
  assert.deepEqual(cullFlagTargets([...images,images[0]],'approved').map(im=>im.name),['unflagged.raw']);
  assert.deepEqual(cullFlagTargets(images,'approved',true).map(im=>im.name),['unflagged.raw','rejected.jpg']);
});
test('batch undo restores previous mixed flags without touching edits',async()=>{
  const {images,batch}=harness();
  await batch.apply(images,'skipped',true);
  assert.deepEqual(images.map(im=>im.status),['skipped','skipped','skipped']);
  assert.equal((await batch.undo()).count,2);
  assert.deepEqual(images.map(im=>im.status),['pending','approved','skipped']);
  assert.deepEqual(images[0].grade,{exposure:2});
});
test('later manual and external decisions survive undo, including same-value decisions',async()=>{
  const {images,batch}=harness();
  await batch.apply(images,'approved',true);
  batch.noteFlagChange(images[0].name,{status:'approved'});
  batch.noteFlagChange(images[2].name,{status:'pending'}); images[2].status='pending';
  assert.equal((await batch.undo()).count,0);
  assert.deepEqual(images.map(im=>im.status),['approved','approved','pending']);
});
test('failed saves retain undo so a partial batch can be restored through the same queue',async()=>{
  const h=harness(); h.setSaved(false);
  assert.equal((await h.batch.apply(h.images,'approved')).saved,false);
  assert.equal(h.batch.canUndo,true);
  h.setSaved(true);
  assert.equal((await h.batch.undo()).saved,true);
  assert.equal(h.images[0].status,'pending');
});
test('double clicks cannot enqueue another batch while the first is saving',async()=>{
  const h=harness(); let resolve;
  h.setWait(new Promise(r=>resolve=r));
  const first=h.batch.apply(h.images,'approved');
  assert.equal(h.batch.busy,true);
  assert.equal(await h.batch.apply(h.images,'skipped',true),null);
  assert.equal(await h.batch.undo(),null);
  resolve(true); await first;
  assert.equal(h.writes.length,1);
});
test('multiple batches unwind in order',async()=>{
  const {images,batch}=harness();
  await batch.apply([images[0]],'approved');
  await batch.apply([images[0]],'skipped',true);
  await batch.apply([images[0]],'approved',true);
  await batch.undo(); assert.equal(images[0].status,'skipped');
  await batch.undo(); assert.equal(images[0].status,'approved');
  await batch.undo(); assert.equal(images[0].status,'pending');
});
test('removed photos are not resurrected by undo',async()=>{
  const {images,batch}=harness();
  await batch.apply(images,'approved'); images.shift();
  assert.equal((await batch.undo()).count,0);
});
