// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
import {gradeBakeRequest, gradeBakeKey} from '../web/preview-processing.js';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

for (const native of [false, true]) for (const baked of [false, true]) test(`Before selects original pixels and restores edits (${native ? 'Metal' : 'WebGL'}, baked=${baked})`, () => {
  const noop = () => {}, calls = [];
  const S = {holdBefore:false, compareActive:false, comparePosition:0.4, params:{profile_enabled:true},
    renderState:'ready', grade:{exposure:0.5}, masks:[{id:'mask', grade:{texture:baked ? 0.3 : 0}}], softProof:{active:true},
    gradeEditsBaked:baked,
    maskTextureDirty:false, gl:{draw:(grade,masks) => calls.push({grade,masks}),
      drawCompare:position => {calls.push({position}); return true;}}};
  S.presentedGradeKey = gradeBakeKey(gradeBakeRequest(S.grade, S.masks));
  const node = {classList:{toggle:noop}, removeAttribute:noop, style:{setProperty:noop}};
  const context = {S, $:() => node, performance, GRADE_DEFAULTS:{exposure:0},
    MAX_MASKS:16, LOCAL_GRADE_DEFAULTS:{exposure:0},
    gradeBakeRequest, gradeBakeKey,
    renderPhysicalPreview:() => assert.fail('Holding Before must not rebuild the edited surface'),
    scheduleViewportRegionRender:noop, syncPreviewBackend:noop, packedMaskData:{},
    syncBrowserOriginal:() => calls.push({originalRequested:true}),
    nativePreviewActive:() => native, scheduleHistogram:noop, GRADE_PERF:{take:() => null},
    nativeGradePayload:grade => ({grade}), postNative:(action,detail) => calls.push({action,...detail}),
    spotVisualization:() => ({enabled:true, threshold:0.5}),
    previewSourceX:position => position, syncCompareView:noop, syncCompareControl:noop,
    setCompareActive:on => {S.compareActive=on;}};
  const code = between('function drawGradeNow(', 'function drawGrade()') +
    between('function nativeMaskPayload(', 'function nativeMaskChannelPayload(') +
    between('function renderedComparePosition()', 'function compareEditingBlocked()') +
    between('function renderCompare()', 'function setCompareActive(') +
    between('function setBefore(', "$('beforeBtn').addEventListener") +
    '\nfunction drawGrade(){drawGradeNow();} globalThis.before=setBefore;';
  vm.runInNewContext(code, context);
  context.before(true);
  assert.equal(calls.filter(c => c.originalRequested).length, 1);
  assert.equal(calls.findLast(c => 'position' in c)?.position, 1, 'Before must select the original texture');
  assert.equal(calls.findLast(c => c.grade)?.grade, baked ? context.GRADE_DEFAULTS : S.grade, 'Keep finishing adjustments ready for release without applying baked grade twice');
  if (!native) assert.deepEqual(Array.from(calls.findLast(c => c.masks)?.masks), baked ? [] : S.masks, 'Keep the edited mask uniforms while holding Before');
  context.before(false);
  assert.equal(calls.findLast(c => 'position' in c)?.position, 0);
  assert.equal(S.params.profile_enabled, true, 'Before must not change saved Film settings');
});
