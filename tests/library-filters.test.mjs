import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import { normalizeFileTypes, photoFileType, photoHasEdits, matchesLibraryFilters, filterChips } from '../web/library-filters.js';
import { collapsePairs, pairViewPreference } from '../web/photo-pairs.js';
import { normalizeMasks, normalizeHeals, normalizeOptics, OPTICS_DEFAULTS } from '../web/editor-panels.js';

const images = [
  {name:'1:Tree.ARW', raw:true, rating:5, status:'approved', hasEdits:true},
  {name:'1:Tree.JPG', rating:4, status:'approved', label:'red'},
  {name:'2:Portrait.JPEG', rating:2, status:'skipped', grade:{exposure:1}},
  {name:'2:Portrait.PNG', rating:4, status:'approved', label:'red'},
  {name:'copy', sourceName:'2:Portrait.TIFF', virtual:true, rating:5, status:'approved'},
];

function harness() {
  const controls = Object.fromEntries(Object.entries({ filter:'all', ratingFilter:'0', kindFilter:'all',
    labelFilter:'all', editFilter:'all', search:'', sort:'name' }).map(([id,value])=>[id,{value}]));
  let types = [];
  const ctx = vm.createContext({ $: id=>controls[id], S:{images,library:{stacks:[]},cull:{review:'all',on:{}}},
    LIBRARY_FILTERS:{types:()=>types}, APP_PREFS:{pairView:'raw'},
    _cachedVisibleList:null, _cachedVisibleKey:'', _visibleEpoch:0, _cachedVisibleImages:null, _cachedVisibleLibrary:null,
    CULL_SELECT:[], CULL_REJECT:[], matchesCullReview:()=>true, collectionScope:()=>images,
    cleanLabel:label=>label||'none', photoHasEdits, matchesLibraryFilters,
    photoMatchesQuery:(im,query)=>im.name.toLowerCase().includes(query.toLowerCase()),
    collapsePairs, pairViewPreference, pairOverrides:new Map() });
  const source = readFileSync(new URL('../web/app.js', import.meta.url),'utf8');
  vm.runInContext(source.slice(source.indexOf('function visible() {'), source.indexOf('\nfunction inFolderScope')),ctx);
  return { controls, types(next) {types=next;}, names:()=>Array.from(ctx.visible(),im=>im.name) };
}

test('file types normalize safely and match source extensions, not display names',()=>{
  assert.deepEqual(normalizeFileTypes(['png','raw','raw','bad']),['raw','png']);
  assert.deepEqual(normalizeFileTypes('raw'),[]);
  for (const [name,type] of [['a.JPG','jpeg'],['a.jpeg','jpeg'],['a.HEIF','heic'],['a.HIF','heic'],['a.TIF','tiff'],['a.TIFF','tiff'],['a.PNG','png'],['a.webp','other']]) {
    assert.equal(photoFileType({name:'virtual id',sourceName:name,displayName:'My RAW photo'}),type);
  }
  assert.equal(photoFileType({name:'capture.DNG',raw:true}),'raw');
});

test('file types combine with OR, other filters with AND, before RAW/JPEG pair collapsing',()=>{
  const h=harness();
  h.types(['jpeg']); assert.deepEqual(h.names(),['1:Tree.JPG','2:Portrait.JPEG']);
  h.types(['jpeg','png']); assert.deepEqual(h.names(),['1:Tree.JPG','2:Portrait.JPEG','2:Portrait.PNG']);
  h.controls.filter.value='approved';h.controls.ratingFilter.value='4';h.controls.labelFilter.value='red';
  assert.deepEqual(h.names(),['1:Tree.JPG','2:Portrait.PNG']);
  h.controls.search.value='portrait'; assert.deepEqual(h.names(),['2:Portrait.PNG']);
  h.controls.editFilter.value='edited'; assert.deepEqual(h.names(),[]);
  h.controls.editFilter.value='unedited'; assert.deepEqual(h.names(),['2:Portrait.PNG']);
});

test('changing only a file type or edit status invalidates the visible-list cache',()=>{
  const h=harness();
  h.types(['raw']); assert.deepEqual(h.names(),['1:Tree.ARW']);
  h.types(['tiff']); assert.deepEqual(h.names(),['copy']);
  h.controls.editFilter.value='virtual'; assert.deepEqual(h.names(),['copy']);
  h.controls.editFilter.value='edited'; assert.deepEqual(h.names(),[]);
  h.controls.editFilter.value='all'; h.types([]); assert.equal(h.names().length,4);
});

test('ratings and empty edit containers do not count as edits',()=>{
  assert.equal(photoHasEdits({rating:5,params:{},grade:{}}),false);
  assert.equal(matchesLibraryFilters({hasEdits:true},[],'unedited'),false);
  assert.equal(matchesLibraryFilters({name:'copy',sourceName:'a.JPG',virtual:true},['jpeg'],'virtual'),true);
});

test('local masks, healing and optics count before the catalog edit flag refreshes',()=>{
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const normalize = source.match(/^function normalizeLibraryImage\([^]*?^}/m)[0];
  const context = vm.createContext({S: {catalogEnabled: false}, normalizeMasks, normalizeHeals, normalizeOptics});
  vm.runInContext(normalize, context);
  for (const changes of [
    {masks: [{id: 'mask', type: 'radial', grade: {exposure: 1}}]},
    {heals: [{id: 'spot', mode: 'clone', target: [.5, .5]}]},
    {optics: {rotate: 2}}, {optics: {scale: 1.1}}, {optics: {profileEnabled: true}},
  ]) {
    const image = context.normalizeLibraryImage({name: 'photo.jpg', hasEdits: false, ...changes});
    assert.equal(matchesLibraryFilters(image, [], 'edited'), true, JSON.stringify(changes));
    assert.equal(matchesLibraryFilters(image, [], 'unedited'), false);
  }
  const neutral = context.normalizeLibraryImage({name: 'photo.jpg', rating: 5});
  assert.deepEqual(JSON.parse(JSON.stringify(neutral.optics)), OPTICS_DEFAULTS);
  assert.equal(photoHasEdits(neutral), false);
  assert.equal(photoHasEdits({...neutral, masks: [], heals: [], optics: {}}), false);
});

test('each active restriction is visible in the summary, including old saved filters',()=>{
  const chips=filterChips({filter:'approved',ratingFilter:'4',labelFilter:'red',editFilter:'unedited',kindFilter:'processed'},['jpeg','png']);
  assert.deepEqual(chips.map(c=>c.id),['type:jpeg','type:png','filter','ratingFilter','labelFilter','editFilter','kindFilter']);
  assert.deepEqual(filterChips({filter:'all',ratingFilter:'0',labelFilter:'all',editFilter:'all',kindFilter:'all'},[]),[]);
});

test('saved collection rules preserve the same file type, flag, rating and edit restrictions',()=>{
  const source=readFileSync(new URL('../web/library.js',import.meta.url),'utf8');
  const ctx=vm.createContext({matchesLibraryFilters,normalizeFileTypes,aiSearchTerms:()=>[]});
  vm.runInContext(source.replace(/^import .*;\n/gm,'').replaceAll('export function','function'),ctx);
  const rules={fileTypes:['jpeg','png'],status:'approved',ratingMin:4,label:'red',editState:'unedited'};
  assert.deepEqual(images.filter(im=>ctx.photoMatchesRules(im,rules)).map(im=>im.name),['1:Tree.JPG','2:Portrait.PNG']);
  assert.equal(ctx.photoMatchesRules(images[1],{...rules,unrated:true}),false);
});
