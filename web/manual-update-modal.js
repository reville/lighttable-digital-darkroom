// SPDX-License-Identifier: GPL-3.0-only
import { sendNative } from './native-bridge.js';

const tr = (source, values = {}) => {
  if (typeof window !== 'undefined' && typeof window.lightTableTranslate === 'function') {
    return window.lightTableTranslate(source, values);
  }
  return String(source).replace(/\{(\w+)\}/g, (token, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : token);
};

const escapeHTML = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

// The dialog element is reused across shows; bind its own listeners once so
// repeated opens cannot stack duplicate Escape/backdrop handlers.
const boundDialogs = new WeakSet();

export function getPlatformUpdateInstructions(options = {}) {
  const platform = options.platform || (typeof window !== 'undefined' ? window.__LIGHTTABLE_PLATFORM__ : null) || 'macos';
  const owner = String(options.owner || options.managedBy || '');
  const personal = Boolean(options.personal);
  const version = options.version || options.available_version || '';

  let explanation = options.explanation || '';
  let command = options.command || '';
  let websiteUrl = options.url || '';
  let websiteLabel = tr('Go to Website');

  if (platform === 'macos') {
    if (personal) {
      explanation = explanation || tr('You are running a local development build. Update by pulling the latest changes and running the update script.');
      command = command || 'git pull && ./scripts/update-personal-app.sh';
      websiteUrl = websiteUrl || 'https://github.com/reville/lighttable-digital-darkroom';
      websiteLabel = tr('View Repository');
    } else if (owner.toLowerCase() === 'homebrew' || options.channel === 'beta') {
      explanation = explanation || tr('Automatic in-app updates on macOS are manual during beta while Developer ID notarization is pending. Update via Homebrew or download the disk image.');
      command = command || 'brew update && brew upgrade --cask reville/lighttable/lighttable@beta';
      websiteUrl = websiteUrl || 'https://lighttable.app/';
      websiteLabel = tr('Download DMG from Website');
    } else {
      explanation = explanation || tr('Automatic in-app updates on macOS are manual while Developer ID notarization is pending. Download the latest release from the website or upgrade via Homebrew.');
      command = command || 'brew update && brew upgrade --cask reville/lighttable/lighttable@beta';
      websiteUrl = websiteUrl || 'https://lighttable.app/';
      websiteLabel = tr('Download DMG from Website');
    }
  } else if (platform === 'linux') {
    websiteUrl = websiteUrl || 'https://lighttable.app/linux.html';
    websiteLabel = tr('Download Linux Package');
    const lowerOwner = owner.toLowerCase();
    if (lowerOwner.includes('arch') || lowerOwner.includes('omarchy') || lowerOwner.includes('aur')) {
      explanation = explanation || tr('Updates are managed by your Arch Linux package manager or AUR helper.');
      command = command || 'yay -Syu lighttable-bin';
    } else if (lowerOwner.includes('flatpak')) {
      explanation = explanation || tr('Updates are managed by Flatpak.');
      command = command || 'flatpak update app.lighttable.LightTable';
    } else if (lowerOwner.includes('snap')) {
      explanation = explanation || tr('Updates are managed by Snap.');
      command = command || 'sudo snap refresh lighttable';
    } else {
      explanation = explanation || tr('Download the latest portable Linux archive from the website.');
    }
  } else if (platform === 'windows') {
    websiteUrl = websiteUrl || 'https://lighttable.app/windows.html';
    websiteLabel = tr('Download Windows Installer');
    const lowerOwner = owner.toLowerCase();
    if (lowerOwner.includes('scoop')) {
      explanation = explanation || tr('Updates are managed by Scoop.');
      command = command || 'scoop update lighttable';
    } else if (lowerOwner.includes('winget')) {
      explanation = explanation || tr('Updates are managed by WinGet.');
      command = command || 'winget upgrade NicholasReville.LightTable';
    } else {
      explanation = explanation || tr('Download the latest Windows installer or portable archive from the website.');
    }
  } else {
    explanation = explanation || tr('A new version of LightTable is available. Visit the website to download the update.');
    websiteUrl = websiteUrl || 'https://lighttable.app/';
  }

  const releaseNotesUrl = options.releaseNotesUrl
    || (version
      ? `https://github.com/reville/lighttable-digital-darkroom/releases/tag/v${version}`
      : 'https://github.com/reville/lighttable-digital-darkroom/releases');

  return {
    platform,
    explanation,
    command,
    websiteUrl,
    websiteLabel,
    releaseNotesUrl,
    version,
    currentVersion: options.currentVersion || ''
  };
}

let activeDialog = null;
let previousFocusedElement = null;

export function closeManualUpdateModal() {
  if (!activeDialog) return;
  activeDialog.classList.remove('on');
  activeDialog.setAttribute('aria-hidden', 'true');
  if (previousFocusedElement && typeof previousFocusedElement.focus === 'function') {
    try { previousFocusedElement.focus(); } catch (_) {}
  }
  previousFocusedElement = null;
  activeDialog = null;
}

export function showManualUpdateModal(options = {}) {
  if (typeof document === 'undefined') return null;

  closeManualUpdateModal();
  previousFocusedElement = document.activeElement;

  let dialog = document.getElementById('manualUpdateDialog');
  if (!dialog) {
    dialog = document.createElement('div');
    dialog.id = 'manualUpdateDialog';
    dialog.className = 'modal-backdrop manual-update-backdrop';
    dialog.setAttribute('aria-hidden', 'true');
    document.body.appendChild(dialog);
  }

  const info = getPlatformUpdateInstructions(options);
  const titleText = info.version
    ? tr('LightTable {version} Available', { version: info.version })
    : tr('Update LightTable');

  let versionBadge = '';
  if (info.currentVersion && info.version) {
    versionBadge = `<span class="manual-update-version-badge">${escapeHTML(tr('Current: v{current} · Latest: v{latest}', { current: info.currentVersion, latest: info.version }))}</span>`;
  }

  let commandHtml = '';
  if (info.command) {
    commandHtml = `
      <div class="manual-update-command-card">
        <span class="manual-update-command-label">${escapeHTML(tr('Terminal command:'))}</span>
        <div class="manual-update-command-row">
          <code class="manual-update-command-code" id="manualUpdateCommandCode">${escapeHTML(info.command)}</code>
          <button type="button" class="compact-btn manual-update-copy-btn" id="manualUpdateCopyBtn" title="${escapeHTML(tr('Copy command to clipboard'))}">${escapeHTML(tr('Copy'))}</button>
        </div>
      </div>
    `;
  }

  dialog.innerHTML = `
    <div class="modal manual-update-modal" role="dialog" aria-modal="true" aria-labelledby="manualUpdateTitle">
      <div class="manual-update-header">
        <div>
          <strong id="manualUpdateTitle">${escapeHTML(titleText)}</strong>
          ${versionBadge}
        </div>
        <button type="button" class="quiet icon-btn manual-update-close-x" id="manualUpdateCloseX" aria-label="${escapeHTML(tr('Close'))}" title="${escapeHTML(tr('Close'))}">×</button>
      </div>
      <p class="manual-update-explanation">${escapeHTML(info.explanation)}</p>
      ${commandHtml}
      <div class="modal-actions manual-update-actions">
        <button type="button" class="quiet" id="manualUpdateNotesBtn">${escapeHTML(tr('Release Notes'))}</button>
        <button type="button" class="quiet" id="manualUpdateLaterBtn">${escapeHTML(tr('Later'))}</button>
        <button type="button" class="primary-btn accent-btn" id="manualUpdateWebsiteBtn">${escapeHTML(info.websiteLabel)}</button>
      </div>
    </div>
  `;

  activeDialog = dialog;
  if (!boundDialogs.has(dialog)) {
    boundDialogs.add(dialog);
    dialog.addEventListener('keydown', (e) => {
      if (!dialog.classList.contains('on')) return;
      if (e.key === 'Escape') {
        e.stopPropagation();
        e.preventDefault();
        closeManualUpdateModal();
      }
    });
    dialog.addEventListener('click', (e) => {
      if (e.target === dialog) closeManualUpdateModal();
    });
  }

  const openUrl = (url) => {
    if (!url) return;
    if (!sendNative('openExternalUrl', { url })) {
      if (typeof window !== 'undefined') window.open(url, '_blank', 'noopener');
    }
  };

  dialog.querySelector('#manualUpdateCloseX')?.addEventListener('click', closeManualUpdateModal);
  dialog.querySelector('#manualUpdateLaterBtn')?.addEventListener('click', closeManualUpdateModal);

  const copyBtn = dialog.querySelector('#manualUpdateCopyBtn');
  if (copyBtn && info.command) {
    copyBtn.addEventListener('click', async () => {
      try {
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(info.command);
        } else if (typeof document !== 'undefined') {
          const ta = document.createElement('textarea');
          ta.value = info.command;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          ta.remove();
        }
        copyBtn.textContent = tr('Copied!');
        setTimeout(() => {
          if (copyBtn.isConnected) copyBtn.textContent = tr('Copy');
        }, 2000);
      } catch (_) {
        copyBtn.textContent = tr('Failed to copy');
        setTimeout(() => {
          if (copyBtn.isConnected) copyBtn.textContent = tr('Copy');
        }, 2000);
      }
    });
  }

  dialog.querySelector('#manualUpdateWebsiteBtn')?.addEventListener('click', () => {
    openUrl(info.websiteUrl);
  });

  dialog.querySelector('#manualUpdateNotesBtn')?.addEventListener('click', () => {
    openUrl(info.releaseNotesUrl);
  });

  dialog.classList.add('on');
  dialog.setAttribute('aria-hidden', 'false');

  const focusTarget = copyBtn || dialog.querySelector('#manualUpdateWebsiteBtn') || dialog.querySelector('#manualUpdateLaterBtn');
  if (focusTarget && typeof focusTarget.focus === 'function') {
    focusTarget.focus();
  }

  return dialog;
}

if (typeof window !== 'undefined') {
  window.lightTableShowUpdateModal = showManualUpdateModal;
  window.lightTableCloseUpdateModal = closeManualUpdateModal;
}
