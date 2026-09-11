// SPDX-License-Identifier: GPL-3.0-only
import { api } from './api.js';
import { sendNative, nativeBridge } from './native-bridge.js';
import { t as tr } from './i18n.js';
import { showManualUpdateModal } from './manual-update-modal.js';

export function installDesktopUpdates({ prepare, cancel, onError }) {
  const platform = window.__LIGHTTABLE_PLATFORM__;
  const button = document.getElementById('checkForUpdates');
  const status = document.getElementById('desktopUpdateStatus');
  const checks = document.getElementById('automaticUpdateChecks');
  const notice = document.getElementById('desktopUpdateNotice');
  const noticeText = document.getElementById('desktopUpdateNoticeText');
  const noticeAction = document.getElementById('desktopUpdateNoticeAction');
  const guideButton = document.getElementById('manualUpdateGuide');
  let latest = {}, poll = null, pollDeadline = 0, deferredVersion = null;
  const controls = document.getElementById('desktopUpdateControls');
  if (controls) controls.hidden = platform === 'macos';

  const openModal = (opts = {}) => {
    const fn = (typeof showManualUpdateModal !== 'undefined' && showManualUpdateModal)
      || (typeof window !== 'undefined' && window.lightTableShowUpdateModal);
    if (typeof fn === 'function') {
      return fn({
        platform,
        owner: latest.owner,
        managedBy: latest.managedBy,
        version: latest.available_version,
        channel: latest.channel,
        ...opts,
      });
    }
    return null;
  };

  async function request(path, body = {}) {
    const result = await api(path, body);
    if (result.error) throw new Error(result.error);
    return result;
  }

  window.lightTablePrepareToUpdate = async () => {
    try {
      if (!await prepare()) return false;
      await request('/api/updates/prepare');
      return true;
    } catch (error) {
      await window.lightTableCancelUpdate();
      onError(error.message);
      return false;
    }
  };
  window.lightTableCancelUpdate = async () => {
    try { await request('/api/updates/cancel'); }
    catch (error) { onError(error.message); }
    finally { cancel(); }
  };
  window.lightTableShutdownForUpdate = async () => {
    try { await request('/api/updates/shutdown'); return true; }
    catch (error) {
      await window.lightTableCancelUpdate();
      onError(error.message);
      return false;
    }
  };

  function render(value, announce = false) {
    latest = value;
    const managed = value.managedBy || ({arch: 'Arch / Omarchy', flatpak: 'Flatpak', snap: 'Snap'}[value.owner] || null);
    checks.disabled = !!managed || value.supported === false;
    button.disabled = value.supported === false || ['checking', 'downloading', 'applying'].includes(value.state);
    button.textContent = value.state === 'ready' ? tr('Restart and update')
      : value.state === 'available' ? tr('Update') : tr('Check for Updates…');
    let message = value.message || (value.supported !== false ? value.error : null);
    status.title = value.error || '';
    if (!message) message = managed ? tr('Updates are managed by {manager}.', {manager: managed})
      : value.state === 'checking' ? tr('Checking for updates…')
      : value.state === 'downloading' ? tr('Downloading update…')
      : value.state === 'ready' ? tr('The update is ready. Restart when your work is finished.')
      : value.state === 'available' ? tr('LightTable {version} is available.', {version: value.available_version})
      : value.supported === false ? tr('Automatic updates are not configured for this build.')
      : value.last_checked ? tr('LightTable is up to date.') : tr('Check for updates when you are ready.');
    status.textContent = message;
    if (guideButton) {
      guideButton.hidden = !(managed || value.supported === false || platform === 'macos');
    }
    if (announce && ['available', 'ready'].includes(value.state)
        && deferredVersion !== value.available_version) {
      noticeText.textContent = message;
      noticeAction.textContent = button.textContent;
      notice.hidden = false;
    } else if (!['available', 'ready'].includes(value.state)) notice.hidden = true;
  }

  async function refresh(announce = false) {
    if (!nativeBridge()) {
      render({ supported: false, message: tr('Open the desktop app to check for updates.') });
      return;
    }
    if (platform !== 'linux') {
      if (platform === 'windows') sendNative('requestUpdateStatus');
      return;
    }
    const response = await fetch('/api/updates');
    if (!response.ok) throw new Error(tr('Could not check for updates.'));
    const value = await response.json();
    render(value, announce);
    if (['checking', 'downloading'].includes(value.state) && Date.now() < pollDeadline) {
      clearTimeout(poll);
      poll = setTimeout(() => refresh(announce).catch(error => render({ ...latest, state: 'error', error: error.message })), 1000);
    }
  }

  async function act() {
    if (!nativeBridge() || latest.supported === false) {
      if (latest.state === 'available') openModal();
      return;
    }
    if (platform !== 'linux') { sendNative('checkForUpdates'); return; }
    try {
      if (latest.state === 'ready') {
        if (!await window.lightTablePrepareToUpdate()) return;
        try {
          const result = await request('/api/updates/apply');
          if (!sendNative('finishUpdateShutdown', { helper_directory: result.helper_directory })) {
            await window.lightTableCancelUpdate();
          }
        } catch (error) { await window.lightTableCancelUpdate(); throw error; }
      } else {
        const action = latest.state === 'available' ? 'download' : 'check';
        render(await request('/api/updates/' + action));
        pollDeadline = Date.now() + 10 * 60 * 1000;
        await refresh(true);
      }
    } catch (error) {
      render({ ...latest, state: 'error', error: error.message });
      onError(error.message);
    }
  }
  button.addEventListener('click', act);
  noticeAction.addEventListener('click', act);
  if (guideButton) guideButton.addEventListener('click', () => openModal());
  document.getElementById('desktopUpdateLater').addEventListener('click', () => {
    deferredVersion = latest.available_version;
    notice.hidden = true;
  });
  window.lightTableRefreshUpdates = () => refresh().catch(error => {
    status.textContent = error.message;
  });

  if (!nativeBridge()) render({ supported: false, message: tr('Open the desktop app to check for updates.') });
  else if (platform === 'linux') {
    void window.lightTableRefreshUpdates();
    // Delay the first check until the user has reached the actual editor.
    // The server persists the once-per-day limit and honors the preference.
    setTimeout(async () => {
      try {
        await request('/api/updates/check', { automatic: true });
        pollDeadline = Date.now() + 120000;
        await refresh(true);
      } catch (_) { /* Automatic network failures must not interrupt editing. */ }
    }, 20000);
  } else if (platform === 'windows') sendNative('requestUpdateStatus');

  return {
    nativeEvent(message) {
      if (message?.type === 'updateStatus') render(message);
      if (message?.type === 'showUpdateModal') openModal(message.options || message);
    },
  };
}
