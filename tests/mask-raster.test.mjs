// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {autoMaskValues, strokeCoverage, accumulateStroke} from '../web/mask-raster.js';
import {radialHandles, editRadial} from '../web/mask-shape.js';
import {normalizeMasks} from '../web/editor-panels.js';
import {gradeBakeRequest} from '../web/preview-processing.js';

test('Auto Mask holds the sampled color and excludes a contrasting edge', () => {
  const pixels = new Uint8Array([210,30,20,255, 200,32,22,255, 15,50,220,255]);
  const mask = autoMaskValues(pixels,3,1,[0,0],.18);
  assert.equal(mask[0],255); assert.equal(mask[1],255); assert.equal(mask[2],0);
});
test('Flow accumulates with a separate Density ceiling', () => {
  const s = {size:.3,feather:0,flow:.2,density:.45,points:[[.5,.5]]};
  const values = new Float32Array(101*101), coverage = strokeCoverage(s,101,101);
  for(let i=0;i<4;i++) accumulateStroke(values,coverage,s);
  assert.ok(Math.abs(values[50*101+50]-.45)<1e-6);
});
test('Ellipse handles resize, rotate and move without losing aspect', () => {
  const mask = normalizeMasks([{type:'radial',radius:.2,radiusX:.4,radiusY:.1}])[0];
  const handles = radialHandles(mask,200,100);
  assert.deepEqual(handles.x,[.7,.5]);
  editRadial(mask,{handle:'rotate'},[.8,.5],{width:200,height:100});
  assert.equal(mask.angle,90);
  editRadial(mask,{handle:'center',center:[.5,.5],start:[.5,.5]},[.6,.6],{width:200,height:100});
  assert.ok(mask.center.every(x => Math.abs(x-.6)<1e-12)); assert.equal(mask.radiusX,.4); assert.equal(mask.radiusY,.1);
});
test('New local controls request the same ordered server preview as export', () => {
  for(const key of ['whites','blacks','curveL','curveR','curveG','curveB']) {
    const value = key.startsWith('curve') ? Array.from({length:256},(_,i)=>(i/255)**.7) : .3;
    const masks = [{enabled:true,opacity:1,grade:{[key]:value}}];
    assert.equal(gradeBakeRequest({},masks).masks,masks);
    assert.deepEqual(gradeBakeRequest({},masks,true),{});
    assert.deepEqual(gradeBakeRequest({},[{...masks[0],enabled:false}]),{});
  }
});
