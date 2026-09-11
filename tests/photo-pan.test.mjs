// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createPhotoPanMemory} from '../web/photo-pan.js';

const frame = {width:2000, height:1000};
const view = () => ({viewMode:'detail', zoomMode:'100', zoom:2, panX:-500, panY:200});

test('each photo returns to its own area at the current magnification', () => {
  const memory = createPhotoPanMemory(), state = view();
  memory.remember('folder/a', state, frame);
  memory.restore('folder/b', state, frame);
  assert.deepEqual([state.panX, state.panY], [0, 0]);
  state.panX = 400; state.panY = -100;
  memory.remember('folder/b', state, frame);
  state.zoomMode = 'custom'; state.zoom = 4;
  memory.restore('folder/a', state, {width:4000,height:2000});
  assert.deepEqual([state.panX, state.panY], [-1000, 400]);
  assert.equal(state.zoom, 4);
  assert.equal(state.zoomMode, 'custom');
  memory.restore('folder/b', state, frame);
  assert.deepEqual([state.panX, state.panY], [400, -100]);
  memory.restore('other-folder/a', state, frame);
  assert.deepEqual([state.panX, state.panY], [0, 0]);
});

test('Fit, hidden grids, and crop framing do not erase inspection positions', () => {
  const memory = createPhotoPanMemory(), state = view();
  memory.remember('a', state, frame);
  for (const override of [{zoomMode:'fit'}, {zoomMode:'crop'}, {viewMode:'photo'}, {cropping:true},
    {cropTransition:'exit'}]) {
    memory.remember('a', {...state,...override,panX:0,panY:0}, frame);
  }
  state.zoomMode = 'fit';
  memory.restore('a', state, frame);
  assert.deepEqual([state.panX,state.panY], [0,0]);
  state.zoomMode = '100';
  memory.restore('a', state, frame);
  assert.deepEqual([state.panX,state.panY], [-500,200]);
  state.cropping = true; state.panX = 50;
  memory.restore('a', state, frame);
  assert.equal(state.panX, 50);
});

test('unavailable geometry and invalid positions never poison memory', () => {
  const memory = createPhotoPanMemory(), state = view();
  memory.remember('a', state, frame);
  memory.remember('a', {...state,panX:NaN}, frame);
  memory.remember('a', state, {width:0,height:0});
  memory.restore('a', state, frame);
  assert.deepEqual([state.panX,state.panY], [-500,200]);
});

test('navigation still loading another photo cannot overwrite the outgoing position', () => {
  const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const remember = source.match(/function rememberPhotoPan\(\).*?\n\}/s)[0];
  const state = {...view(), editingName:'a'}, memory = createPhotoPanMemory();
  let current = 'a';
  const capture = new Function('S','cur','$','photoPanMemory','photoPanKey',
    `${remember}; return rememberPhotoPan;`)(state,()=>({name:current}),
      ()=>({getBoundingClientRect:()=>frame}),memory,'a');
  capture();
  current = 'b'; state.panX = 0; state.panY = 0;
  capture();
  memory.restore('a', state, frame);
  assert.deepEqual([state.panX,state.panY], [-500,200]);
});
