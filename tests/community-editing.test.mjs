import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const moduleFrom = async file => import(`data:text/javascript;base64,${Buffer.from(readFileSync(new URL(file, import.meta.url))).toString('base64')}`);
const {transferChoices, transferPatch, regenerateTransferMasks, cropGeometry, restoreCropGeometry} = await moduleFrom('../web/edit-transfer.js');
const {indexPairs, pairViewPreference, collapsePairs, pairedTargets} = await moduleFrom('../web/photo-pairs.js');
const only = (...selected) => Object.fromEntries(Object.keys(transferChoices()).map(key => [key, selected.includes(key)]));
const source = {params: {stock: 'source', rotate: 90, wb_temperature: 5600}, grade: {exposure: 2, temp: .3, curveL: [.1,.9], clarity: .4}, optics: {rotate: 2, distortion: .1}, crop: {x:.2,y:.1,w:.5,h:.5}, masks: [], heals: [{source:[.1,.1],target:[.2,.2]}]};
const destination = {params:{stock:'dest',rotate:0,wb_temperature:3200}, grade:{exposure:-1,temp:-.2,curveR:[0,.8],clarity:.1,hsl:{red:[.1,0,0]}}, optics:{rotate:-3,distortion:.6},crop:null,masks:[{type:'radial'}]};
test('tone-only paste preserves destination color, film, crop, masks and detail', () => {
  const before = structuredClone(destination);
  const patch = transferPatch(source,destination,only('tone'));
  assert.equal(patch.grade.exposure,2); assert.deepEqual(patch.grade.curveL,[.1,.9]);
  assert.equal(patch.grade.temp,-.2); assert.equal(patch.grade.clarity,.1);
  assert.deepEqual(patch.grade.curveR,[0,.8]); assert.deepEqual(patch.grade.hsl,destination.grade.hsl);
  assert.deepEqual(Object.keys(patch),['grade']); assert.deepEqual(destination,before);
});
test('selected color reset removes source-absent curves without altering tone', () => {
  const patch=transferPatch(source,destination,only('color'));
  assert.equal(patch.grade.temp,.3); assert.equal(patch.grade.exposure,-1);
  assert.equal(patch.grade.curveR,undefined); assert.equal(patch.grade.hsl,undefined);
});
test('lens corrections and crop geometry transfer independently', () => {
  let patch=transferPatch(source,destination,only('optics'));
  assert.equal(patch.optics.distortion,.1); assert.equal(patch.optics.rotate,-3); assert.equal(patch.crop,undefined);
  patch=transferPatch(source,destination,only('crop'));
  assert.equal(patch.params.rotate,90); assert.equal(patch.params.stock,'dest');
  assert.equal(patch.optics.rotate,2); assert.equal(patch.optics.distortion,.6);
  assert.deepEqual(patch.crop,source.crop); patch.crop.x=.8; assert.equal(source.crop.x,.2);
});
test('RAW development does not copy the film look or post-film white balance', () => {
  const patch=transferPatch(source,destination,only('raw'));
  assert.deepEqual(patch.params,{stock:'dest',rotate:0,wb_temperature:5600}); assert.equal(patch.grade,undefined);
  assert.equal(transferChoices().crop,false); assert.equal(transferChoices().masks,false);
  assert.deepEqual(transferChoices(only('heals')),only('heals'));
});
test('AI selections are regenerated once per type for each destination and retain Boolean intent', async () => {
  const masks=[{name:'Subject',type:'subject',bitmap:{data:'source'},opacity:.5,grade:{exposure:.3},components:[{type:'subject',bitmap:{data:'source'},invert:true},{type:'sky',combine:'subtract',bitmap:{data:'sourceSky'}}]}, {type:'subject',bitmap:{data:'source'}}];
  const calls=[];
  const result=await regenerateTransferMasks(masks, async type => {calls.push(type); return {bitmap:{data:`target-${type}`},provider:'test'};});
  assert.deepEqual(calls,['subject','sky']); assert.equal(result[0].bitmap.data,'target-subject');
  assert.equal(result[0].components[0].invert,true); assert.equal(result[0].components[1].combine,'subtract');
  assert.equal(result[0].components[1].bitmap.data,'target-sky'); assert.equal(result[1].bitmap.data,'target-subject');
  assert.deepEqual(result[0].grade,{exposure:.3}); assert.equal(masks[0].bitmap.data,'source');
  const again=await regenerateTransferMasks(masks,async type=>({bitmap:{data:`another-${type}`}}));
  assert.equal(again[0].bitmap.data,'another-subject');
});
test('unrecoverable object intent, painted AI refinements and detector errors refuse a target', async () => {
  await assert.rejects(regenerateTransferMasks([{type:'object'}],async()=>assert.fail()),/new object selection/);
  await assert.rejects(regenerateTransferMasks([{components:[{type:'subject'},{type:'brush',strokes:[]}]}],async()=>assert.fail()),/painted refinements/);
  await assert.rejects(regenerateTransferMasks([{type:'subject'}],async()=>({error:'Model unavailable'})),/Model unavailable/);
  for (const key of ['addStrokes','subtractStrokes','intersectStrokes']) {
    await assert.rejects(regenerateTransferMasks([{type:'subject',[key]:[{points:[[.3,.4]]}]}],async()=>assert.fail()),/painted refinements/);
  }
  const masks=[{type:'brush',strokes:[{points:[[.3,.4]]}]}];
  assert.deepEqual(await regenerateTransferMasks(masks,async()=>assert.fail()),masks);
  assert.deepEqual(await regenerateTransferMasks([{type:'object'}],async()=>assert.fail(),{samePhoto:true}),[{type:'object'}]);
});
test('Crop Cancel restores entry crop and geometry while preserving other edits', () => {
  const entry=cropGeometry({...destination,cropChoices:{aspect:'3:2'}});
  const changed={...structuredClone(source),grade:{exposure:4},rating:5,keywords:['keep'],optics:{...source.optics,distortion:.9}};
  const restored=restoreCropGeometry(changed,entry);
  assert.equal(restored.crop,null); assert.equal(restored.params.rotate,0); assert.equal(restored.optics.rotate,-3);
  assert.equal(restored.optics.distortion,.9); assert.deepEqual(restored.grade,{exposure:4});
  assert.equal(restored.rating,5); assert.deepEqual(restored.keywords,['keep']);
  assert.deepEqual(restored.cropChoices,{aspect:'3:2'}); assert.equal(changed.params.rotate,90);
});
const raw={name:'1:trip/IMG.CR3',raw:true}, jpeg={name:'1:trip/IMG.JPG',raw:false};
test('RAWJPEG pairs use complete source/folder identity and exclude virtual or ambiguous variants', () => {
  const others=[{name:'2:trip/IMG.JPG',raw:false},{name:'1:elsewhere/IMG.JPG',raw:false},{name:'1:trip/IMG.CR3::copy',raw:true,virtual:true}];
  const pairs=indexPairs([raw,jpeg,...others]); assert.equal(pairs.size,2);
  assert.deepEqual(pairedTargets([raw,jpeg],pairs,true),[raw,jpeg]); assert.deepEqual(pairedTargets([raw],pairs,false),[raw]);
  assert.equal(indexPairs([raw,jpeg,{name:'1:trip/IMG.JPEG',raw:false}]).size,0);
});
test('preferred view retains filtered-only companions and honors explicit editor switches', () => {
  assert.deepEqual(collapsePairs([raw,jpeg],'raw'),[raw]); assert.deepEqual(collapsePairs([raw,jpeg],'jpeg'),[jpeg]);
  assert.deepEqual(collapsePairs([jpeg],'raw'),[jpeg]); assert.deepEqual(collapsePairs([raw],'jpeg'),[raw]);
  assert.deepEqual(collapsePairs([raw,jpeg],'raw',new Map([['1:trip/img',jpeg.name]])),[jpeg]);
  assert.equal(pairViewPreference({hidePairedJPEG:true}),'raw'); assert.equal(pairViewPreference({pairRawJPEG:false,pairView:'raw'}),'both');
});

// Exercise the actual paste orchestrator: a state-load failure and an AI error
// must leave each target untouched, while subsequent safe targets still save.
const app=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const pasteSource=app.slice(app.indexOf('async function pasteSettingsTo('),app.indexOf("$('pasteBtn').onclick =",app.indexOf('async function pasteSettingsTo(')));
function pasteHarness(images, {clipboard={...source,sourceName:'source.raw',choices:only('tone')}, failLoad=false, semanticError=false}={}) {
  const calls=[], notices=[], nodes=new Map();
  const context={S:{clipboard,editingName:''},transferRunning:false,transferCancelled:false,
    cloneValue:structuredClone,transferPatch,regenerateTransferMasks,
    $:id=> {if(!nodes.has(id))nodes.set(id,{focus(){}});return nodes.get(id);},
    saveState:async()=>true,prefetchState:async image=>{image.stateLoaded=!failLoad;},isStateLoaded:image=>image.stateLoaded,
    editSaveQueue:{flush:async()=>{}},normalizeFilmParams:p=>({...p}),normalizeOptics:p=>({...p}),GRADE_DEFAULTS:{},
    api:async(path,body)=>{calls.push({path,body}); return path.includes('semantic') ? semanticError?{error:'No model'}:{bitmap:{data:body.name}} : {ok:true};},
    HISTORY:{record(){}},cur:()=>null,displayName:i=>i.name,showTransferDialog(){},closeTransferDialog(){},
    invalidateEditedThumbnail(){},refreshLists(){},confirmTransfer(){},toast:t=>notices.push(t),
  };
  vm.createContext(context); vm.runInContext(pasteSource+'\nthis.run = pasteSettingsTo;',context);
  return {run:()=>context.run(images),calls,nodes,notices};
}
test('paste refuses unloaded edited destinations before writing',async()=>{
  const target={name:'target.raw',...structuredClone(destination)};
  const h=pasteHarness([target],{failLoad:true}); await h.run();
  assert.equal(h.calls.length,0); assert.equal(target.grade.exposure,-1);
  assert.match(h.nodes.get('transferStatus').textContent,/Existing settings could not be loaded/);
});
test('paste orchestrator merges unchecked target edits and submits only selected categories',async()=>{
  const target={name:'target.raw',...structuredClone(destination)};
  const h=pasteHarness([target]); await h.run();
  assert.equal(h.calls.length,1); assert.equal(h.calls[0].body.grade.temp,-.2);
  assert.equal(h.calls[0].body.grade.exposure,2); assert.equal(h.calls[0].body.crop,undefined);
  assert.equal(target.params.stock,'dest'); assert.equal(target.grade.temp,-.2);
});
test('AI failure prevents saving any part of a target paste',async()=>{
  const target={name:'target.raw',...structuredClone(destination)};
  const clipboard={...source,sourceName:'source.raw',masks:[{type:'subject',bitmap:{data:'old'}}],choices:only('tone','masks')};
  const h=pasteHarness([target],{clipboard,semanticError:true}); await h.run();
  assert.equal(h.calls.filter(c=>c.path==='/api/state').length,0); assert.equal(target.grade.exposure,-1);
  assert.match(h.nodes.get('transferStatus').textContent,/No model/);
});
