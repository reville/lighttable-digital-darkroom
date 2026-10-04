// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {GradeRenderer} from '../web/gl.js';

test('the native presentation policy is applied when the WebGL context is created', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis,
    '__LIGHTTABLE_PRESERVE_DRAWING_BUFFER__');
  try {
    for (const policy of [undefined, false, true, 'true', 1]) {
      if (policy === undefined) delete globalThis.__LIGHTTABLE_PRESERVE_DRAWING_BUFFER__;
      else globalThis.__LIGHTTABLE_PRESERVE_DRAWING_BUFFER__ = policy;
      let requested;
      const canvas = {getContext: (kind, options) => {
        requested = {kind, options};
        return null; // Stop before shader setup; this test owns context creation.
      }};
      assert.throws(() => new GradeRenderer(canvas), /WebGL unavailable/);
      assert.deepEqual(requested, {kind: 'webgl', options: {
        preserveDrawingBuffer: policy === true, antialias: false, alpha: false,
      }});
    }
  } finally {
    if (previous) Object.defineProperty(globalThis, '__LIGHTTABLE_PRESERVE_DRAWING_BUFFER__', previous);
    else delete globalThis.__LIGHTTABLE_PRESERVE_DRAWING_BUFFER__;
  }
});
