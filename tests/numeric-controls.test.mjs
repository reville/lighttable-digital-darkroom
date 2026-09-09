import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {syncNumericControl, readNumericControl} from '../web/numeric-controls.js';
import {blendPresetState, reconcilePresetAdjustment} from '../web/preset-amount.js';

// Model the browser's range-value sanitization. The live application journey
// additionally exercises these same controls in a browser with a real catalog.
function range(min, max, step) {
  let value;
  return {
    get value() { return value; },
    set value(input) {
      value = String(Number(Math.max(min, Math.min(max,
        min + Math.round((Number(input) - min) / step) * step)).toFixed(10)));
    },
  };
}

test('Cinematic Color retains its baseline across every amount and control readback', () => {
  const base = {params:{print_exposure:.8,grain_amount:.35,glare_amount:.2,
    stock:'kodak_portra_400',paper:'kodak_2383',profile_enabled:true,
    development_time:0,print_development_time:0,learned_denoise_strength:.6},
    grade:{contrast:.08,saturation:-.04},masks:[],heals:[],optics:{}};
  const target = {...structuredClone(base),
    params:{...base.params,print_exposure:1,grain_amount:.4,glare_amount:.15},
    grade:{contrast:.07,saturation:-.07}};
  const source = fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const html = fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const inputs = {};
  for (const id of ['print_exposure','grain_amount','glare_amount']) {
    const tag = html.match(new RegExp(`<input[^>]+id="${id}"[^>]*>`))[0];
    const number = name => Number(tag.match(new RegExp(`${name}="([^"]+)"`))[1]);
    inputs[id] = range(number('min'),number('max'),number('step'));
  }
  const el = id => inputs[id] ||= {value:'',attributes:{},
    classList:{toggle(){},contains(){return false;}},
    setAttribute(key,value){this.attributes[key]=value;},
    getAttribute(key){return this.attributes[key];},removeAttribute(){}};
  const state = structuredClone(base), noop = () => {};
  const context = {S:state,$:el,document:{querySelectorAll:()=>[]},
    FILM_SLIDERS:['print_exposure','grain_amount','glare_amount'],FILM_SELECTS:[],FILM_TOGGLES:[],
    filmChoiceValue:params=>params.stock,filmSelectionForChoice:stock=>({stock}),
    populatePaperOptions:noop,populateDevelopmentTimes:()=>{el('development_time').value=state.params.development_time;},
    populatePrintDevelopmentTimes:()=>{el('print_development_time').value=state.params.print_development_time;},
    syncFilmReadout:noop,selectedFilmProfile:()=>null,selectedPaperProfile:()=>null,
    isRawInput:()=>false,cur:()=>({name:'a'}),syncEngineForProfile:noop,tr:x=>x,
    syncNumericControl,readNumericControl};
  const functions = ['syncControls','readControls'].map(name=>
    source.match(new RegExp(`function ${name}\\(\\) \\{[\\s\\S]*?\\n\\}`))[0]).join('\n');
  const controls = new Function(...Object.keys(context),`${functions};return {syncControls,readControls};`)(...Object.values(context));
  for (let amount = 0; amount <= 100; amount++) {
    const preset = {id:'cinematic',name:'Cinematic Color',base,target,amount,enabled:amount > 0};
    Object.assign(state,blendPresetState(base,target,amount));
    controls.syncControls(); controls.readControls();
    const retained = reconcilePresetAdjustment(preset,state);
    assert.ok(retained, `Amount ${amount} lost the preset`);
    assert.deepEqual(blendPresetState(retained.base,retained.target,0),base);
    assert.deepEqual(blendPresetState(retained.base,retained.target,amount),state);
  }
});

test('loading another photo and untouched out-of-range values never change the model', () => {
  const input = range(.1,2.5,.05), other = range(.1,2.5,.05);
  for (const value of [.9137, 3.75, .12123, .8]) {
    syncNumericControl(input,value);
    assert.equal(readNumericControl(input,value),value);
  }
  other.value = 1.5;
  assert.equal(readNumericControl(other,1),1.5);
  input.value = 1.25;
  assert.equal(readNumericControl(input,.8),1.25);
});

test('a deliberate input at the rounded thumb position is still a manual edit', () => {
  const source = fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const events = source.slice(source.indexOf('/* ---------------------------------------------------------------- events */'));
  const handler = events.match(/input.addEventListener\('input', \(\) => \{([\s\S]*?)\n  \}\);/)[1];
  const input = range(0,3,.05), S = {editingName:'a',params:{glare_amount:.1725}};
  syncNumericControl(input,S.params.glare_amount);
  assert.equal(readNumericControl(input,S.params.glare_amount),.1725);
  const history = [];
  new Function('S','id','input','syncFilmReadout','renderPhysicalPreview','pushUndo',
    `let gesturePhoto = null; ${handler}`)(
    S,'glare_amount',input,()=>{},()=>{},()=>history.push(structuredClone(S)));
  assert.equal(S.params.glare_amount,.15);
  assert.equal(history[0].params.glare_amount,.1725);
});

test('film slider gestures create one undo step before changing a tracked preset', () => {
  const source = fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const start = source.indexOf('FILM_SLIDERS.forEach',source.indexOf('/* ---------------------------------------------------------------- events */'));
  const block = source.slice(start,source.indexOf('FILM_TOGGLES.forEach',start));
  const input = range(0,3,.05), handlers = {};
  input.addEventListener = (type,callback) => { handlers[type] = callback; };
  input.closest = () => null;
  const S = {editingName:'a',params:{glare_amount:.1725},preset:{id:'cinematic',amount:55}};
  const history = [], noop = () => {};
  syncNumericControl(input,S.params.glare_amount);
  new Function('FILM_SLIDERS','$','S','pushUndo','syncFilmReadout','renderPhysicalPreview',
    'saveState','renderFilm','syncNumericControl','readNumericControl',block)(
    ['glare_amount'],()=>input,S,()=>history.push(structuredClone(S)),noop,noop,noop,noop,
    syncNumericControl,readNumericControl);
  input.value = .25; handlers.input();
  input.value = .3; handlers.input(); handlers.change(); handlers.change();
  assert.equal(history.length,1);
  assert.equal(history[0].params.glare_amount,.1725);
  assert.equal(history[0].preset.amount,55);
  input.value = .4; handlers.input(); handlers.change();
  assert.equal(history.length,2);
  assert.equal(history[1].params.glare_amount,.3);
});
