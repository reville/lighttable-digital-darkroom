// SPDX-License-Identifier: GPL-3.0-only
/* Ask once, at startup, whether crash reports may be sent. The server decides
   whether this build can send them at all. Closing the dialog without a choice
   asks again at the next launch; Settings ▸ General changes a choice later. */
import { t as tr } from './i18n.js';

export function shouldAskCrashReports(status) {
  return status?.available === true && typeof status.consent !== 'boolean';
}

export function installCrashReportConsent({ el, getJSON, post, isBusy = () => false,
  onChoice = () => {}, delay = 1500, retry = 1000 }) {
  const dialog = el('crashReportDialog');
  const buttons = [el('crashReportAllow'), el('crashReportDecline')];
  let status = null, asked = false, started = false, timer = null;
  const isOpen = () => dialog.classList.contains('on');

  function show(visible) {
    dialog.classList.toggle('on', visible);
    dialog.setAttribute('aria-hidden', String(!visible));
  }

  function maybeShow() {
    clearTimeout(timer);
    if (asked || !shouldAskCrashReports(status)) return;
    // Wait behind first-run setup and any other open dialog.
    if (isBusy()) { timer = setTimeout(maybeShow, retry); return; }
    asked = true;
    el('crashReportPending').hidden = !(status.pending > 0);
    el('crashReportError').textContent = '';
    show(true);
  }

  async function choose(value) {
    for (const button of buttons) button.disabled = true;
    try {
      const result = await post('/api/prefs', { crashReports: value });
      if (!result || result.error) throw new Error(result?.error || tr('Could not save changes. Please try again.'));
      status = { ...status, consent: value };
      show(false);
      onChoice(value);
    } catch (error) {
      el('crashReportError').textContent = error.message;
    } finally {
      for (const button of buttons) button.disabled = false;
    }
  }

  buttons[0].addEventListener('click', () => choose(true));
  buttons[1].addEventListener('click', () => choose(false));
  // Capture before editor shortcuts. Tab stays with the shared dialog focus trap.
  document.addEventListener('keydown', (event) => {
    if (!isOpen() || event.key === 'Tab') return;
    event.stopImmediatePropagation();
    if (event.key === 'Escape') { event.preventDefault(); show(false); }
  }, true);

  async function refresh() {
    try { status = await getJSON('/api/crash-reports'); }
    catch (_) { status = null; }
    return status;
  }

  return {
    async start() {
      if (started) return;
      started = true;
      await refresh();
      clearTimeout(timer);
      timer = setTimeout(maybeShow, delay);
    },
    refresh,
    status: () => status,
    isOpen,
  };
}
