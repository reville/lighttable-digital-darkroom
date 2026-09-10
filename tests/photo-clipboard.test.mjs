import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import {createPhotoClipboard, installPhotoCopyContextMenu} from '../web/photo-clipboard.js';

const payload = {name:'a.jpg',state:{grade:{exposure:1},crop:{x:0,y:0,w:.5,h:.5}}};
function harness(t, {native=false, failed=false}={}) {
  const messages=[], requests=[], writes=[];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({url,body:JSON.parse(options.body)});
    return new Response(failed ? '{}' : 'PNG bytes', {status:failed?500:200,
      headers:{'Content-Type':failed?'application/json':'image/png'}});
  });
  const bridge={postMessage(message){writes.push(message);}};
  const app=createPhotoClipboard({getPayload:()=>structuredClone(payload),
    nativeBridge:()=>native?bridge:null,notify:message=>messages.push(message)});
  return {app,messages,requests,writes};
}
test('native copy sends edited PNG and waits for matching confirmation',async t=>{
  const h=harness(t,{native:true}); const copying=h.app.copy();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.requests[0].body.w,2048);
  assert.deepEqual(h.requests[0].body.state,payload.state);
  assert.equal(atob(h.writes[0].png),'PNG bytes');
  assert.deepEqual(h.messages,[]);
  h.app.complete({id:'stale',ok:true}); assert.deepEqual(h.messages,[]);
  await h.app.copy(); assert.equal(h.requests.length,1);
  h.app.complete({id:h.writes[0].id,ok:true});await copying;
  assert.deepEqual(h.messages,['Photo copied to clipboard']);
});
test('render failure leaves clipboard alone and allows retry',async t=>{
  const h=harness(t,{native:true,failed:true});await h.app.copy();await h.app.copy();
  assert.equal(h.writes.length,0);assert.equal(h.requests.length,2);
  assert.deepEqual(h.messages,Array(2).fill('Could not copy photo to clipboard'));
});
test('native clipboard rejection does not announce success',async t=>{
  const h=harness(t,{native:true});const copying=h.app.copy();
  await new Promise(resolve=>setImmediate(resolve));
  h.app.complete({id:h.writes[0].id,ok:false});await copying;
  assert.deepEqual(h.messages,['Could not copy photo to clipboard']);
});
test('browser starts clipboard write synchronously with a promised PNG',async t=>{
  const h=harness(t);let item,called=false;
  const old=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  globalThis.ClipboardItem=class {constructor(data){this.data=data;}};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{write(items){called=true;[item]=items;return item.data['image/png'];}}}});
  t.after(()=>{delete globalThis.ClipboardItem;if(old)Object.defineProperty(globalThis,'navigator',old);else delete globalThis.navigator;});
  const copying=h.app.copy();assert.equal(called,true);
  assert.ok(item.data['image/png'] instanceof Promise);await copying;
  assert.deepEqual(h.messages,['Photo copied to clipboard']);
});
test('missing photo never renders or writes',async()=>{
  let calls=0;const app=createPhotoClipboard({getPayload:()=>null,nativeBridge:()=>{calls++;},notify:()=>{calls++;}});
  await app.copy();assert.equal(calls,0);
});
test('Command/Ctrl-C copies images; Shift-C and text editing keep their behavior',()=>{
  const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const start=source.lastIndexOf("document.addEventListener('keydown', (e) => {");
  const code=source.slice(start,source.indexOf("  if (meta && e.shiftKey && e.key.toLowerCase() === 'v')",start))+'});';
  let handler, copies=0, settings=0;
  const context={document:{querySelector:()=>null,addEventListener:(_,fn)=>handler=fn},PHOTO_TOOL_PANES:[],S:{editingName:'a.jpg'},cur:()=>({name:'a.jpg'}),PHOTO_CLIPBOARD:{copy:()=>{copies++;}},$:()=>({click:()=>{settings++;}})};
  vm.runInNewContext(code,context);
  const event=(extra={})=>({key:'c',metaKey:true,preventDefault(){this.defaultPrevented=true;},target:{tagName:'BODY'},...extra});
  handler(event());handler(event({metaKey:false,ctrlKey:true}));assert.equal(copies,2);
  handler(event({shiftKey:true}));assert.equal(settings,1);
  for(const target of [{tagName:'INPUT'},{tagName:'TEXTAREA'},{tagName:'DIV',isContentEditable:true}])handler(event({target}));
  handler(event({altKey:true}));context.S.editingName='loading.jpg';handler(event());
  assert.equal(copies,2);
});


test('preview context menu opens at the pointer and leaves ordinary controls alone',()=>{
  const handlers={},requests=[];let ready=true,blurred=0;
  const preview={addEventListener:(name,handler)=>handlers[name]=handler,
    ownerDocument:{activeElement:{blur:()=>blurred++}}};
  installPhotoCopyContextMenu(preview,{available:()=>ready,openMenu:point=>requests.push(point)});
  const event=(extra={})=>({clientX:240,clientY:170,target:{closest:()=>null},
    preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},
    stopImmediatePropagation(){this.stopped=true;},...extra});
  const right=event({button:2});handlers.pointerdown(right);assert.equal(right.stopped,true);
  const control=event({button:0,ctrlKey:true});handlers.pointerdown(control);assert.equal(control.stopped,true);
  const left=event({button:0});handlers.pointerdown(left);assert.equal(left.stopped,undefined);
  const menu=event();handlers.contextmenu(menu);
  assert.equal(menu.prevented,true);assert.equal(blurred,1);assert.deepEqual(requests,[{x:240,y:170}]);
  const button=event({target:{closest:()=>({tagName:'BUTTON'})}});handlers.contextmenu(button);
  assert.equal(button.prevented,undefined);assert.equal(blurred,1);
  ready=false;const unavailable=event();handlers.contextmenu(unavailable);
  assert.equal(unavailable.prevented,undefined);assert.equal(requests.length,1);
});
