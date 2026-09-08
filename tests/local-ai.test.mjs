import assert from 'node:assert/strict';
import test from 'node:test';
import { aiSkippedSummary } from '../web/local-ai.js';
import { useCatalog } from '../web/i18n.js';

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

test('skipped summary uses locale plurals for both the photo count and each reason', () => {
  useCatalog('ar', { messages: {}, plurals: {
    '{count} photo skipped{reasons}. Download or restore the originals, then rebuild the index.': {
      two: 'صورتان تم تخطيهما{reasons}. استعد الأصول ثم أعد بناء الفهرس.',
    },
    '{count} empty file': { two: 'ملفان فارغان' },
  } });
  try {
    assert.equal(aiSkippedSummary({ skipped: 2, skippedReasons: { empty: 2 } }),
      'صورتان تم تخطيهما (ملفان فارغان). استعد الأصول ثم أعد بناء الفهرس.');
  } finally {
    useCatalog('en', { messages: {}, plurals: {} });
  }
});
