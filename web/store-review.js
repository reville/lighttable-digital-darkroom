// SPDX-License-Identifier: GPL-3.0-only
import { api } from './api.js';
import { sendNative, nativeBridge } from './native-bridge.js';

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export function afterSuccessfulExport(previous, now) {
  return {
    firstExportAt: Number.isFinite(previous?.firstExportAt) && previous.firstExportAt > 0
      ? previous.firstExportAt : now,
    completedExports: Math.min(3, Math.max(0, Number(previous?.completedExports) || 0) + 1),
    decision: previous?.decision === 'dismissed' || previous?.decision === 'reviewed'
      ? previous.decision : null,
  };
}

export function shouldOfferReview(state, now) {
  return !state?.decision && state?.completedExports >= 3
    && Number.isFinite(state.firstExportAt) && now - state.firstExportAt >= WEEK_MS;
}

export function installStoreReview() {
  if (window.__LIGHTTABLE_PLATFORM__ !== 'windows' || !nativeBridge()) return;

  const helpButton = document.getElementById('helpReviewStore');
  const notice = document.getElementById('storeReviewNotice');
  const updateNotice = document.getElementById('desktopUpdateNotice');
  helpButton.hidden = false;
  let state = null;
  let offeredThisSession = false;

  const loaded = fetch('/api/prefs').then(async response => {
    if (!response.ok) throw new Error('Could not load preferences');
    const prefs = await response.json();
    if (!prefs || typeof prefs !== 'object' || prefs.error) throw new Error('Invalid preferences');
    state = prefs.storeReviewPrompt || {};
  }).catch(() => { /* Keep the manual Help action, but disable automatic prompts. */ });

  async function recordDecision(decision) {
    notice.hidden = true;
    offeredThisSession = true;
    if (state === null) return;
    state = { ...state, decision };
    try { await api('/api/prefs', { storeReviewPrompt: state }); } catch { /* Best effort. */ }
  }

  function openReview() {
    document.getElementById('helpClose')?.click();
    sendNative('reviewInStore');
  }

  helpButton.addEventListener('click', openReview);
  document.getElementById('storeReviewOpen').addEventListener('click', () => {
    recordDecision('reviewed');
    openReview();
  });
  document.getElementById('storeReviewDismiss').addEventListener('click', () => {
    recordDecision('dismissed');
  });

  window.addEventListener('lighttable:export-complete', async () => {
    await loaded;
    if (state === null || state.decision) return;
    const now = Date.now();
    const next = afterSuccessfulExport(state, now);
    try {
      const result = await api('/api/prefs', { storeReviewPrompt: next });
      if (result?.ok !== true) return;
      state = next;
    } catch { return; }
    if (offeredThisSession || !shouldOfferReview(state, now) || !updateNotice.hidden
      || document.querySelector('.modal-backdrop.on')) return;
    notice.hidden = false;
    offeredThisSession = true;
  });
}

if (typeof window !== 'undefined') installStoreReview();
