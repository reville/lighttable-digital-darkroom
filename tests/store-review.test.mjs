// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { afterSuccessfulExport, shouldOfferReview, installStoreReview } from '../web/store-review.js';

const DAY = 24 * 60 * 60 * 1000;

test('review offer waits for three successful exports and seven days', () => {
  let state = {};
  state = afterSuccessfulExport(state, DAY);
  state = afterSuccessfulExport(state, DAY + 7 * DAY);
  assert.equal(shouldOfferReview(state, DAY + 7 * DAY), false);
  state = afterSuccessfulExport(state, DAY + 7 * DAY);
  assert.equal(shouldOfferReview(state, DAY + 7 * DAY), true);
  assert.equal(shouldOfferReview({ ...state, decision: 'dismissed' }, DAY + 30 * DAY), false);
  assert.equal(shouldOfferReview({ ...state, decision: 'reviewed' }, DAY + 30 * DAY), false);
  assert.equal(afterSuccessfulExport(state, DAY + 8 * DAY).completedExports, 3);
});

test('manual review action is Windows-only and remains available after dismissal', async () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  const priorFetch = globalThis.fetch;
  try {
    const nodes = new Map();
    const node = id => {
      if (!nodes.has(id)) nodes.set(id, {
        hidden: true, listeners: {}, clicked: false,
        addEventListener(event, fn) { this.listeners[event] = fn; },
        click() { this.clicked = true; this.listeners.click?.(); },
      });
      return nodes.get(id);
    };
    const native = [];
    globalThis.document = { getElementById: node, querySelector: () => null };
    globalThis.window = {
      __LIGHTTABLE_PLATFORM__: 'linux',
      lightTableNativeBridge: { postMessage: message => native.push(message) },
      addEventListener() {},
    };
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ storeReviewPrompt: { decision: 'dismissed' } }) });
    installStoreReview();
    assert.equal(node('helpReviewStore').hidden, true);
    globalThis.window.__LIGHTTABLE_PLATFORM__ = 'windows';
    installStoreReview();
    await Promise.resolve();
    assert.equal(node('helpReviewStore').hidden, false);
    node('helpReviewStore').click();
    assert.equal(node('helpClose').clicked, true);
    assert.deepEqual(native, [{ action: 'reviewInStore' }]);
    assert.equal(node('storeReviewNotice').hidden, true);
  } finally {
    globalThis.window = priorWindow;
    globalThis.document = priorDocument;
    globalThis.fetch = priorFetch;
  }
});

test('automatic prompt persists progress and never appears over an update', async () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  const priorFetch = globalThis.fetch;
  try {
    const nodes = new Map();
    const listeners = {};
    const writes = [];
    const node = id => {
      if (!nodes.has(id)) nodes.set(id, { hidden: true, listeners: {},
        addEventListener(event, fn) { this.listeners[event] = fn; } });
      return nodes.get(id);
    };
    globalThis.document = { getElementById: node, querySelector: () => null };
    globalThis.window = {
      __LIGHTTABLE_PLATFORM__: 'windows',
      lightTableNativeBridge: { postMessage() {} },
      addEventListener: (event, fn) => { listeners[event] = fn; },
    };
    globalThis.fetch = async (_path, options) => {
      if (options?.body) { writes.push(JSON.parse(options.body)); return { json: async () => ({ ok: true }) }; }
      return { ok: true, json: async () => ({ storeReviewPrompt: {
        firstExportAt: Date.now() - 8 * DAY, completedExports: 2,
      } }) };
    };
    installStoreReview();
    await listeners['lighttable:export-complete']();
    assert.equal(writes[0].storeReviewPrompt.completedExports, 3);
    assert.equal(node('storeReviewNotice').hidden, false);
    node('storeReviewDismiss').listeners.click();
    assert.equal(node('storeReviewNotice').hidden, true);
    await Promise.resolve();
    assert.equal(writes[1].storeReviewPrompt.decision, 'dismissed');
    await listeners['lighttable:export-complete']();
    assert.equal(writes.length, 2);

    node('desktopUpdateNotice').hidden = false;
    globalThis.fetch = async (_path, options) => {
      if (options?.body) return { json: async () => ({ ok: true }) };
      return { ok: true, json: async () => ({ storeReviewPrompt: {
        firstExportAt: Date.now() - 8 * DAY, completedExports: 2,
      } }) };
    };
    node('storeReviewNotice').hidden = true;
    installStoreReview();
    await listeners['lighttable:export-complete']();
    assert.equal(node('storeReviewNotice').hidden, true);
  } finally {
    globalThis.window = priorWindow;
    globalThis.document = priorDocument;
    globalThis.fetch = priorFetch;
  }
});
