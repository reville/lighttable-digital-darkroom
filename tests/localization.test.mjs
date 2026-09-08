import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import { t, tn, useCatalog, launchLanguage, resolveLocale, saveLanguage,
  initializeLanguage, currentLocale } from '../web/i18n.js';
const codes = ['en','es','fr','de','it','pt','ru','zh-Hans','zh-Hant','ja','ko','ar','hi','bn','id','tr','vi','th','pl','nl'];
const manifest = { version: 1, locales: codes.map(code => ({ code })) };
const response = (body, ok = true) => ({ ok, json: async () => body });

test('first launch uses primary OS language; English variants skip choice', () => {
  for (const code of ['en', 'en-US', 'en_GB']) assert.deepEqual(launchLanguage({}, [code], codes), { locale:'en', ask:false, save:true });
  assert.deepEqual(launchLanguage({}, ['de-DE','en-US'], codes), { locale:'de', ask:true, save:true });
  assert.deepEqual(launchLanguage({}, ['uk-UA','de-DE'], codes), { locale:'en', ask:true, save:true });
  assert.deepEqual(launchLanguage({locale:'ja',localeChosen:true}, ['ar'], codes), { locale:'ja', ask:false, save:false });
  assert.equal(launchLanguage({locale:'deleted',localeChosen:true}, ['es'], codes).ask, true);
});
test('Chinese script/region matching retains traditional and simplified variants', () => {
  for (const code of ['zh-TW','zh-HK','zh-Hant','zh-Hant-US','zh_MO']) assert.equal(resolveLocale(code,codes),'zh-Hant');
  for (const code of ['zh','zh-CN','zh-SG','zh-Hans-HK']) assert.equal(resolveLocale(code,codes), code === 'zh-Hans-HK' ? 'zh-Hans' : 'zh-Hans');
  assert.equal(resolveLocale('pt-PT',codes),'pt');
});
test('gettext substitutes data only once and never treats data as a source', () => {
  useCatalog('de',{messages:{'Export {name}':'{name} exportieren','Sunset':'Sonnenuntergang'}},manifest);
  assert.equal(t('Export {name}',{name:'Sunset {name}.jpg'}),'Sunset {name}.jpg exportieren');
  assert.equal(t('Missing {name}'),'Missing {name}');
});
test('Arabic and Polish select actual language plural categories', () => {
  const one='{count} photo', other='{count} photos';
  const forms={zero:'zero {count}',one:'one {count}',two:'two {count}',few:'few {count}',many:'many {count}',other:'other {count}'};
  useCatalog('ar',{messages:{},plurals:{[one]:forms}},manifest);
  for (const [count,category] of [[0,'zero'],[1,'one'],[2,'two'],[3,'few'],[11,'many'],[1.5,'other']]) assert.match(tn(one,other,count),new RegExp(`^${category} `));
  useCatalog('pl',{messages:{},plurals:{[one]:forms}},manifest);
  for (const [count,category] of [[1,'one'],[2,'few'],[5,'many'],[1.5,'other']]) assert.match(tn(one,other,count),new RegExp(`^${category} `));
});
test('failed language save does not report success or change current locale', async () => {
  useCatalog('en',{messages:{}},manifest);
  const calls=[];
  const fetcher=async (url, options) => {
    calls.push([url,options]);
    if (url.endsWith('.json')) return response({version:1,locale:'fr',messages:{}});
    return response({error:'disk full'},false);
  };
  await assert.rejects(saveLanguage('fr',fetcher),/Could not save/);
  assert.equal(currentLocale(),'en');
  assert.deepEqual(JSON.parse(calls[1][1].body),{locale:'fr',localeChosen:true});
});
test('English launch persists the decision, subsequent launch never asks', async () => {
  let prefs={}; let saves=0; let asked=0;
  const fetcher=async (url,options) => {
    if(url.endsWith('manifest.json')) return response(manifest);
    if(url.endsWith('/en.json')) return response({version:1,locale:'en',messages:{}});
    if(options) { prefs={...prefs,...JSON.parse(options.body)}; saves++; return response({ok:true}); }
    return response(prefs);
  };
  await initializeLanguage({fetcher,languages:['en-AU'],choose:()=>{asked++;}});
  await initializeLanguage({fetcher,languages:['es-ES'],choose:()=>{asked++;}});
  assert.equal(saves,1); assert.equal(asked,0); assert.equal(currentLocale(),'en');
});
test('non-English launch waits for explicit selection, then uses that catalog', async () => {
  let release;
  const fetcher=async url => response(url.endsWith('manifest.json') ? manifest : url.endsWith('/ja.json') ? {version:1,locale:'ja',messages:{Hello:'こんにちは'}} : {});
  let done=false;
  const boot=initializeLanguage({fetcher,languages:['ko-KR'],choose:async suggested=>{assert.equal(suggested,'ko');await new Promise(resolve=>{release=resolve;});return 'ja';}}).then(()=>{done=true;});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false);
  release();await boot;assert.equal(t('Hello'),'こんにちは');
});

// Exercise the production command with a catalog that would visibly corrupt an ID.
test('localized automation keeps DOM identifiers stable when filtering by color', async () => {
  useCatalog('fr', {messages:{labelFilter:'filtreEtiquette'}}, manifest);
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function executeUICommand(');
  const end = source.indexOf("\nif ($('allowAutomation'))", start);
  assert.ok(start >= 0 && end > start);
  const fields = Object.fromEntries(['filter','ratingFilter','kindFilter','labelFilter','search'].map(id => [id, {value:''}]));
  let refreshed = false;
  const context = vm.createContext({tr:t, $:id=>fields[id], refreshFilteredView:()=>{refreshed=true;}, uiStateReport:()=>({})});
  vm.runInContext(source.slice(start,end), context);
  await context.executeUICommand('filter', {label:'red',query:'Sunset.jpg'});
  assert.equal(fields.labelFilter.value,'red');
  assert.equal(fields.search.value,'Sunset.jpg');
  assert.equal(refreshed,true);
});
