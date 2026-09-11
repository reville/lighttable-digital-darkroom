// SPDX-License-Identifier: GPL-3.0-only
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {probe} from './probes.mjs';
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = await import(pathToFileURL(cfg.module).href);
const browser = await chromium.launch({headless:true, executablePath:cfg.browser || undefined,
  args:['--enable-unsafe-swiftshader','--force-color-profile=srgb']});
const records = [], errors = [];
let context, page, evidencePage, state, sequence = [], attempt = 0, commandAt = 0;
const save = () => fs.writeFileSync(cfg.result, JSON.stringify({records,errors,browserVersion:browser.version()},null,2));
const deadline = Date.now()+cfg.timeout*1000;
const checkTime = () => {if(Date.now() > deadline) throw Error('Audit wall-clock limit reached');};
const pause = ms => new Promise(r=>setTimeout(r,ms));
const api = async (route, data) => {
  const response = data === undefined ? await page.request.get(cfg.baseUrl+route) :
    await page.request.post(cfg.baseUrl+route,{headers:{Origin:cfg.baseUrl},data});
  if(!response.ok()) throw Error(`${route}: ${response.status()} ${await response.text()}`);
  return response.json();
};
const command = async (name,args={}) => {
  checkTime();
  commandAt=Date.now()/1000;
  const result = await api('/api/ui/command',{command:name,args,timeout:20,origin:'ui-audit'});
  if (!result.ok) throw Error(JSON.stringify(result));
  state = result.result; return state;
};
const settle = async (render = true) => {
  const until = Math.min(deadline,Date.now()+30000);
  do {
    state = await api('/api/ui/state');
    if (state.reportedAt >= commandAt && (!render || !state.current || state.render?.state === 'error' ||
        (state.render?.state === 'ready' && state.render.name === state.current))) {
      await pause(350); await page.evaluate(()=>document.fonts.ready); return;
    }
    await pause(100);
  } while(Date.now() < until);
  throw Error('Photo did not reach a settled render');
};
let images = [];
async function reset(test) {
  checkTime();
  if(context) await context.close();
  context = await browser.newContext({viewport:test.viewport,deviceScaleFactor:1,locale:test.locale});
  page = await context.newPage(); evidencePage = page; page.setDefaultTimeout(10000);
  page.on('pageerror', e=>errors.push({case:test.id,message:String(e)}));
  await page.request.get(cfg.baseUrl);
  await api('/api/prefs',{locale:test.locale,localeChosen:true,allowAutomation:true,
    firstRunSetup:{version:cfg.fixture === 'first-run'?0:1},smoothZoom:true,autoAdvance:false});
  images = (await api('/api/images')).images;
  for(const image of images) await api('/api/state',{name:image.name,
    params:{profile_enabled:false,grain_on:false,halation_on:false,glare_on:false},grade:{},masks:[],heals:[],optics:{},crop:null,
    keywords:image.name.includes('Long-') ? Array.from({length:24},(_,i)=>`Landscape > Long keyword Deutsch Русский العربية ${i}`):[]});
  const oldClient = (await api('/api/ui/state')).client;
  await page.goto(cfg.baseUrl,{waitUntil:'domcontentloaded'});
  const until = Math.min(deadline,Date.now()+45000);
  do {
    state = await api('/api/ui/state');
    if(state.client && state.client !== oldClient) break;
    await pause(100);
  } while(Date.now()<until);
  if(!state.client || state.client === oldClient) throw Error('Fresh browser did not connect to the UI bridge');
  await page.waitForFunction(locale=>document.documentElement.lang===locale,test.locale);
  if(cfg.fixture === 'first-run') {
    await page.locator('#firstRunDialog.on').waitFor(); return;
  }
  await command('filter',{query:'',status:'all',rating:'all',kind:'all',label:'all'});
  await command('view:detail'); await command('pane:edit');
  if(images.length) await command('goto',{name:images[0].name});
  await command('zoomFit'); await settle();
}
async function action(a) {
  if(a.type === 'pointer') {
    const r = await page.locator('#zoomwrap').boundingBox();
    if(!r) throw Error('No pointer viewport');
    const x=r.x+r.width*.5,y=r.y+r.height*.5;
    await page.mouse.move(x,y); await page.mouse.down();
    try {await page.mouse.move(x+a.dx,y+a.dy,{steps:8});} finally {await page.mouse.up();}
  } else if(a.type === 'burst') {
    for(const item of a.actions) {await command(item.command,item.args); await pause(30);}
  } else if(a.command === 'secondaryLoupe') {
    const popup=page.waitForEvent('popup'); await command(a.command,a.args);
    evidencePage=await popup; await evidencePage.setViewportSize(page.viewportSize());
    await evidencePage.waitForLoadState('domcontentloaded');
    await evidencePage.locator('#loupeImg').waitFor({state:'visible'});
    await evidencePage.waitForFunction(()=>document.querySelector('#loupeImg').naturalWidth>0);
  } else await command(a.command,a.args);
}
function caseActions(test) {
  const a=[]; const cmd=(command,args)=>a.push({command,args});
  if(test.pane) cmd(`pane:${test.pane}`);
  if(test.view === 'grid') cmd('view:square');
  if(test.view === 'compare') cmd('compare');
  if(test.view === 'survey') {cmd('select',{action:'set',names:images.slice(0,3).map(i=>i.name)});cmd('survey');}
  if(test.view === 'loupe') cmd('secondaryLoupe');
  if(test.view === 'help') cmd('help');
  if(test.view === 'settings') cmd('preferences');
  if(test.view === 'no-results') {cmd('view:square');cmd('filter',{query:'UI_AUDIT_NO_MATCH_94731'});}
  if(test.view === 'long-name') {cmd('goto',{name:images.find(i=>i.name.includes('Long-'))?.name});cmd('pane:info');}
  if(test.zoom === 'actual') cmd('zoomActual');
  if(test.zoom === 'in') {cmd('zoomIn');cmd('zoomIn');}
  if(test.zoom === 'out') {cmd('zoomIn');cmd('zoomIn');cmd('zoomOut');}
  return a;
}
async function verifyState(test) {
  if(cfg.fixture==='photos' && (state.render?.state!=='ready' || state.render.name!==state.current)) throw Error('Expected a ready matching photo render');
  if(cfg.fixture==='missing' && state.render?.state!=='error') throw Error('Missing original did not show a render error');
  const expected = test.pane ? `${test.pane}Pane` : null;
  if(expected && (state.pane !== expected || !await page.locator(`#${expected}`).isVisible())) throw Error(`Pane did not open: ${expected}`);
  for(const [view,sel] of Object.entries({help:'#helpDialog.on',settings:'#settingsDialog.on',survey:'#survey:not([hidden])','first-run':'#firstRunDialog.on'}))
    if(test.view===view && !await page.locator(sel).isVisible()) throw Error(`View did not open: ${view} (${sel})`);
  if(test.view==='grid' && state.viewMode!=='square') throw Error('Grid did not open');
  if(test.view==='compare' && !state.compare?.active) throw Error('Compare did not activate');
  if(test.view==='no-results' && state.visibleCount!==0) throw Error('Filter did not produce an empty result');
  if(test.zoom==='actual' && state.zoomMode!=='100') throw Error('Actual zoom did not activate');
}
const runProbes = async test => (await evidencePage.evaluate(probe,{state,direction:test.direction}))
  .filter(hit=>!cfg.rules || cfg.rules.includes(hit.rule));
async function capture(test,id) {
  const screenshot=`${id}.png`, masked=`${id}-masked.png`;
  await evidencePage.screenshot({path:path.join(cfg.output,screenshot),animations:'disabled'});
  await evidencePage.screenshot({path:path.join(cfg.output,masked),animations:'disabled',
    mask:[evidencePage.locator('#cv,#glBefore,.thumb img,.survey-cell img,#loupeImg')],maskColor:'#555555'});
  return {screenshot,masked};
}
// CDP keeps actual timestamps and original frames; sampling is not a 60-fps guarantee.
async function recordFrames(id) {
  const session=await context.newCDPSession(page), frames=[];
  const dir=path.join(cfg.output,`${id}-frames`);fs.mkdirSync(dir);
  session.on('Page.screencastFrame', e=>{
    if(frames.length<cfg.maxFrames) {
      const file=`${id}-frames/${String(frames.length).padStart(5,'0')}.jpg`;
      fs.writeFileSync(path.join(cfg.output,file),Buffer.from(e.data,'base64'));
      frames.push({file,timestamp:e.metadata.timestamp,receivedEpochMs:Date.now()});
    }
    session.send('Page.screencastFrameAck',{sessionId:e.sessionId}).catch(()=>{});
  });
  await session.send('Page.startScreencast',{format:'jpeg',quality:85,maxWidth:1280,maxHeight:900,everyNthFrame:1});
  return async()=>{
    await session.send('Page.stopScreencast');await session.detach();
    fs.writeFileSync(path.join(cfg.output,`${id}-frames.json`),JSON.stringify({frames,capped:frames.length>=cfg.maxFrames},null,2));
  };
}
function random(seed) {let x=seed>>>0;return()=>{x+=0x6D2B79F5;let t=Math.imul(x^x>>>15,1|x);t^=t+Math.imul(t^t>>>7,61|t);return((t^t>>>14)>>>0)/4294967296;};}
const signature = hit=>`${hit.rule}:${hit.selector}:${hit.detail?.other||''}`;
async function minimize(test,actions,target) {
  // Greedy deletion with fresh browser + reset photo state per attempt. Only claim 1-minimal if exhausted.
  let best=actions.slice(), complete=true;
  for(let i=0;i<best.length;) {
    if(attempt++>=cfg.minimizeAttempts || Date.now()+10000>deadline) {complete=false;break;}
    const trial=best.filter((_,j)=>j!==i);
    try {
      await reset(test); for(const a of trial) await action(a); await settle();
      if((await runProbes(test)).some(h=>signature(h)===target)) {best=trial;i=0;} else i++;
    } catch {i++;}
  }
  return {actions:best,oneMinimal:complete,attempts:attempt};
}
try {
  for(const test of cfg.cases) {
    const record={...test,startedEpochMs:Date.now(),status:'passed',candidates:[]};
    let stopFrames;
    try {
      await reset(test); sequence=[]; attempt=0;
      if(cfg.mode==='explore') {
        const initial=await runProbes(test), known=new Set(initial.map(signature));
        record.initialCandidates=initial;
        record.initialEvidence=await capture(test,`${test.id}-initial`);
        stopFrames=await recordFrames(test.id);
        const rng=random(cfg.seed);
        const vocabulary=[...cfg.panes.map(p=>({command:`pane:${p}`})),
          ...['zoomFit','zoomActual','zoomIn','zoomOut','nextPhoto','previousPhoto','compare','toggleLibrary','toggleFilmstrip'].map(command=>({command})),
          {type:'pointer',dx:110,dy:60}, {type:'burst',actions:['zoomIn','zoomOut','nextPhoto','zoomFit'].map(command=>({command}))}];
        const actions=cfg.replay?.actions || Array.from({length:cfg.steps},()=>vocabulary[Math.floor(rng()*vocabulary.length)]);
        for(const a of actions) {
          sequence.push(a); await action(a); await settle();
          const hits=await runProbes(test);
          const fresh=hits.filter(h=>cfg.replay ? signature(h)===cfg.replay.target : !known.has(signature(h)));
          if(fresh.length) {
            record.candidates=fresh;record.state=state;
            Object.assign(record,await capture(test,test.id));
            await stopFrames();stopFrames=null;
            const original=sequence.slice(),target=signature(fresh[0]);
            // Persist the original reproducer before attempting reduction.
            const replay={schema:1,test,seed:cfg.seed,rules:cfg.rules,target,actions:original};
            fs.writeFileSync(path.join(cfg.output,`${test.id}-replay.json`),JSON.stringify(replay,null,2));
            const reduced=await minimize(test,original,target);
            fs.writeFileSync(path.join(cfg.output,`${test.id}-minimal.json`),JSON.stringify({...replay,...reduced},null,2));
            record.replay=`${test.id}-replay.json`;record.minimal=`${test.id}-minimal.json`;break;
          }
        }
        record.steps=sequence;
        if(cfg.replay && !record.candidates.some(h=>signature(h)===cfg.replay.target)) {
          const hits=await runProbes(test); record.candidates=hits.filter(h=>signature(h)===cfg.replay.target);
          if(!record.candidates.length) throw Error(`Replay target did not reproduce: ${cfg.replay.target}`);
        }
      } else {
        for(const a of caseActions(test)) {await action(a);sequence.push(a);await settle();}
        await verifyState(test);record.state=state;
        if(cfg.mode!=='snapshot') record.candidates=await runProbes(test);
      }
      if(!record.screenshot) Object.assign(record,await capture(test,test.id));
    } catch(e) {
      record.status='failed';record.error=String(e.stack||e);
      if(page && !page.isClosed()) try {Object.assign(record,await capture(test,test.id));} catch {}
    } finally {
      if(stopFrames) await stopFrames();
      record.actions=sequence;record.endedEpochMs=Date.now();records.push(record);save();
      process.stdout.write(`${test.id}: ${record.status}, ${record.candidates.length} candidates\n`);
    }
    checkTime();
  }
} catch(e) {errors.push({message:String(e.stack||e)});} finally {
  if(context) await context.close();await browser.close();save();
}
if(errors.length || records.some(r=>r.status==='failed') || records.length!==cfg.cases.length) process.exitCode=2;
