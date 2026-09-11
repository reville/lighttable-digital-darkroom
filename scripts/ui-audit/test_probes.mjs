// SPDX-License-Identifier: GPL-3.0-only
// Synthetic DOM tests validate the oracles only; never presented as LightTable visual proof.
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {probe} from './probes.mjs';
const {chromium}=await import(pathToFileURL(process.env.LIGHTTABLE_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true,executablePath:process.env.LIGHTTABLE_BROWSER_EXECUTABLE||undefined});
try {
  const page=await browser.newPage({viewport:{width:500,height:400}});
  await page.setContent(`<!doctype html><html dir="ltr"><style>body{margin:0;background:white;color:black}button{font:16px Arial}</style>
    <button id="clean">Readable</button><div hidden><button id="hidden">Invisible</button></div>
    <details><summary>Closed</summary><button id="closed">Invisible</button></details></html>`);
  let hits=await page.evaluate(probe,{});
  assert.equal(hits.length,0,JSON.stringify(hits));
  await page.setContent(`<!doctype html><html dir="ltr"><style>body{margin:0;background:white;color:black}button{font:16px Arial}
    #badHidden[hidden]{display:block!important}#clip{width:30px;overflow:hidden;white-space:nowrap}
    #a,#b{position:absolute;left:100px;top:100px;width:80px;height:40px}
    #low{color:rgb(220,220,220)}#wide{width:700px;height:10px}#zero{width:0;height:0;padding:0;border:0}</style>
    <button id="badHidden" hidden>Painted hidden</button><button id="clip">A long label here</button>
    <button id="a">Overlap A</button><button id="b">Overlap B</button><span id="low">Low contrast</span>
    <button id="tab" tabindex="2">Tab order</button><div id="wide"></div><button id="zero"></button></html>`);
  hits=await page.evaluate(probe,{direction:'rtl'});
  for(const rule of ['horizontal-overflow','hidden-paints','overlapping-controls','control-text-overflow','text-contrast','positive-tab-order','zero-size-control','document-direction'])
    assert(hits.some(h=>h.rule===rule),`Missing ${rule}: ${JSON.stringify(hits)}`);
  await page.setContent('<!doctype html><html dir="ltr"><div id="zoomwrap" style="width:200px;height:200px"><canvas id="cv" style="width:300px;height:300px"></canvas></div></html>');
  hits=await page.evaluate(probe,{state:{zoomMode:'fit',viewMode:'detail',render:{state:'ready'}}});
  assert(hits.some(h=>h.rule==='fit-geometry'));
  console.log('Probe self-checks passed (clean DOM, eight defects, Fit geometry).');
} finally {await browser.close();}
