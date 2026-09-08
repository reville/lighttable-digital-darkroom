import assert from 'node:assert/strict';
import test from 'node:test';
import { aiSkippedSummary } from '../web/local-ai.js';

test('skipped status distinguishes unavailable inputs and gives a recovery step', () => {
  const detail = aiSkippedSummary({ skipped: 3,
    skippedReasons: { empty: 1, 'cloud-only': 1, unavailable: 1 } });
  assert.match(detail, /3 photos skipped/);
  assert.match(detail, /1 empty file/);
  assert.match(detail, /1 file not downloaded/);
  assert.match(detail, /1 unavailable file/);
  assert.match(detail, /Download or restore the originals, then rebuild the index/);
  assert.equal(aiSkippedSummary({ skipped: 0 }), '');
});
