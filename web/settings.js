import { api } from '/web/api.js';
import { sendNative } from '/web/native-bridge.js';

const byId = (id) => document.getElementById(id);
const VALID_BACKGROUNDS = new Set(['#0f0f0f', '#121212', '#252525']);

function bytesLabel(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export function installSettings(context) {
  const dialog = byId('settingsDialog');
  if (!dialog) return null;
  let returnFocus = null;
  let currentPrefs = {};

  const pref = (key, fallback) => currentPrefs[key] ?? fallback;
  const setStatus = (text) => { byId('settingsSaveStatus').textContent = text; };
  const savePatch = async (patch, message = 'Saved') => {
    currentPrefs = { ...currentPrefs, ...patch };
    context.setPrefs(currentPrefs);
    applyAppearance();
    setStatus('Saving…');
    const result = await api('/api/prefs', patch);
    setStatus(result?.error ? 'Could not save changes.' : message);
    return result;
  };

  function applyAppearance() {
    const background = VALID_BACKGROUNDS.has(pref('viewerBackground', '#121212'))
      ? pref('viewerBackground', '#121212') : '#121212';
    document.documentElement.style.setProperty('--viewer-bg', background);
    document.documentElement.style.setProperty('--ui-font-scale',
      String(Math.max(.85, Math.min(1.25, Number(pref('uiFontScale', 100)) / 100))));
    document.documentElement.style.setProperty('--lights-out-opacity',
      String(Math.max(.15, Math.min(.75, 1 - Number(pref('lightsOutDim', 60)) / 100))));
    document.body.classList.toggle('lights-out', !!pref('lightsOutEnabled', false));
    document.body.dataset.cropGuide = ['thirds', 'golden', 'diagonal', 'none']
      .includes(pref('cropGuide', 'thirds')) ? pref('cropGuide', 'thirds') : 'thirds';
    document.body.dataset.completionNotifications =
      pref('completionNotifications', false) ? '1' : '0';
    const overlay = byId('loupeInfoOverlay');
    if (overlay) overlay.hidden = !pref('loupeInfoEnabled', false);
    const keyword = byId('keywordInput');
    if (keyword) {
      if (pref('keywordAutocomplete', true)) keyword.setAttribute('list', 'keywordSuggestions');
      else keyword.removeAttribute('list');
    }
    sendNative('viewerAppearance', { color: background });
  }

  function showTab(name) {
    const selected = dialog.querySelector(`[data-settings-tab="${CSS.escape(name)}"]`)
      || dialog.querySelector('[data-settings-tab]');
    if (!selected) return;
    dialog.querySelectorAll('[data-settings-tab]').forEach((button) => {
      const on = button === selected;
      button.classList.toggle('on', on);
      button.setAttribute('aria-selected', String(on));
    });
    dialog.querySelectorAll('[data-settings-pane]').forEach((pane) => {
      pane.classList.toggle('on', pane.dataset.settingsPane === selected.dataset.settingsTab);
    });
  }

  async function refreshLocations() {
    const [cache, catalog] = await Promise.all([
      fetch('/api/cache/status').then((response) => response.json()).catch(() => null),
      fetch('/api/catalog').then((response) => response.json()).catch(() => null),
    ]);
    if (cache) {
      byId('cacheLocation').textContent = cache.path || 'Unavailable';
      byId('cacheUsage').textContent = `${bytesLabel(cache.usedBytes)} used · `
        + `${bytesLabel(cache.budgetBytes)} budget`;
    }
    byId('catalogLocation').textContent = catalog?.path || 'Folder mode · no catalog';
    if (!byId('backupDirectory').value && catalog?.backupPath) {
      byId('backupDirectory').placeholder = catalog.backupPath;
    }
    byId('settingsBackupNow').disabled = !catalog?.enabled;
  }

  async function populate() {
    const loaded = await fetch('/api/prefs').then((response) => response.json())
      .catch(() => ({}));
    currentPrefs = { ...loaded };
    context.setPrefs(currentPrefs);
    byId('autoAdvance').checked = pref('autoAdvance', true);
    byId('completionNotifications').checked = pref('completionNotifications', false);
    byId('automaticUpdateChecks').checked = pref('automaticUpdateChecks', true);
    byId('viewerBackground').value = pref('viewerBackground', '#121212');
    byId('uiFontScale').value = String(pref('uiFontScale', 100));
    byId('uiFontScaleV').textContent = `${byId('uiFontScale').value}%`;
    byId('lightsOutEnabled').checked = pref('lightsOutEnabled', false);
    byId('lightsOutDim').value = String(pref('lightsOutDim', 60));
    byId('lightsOutDimV').textContent = `${byId('lightsOutDim').value}%`;
    byId('loupeInfoEnabled').checked = pref('loupeInfoEnabled', false);
    byId('cropGuide').value = pref('cropGuide', 'thirds');
    const defaults = pref('newPhotoDefaults', {});
    byId('newPhotoFilmEnabled').checked = defaults.filmEnabled === true;
    byId('newPhotoWorkflow').value = defaults.workflow || 'authentic';
    byId('newPhotoDevelopProfile').value = defaults.developProfile || 'standard';
    const presets = await fetch('/api/presets').then((response) => response.json())
      .catch(() => []);
    byId('newPhotoPreset').replaceChildren(new Option('None', ''),
      ...presets.map((item) => new Option(item.name, item.name)));
    byId('newPhotoPreset').value = defaults.preset || '';
    byId('rawDefaultMatch').value = pref('rawDefaultMatch', 'model');
    byId('writeSidecars').checked = pref('writeSidecars', false);
    byId('pairRawJPEG').checked = pref('pairRawJPEG', true);
    byId('hidePairedJPEG').checked = pref('hidePairedJPEG', false);
    byId('hidePairedJPEG').disabled = !byId('pairRawJPEG').checked;
    byId('keywordAutocomplete').checked = pref('keywordAutocomplete', true);
    byId('keywordSeparators').value = pref('keywordSeparators', 'comma');
    byId('settingsExternalEditSpace').value = pref('externalEditorSpace', 'prophoto');
    byId('settingsExternalEditBitDepth').value = String(pref('externalEditBitDepth', 16));
    byId('settingsExternalEditStack').checked = pref('externalEditStack', true);
    byId('settingsEngine').value = pref('engine', byId('engine')?.value || 'auto');
    byId('settingsPreviewWidth').value = pref('pw', byId('pw')?.value || 'auto');
    const budget = Math.round(Number(pref('cacheBudgetGB', 7.5)));
    byId('cacheBudgetGB').value = String(Math.max(2, Math.min(32, budget)));
    byId('cacheBudgetGBV').textContent = `${byId('cacheBudgetGB').value} GB`;
    byId('backupFrequency').value = pref('backupFrequency', 'daily');
    byId('backupDirectory').value = pref('backupDirectory', '');
    byId('keySchemeSelect').value = pref('keyScheme', 'lighttable');
    byId('speedKeysEnabled').checked = pref('speedKeys', true);
    byId('allowAutomation').checked = pref('allowAutomation', true);
    const external = pref('externalEditor', {});
    if (external.path && !byId('settingsExternalEditor').querySelector(
      `option[value="${CSS.escape(external.path)}"]`)) {
      byId('settingsExternalEditor').appendChild(new Option(
        external.name || external.path.split('/').pop(), external.path));
    }
    byId('settingsExternalEditor').value = external.path || '';
    const raw = context.currentRawDefault();
    byId('settingsCameraDefaultStatus').textContent = raw
      ? `${raw.label}${raw.serial ? ` · ${raw.serial}` : ''}${raw.iso ? ` · ISO ${raw.iso}` : ''}`
      : 'Open a RAW photo to set its camera default.';
    byId('settingsSaveCameraDefault').disabled = !raw;
    byId('settingsResetCameraDefault').disabled = !raw?.settings;
    applyAppearance();
    refreshLocations();
  }

  async function open(tab = 'general') {
    returnFocus = document.activeElement;
    await populate();
    showTab(tab);
    dialog.classList.add('on');
    dialog.setAttribute('aria-hidden', 'false');
    dialog.querySelector(`[data-settings-tab="${CSS.escape(tab)}"]`)?.focus();
    sendNative('listEditors', {});
  }

  function close() {
    dialog.classList.remove('on');
    dialog.setAttribute('aria-hidden', 'true');
    returnFocus?.focus?.({ preventScroll: true });
    returnFocus = null;
  }

  dialog.querySelectorAll('[data-settings-tab]').forEach((button) => {
    button.addEventListener('click', () => showTab(button.dataset.settingsTab));
  });
  byId('settingsClose').onclick = close;
  byId('settingsDone').onclick = close;
  byId('openSettingsFromInfo').onclick = () => open('ai');
  dialog.addEventListener('pointerdown', (event) => {
    if (event.target === dialog) close();
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
  });

  const checkboxPrefs = {
    autoAdvance: 'autoAdvance', completionNotifications: 'completionNotifications',
    automaticUpdateChecks: 'automaticUpdateChecks', lightsOutEnabled: 'lightsOutEnabled',
    loupeInfoEnabled: 'loupeInfoEnabled', writeSidecars: 'writeSidecars',
    pairRawJPEG: 'pairRawJPEG', hidePairedJPEG: 'hidePairedJPEG',
    keywordAutocomplete: 'keywordAutocomplete', settingsExternalEditStack: 'externalEditStack',
  };
  Object.entries(checkboxPrefs).forEach(([id, key]) => {
    byId(id).addEventListener('change', async (event) => {
      if (id === 'pairRawJPEG') byId('hidePairedJPEG').disabled = !event.target.checked;
      await savePatch({ [key]: event.target.checked });
      if (['pairRawJPEG', 'hidePairedJPEG'].includes(id)) context.refreshPreferences();
      if (id === 'automaticUpdateChecks') {
        sendNative('automaticUpdateChecks', { enabled: event.target.checked });
      }
      if (id === 'completionNotifications' && event.target.checked) {
        sendNative('requestNotificationPermission', {});
      }
    });
  });
  const selectPrefs = {
    viewerBackground: 'viewerBackground', cropGuide: 'cropGuide',
    rawDefaultMatch: 'rawDefaultMatch', keywordSeparators: 'keywordSeparators',
    settingsExternalEditSpace: 'externalEditorSpace',
    settingsExternalEditBitDepth: 'externalEditBitDepth', backupFrequency: 'backupFrequency',
  };
  Object.entries(selectPrefs).forEach(([id, key]) => {
    byId(id).addEventListener('change', async (event) => {
      await savePatch({ [key]: event.target.value });
      if (id === 'rawDefaultMatch') await context.reloadCurrentRawDefault();
    });
  });
  for (const id of ['uiFontScale', 'lightsOutDim', 'cacheBudgetGB']) {
    byId(id).addEventListener('input', (event) => {
      byId(`${id}V`).textContent = `${event.target.value}${id === 'cacheBudgetGB' ? ' GB' : '%'}`;
      if (id !== 'cacheBudgetGB') {
        currentPrefs[id] = Number(event.target.value);
        applyAppearance();
      }
    });
    byId(id).addEventListener('change', (event) =>
      savePatch({ [id]: Number(event.target.value) }).then(refreshLocations));
  }
  const saveDefaults = async () => {
    await savePatch({ newPhotoDefaults: {
    filmEnabled: byId('newPhotoFilmEnabled').checked,
    workflow: byId('newPhotoWorkflow').value,
    developProfile: byId('newPhotoDevelopProfile').value,
    preset: byId('newPhotoPreset').value,
    } }, 'Saved · applies to unedited photos');
    await context.reloadDefaults();
  };
  for (const id of ['newPhotoFilmEnabled', 'newPhotoWorkflow',
    'newPhotoDevelopProfile', 'newPhotoPreset']) {
    byId(id).addEventListener('change', saveDefaults);
  }
  byId('backupDirectory').addEventListener('change', (event) =>
    savePatch({ backupDirectory: event.target.value.trim() }));
  byId('chooseBackupDirectory').onclick = () =>
    sendNative('choosePreferenceFolder', { key: 'backupDirectory' });
  window.addEventListener('lighttable-preference-folder', (event) => {
    if (event.detail?.key !== 'backupDirectory') return;
    byId('backupDirectory').value = event.detail.path || '';
    savePatch({ backupDirectory: byId('backupDirectory').value });
  });
  byId('writeSidecarsNow').onclick = async () => {
    byId('writeSidecarsNow').disabled = true;
    const result = await api('/api/sidecars/write', { names: context.imageNames() });
    byId('sidecarWriteStatus').textContent = result.error
      ? result.error : `Wrote ${result.written || 0} sidecar${result.written === 1 ? '' : 's'}.`;
    byId('writeSidecarsNow').disabled = false;
  };
  byId('purgeCache').onclick = async () => {
    if (!window.confirm('Purge generated previews and render caches? Originals and edits are not affected.')) return;
    byId('purgeCache').disabled = true;
    const result = await api('/api/cache/purge', {});
    byId('cacheUsage').textContent = result.error
      ? result.error : `Removed ${result.removed} files · ${bytesLabel(result.bytesRemoved)}`;
    byId('purgeCache').disabled = false;
  };
  byId('settingsBackupNow').onclick = async () => {
    byId('settingsBackupNow').disabled = true;
    const result = await api('/api/catalog/backup', {});
    byId('backupStatus').textContent = result.archive
      ? `Verified backup: ${result.archive}` : (result.error || 'Backup failed.');
    byId('settingsBackupNow').disabled = false;
  };
  byId('settingsSaveCameraDefault').onclick = () => {
    byId('rawSaveCameraDefault')?.click();
    setTimeout(populate, 250);
  };
  byId('settingsResetCameraDefault').onclick = () => {
    byId('rawResetCameraDefault')?.click();
    setTimeout(populate, 250);
  };
  byId('settingsExternalEditor').addEventListener('change', (event) => {
    if (event.target.value === '__choose__') {
      event.target.value = '';
      sendNative('chooseExternalEditor', {});
      return;
    }
    const name = event.target.selectedOptions[0]?.textContent || '';
    savePatch({ externalEditor: { path: event.target.value, name } });
  });
  byId('settingsEngine').addEventListener('change', (event) => {
    context.updateRuntimeControl('engine', event.target.value);
    savePatch({ engine: event.target.value });
  });
  byId('settingsPreviewWidth').addEventListener('change', (event) => {
    context.updateRuntimeControl('pw', event.target.value);
    savePatch({ pw: event.target.value });
  });

  window.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() !== 'l' || event.metaKey || event.ctrlKey ||
        event.altKey || ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target?.tagName) ||
        dialog.classList.contains('on')) return;
    currentPrefs.lightsOutEnabled = !pref('lightsOutEnabled', false);
    byId('lightsOutEnabled').checked = currentPrefs.lightsOutEnabled;
    savePatch({ lightsOutEnabled: currentPrefs.lightsOutEnabled });
  });

  populate();
  const controller = { open, close, apply: applyAppearance,
    prefs: () => currentPrefs };
  window.LightTableSettings = controller;
  return controller;
}
