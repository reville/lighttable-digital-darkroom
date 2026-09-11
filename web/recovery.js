// SPDX-License-Identifier: GPL-3.0-only
import { t as tr, tn as trn } from './i18n.js';
/* Library Health: what the window shows about integrity, and the choices it
 * offers when something has gone wrong.
 *
 * The server never decides for the person whose library this is. When the
 * catalog is damaged it opens in folder mode and this dialog lays out the
 * options with what each one holds, so restoring a backup that discards a
 * day of edits is a choice rather than a surprise. Everything destructive is
 * two clicks: the first arms the button, the second acts.
 */

const BYTES = [['GB', 1e9], ['MB', 1e6], ['KB', 1e3]];

function bytesLabel(value) {
  const n = Number(value) || 0;
  for (const [unit, size] of BYTES) {
    if (n >= size) return `${(n / size).toFixed(n >= 10 * size ? 0 : 1)} ${unit}`;
  }
  return `${n} B`;
}

function whenLabel(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  return date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  });
}

function summaryLabel(summary) {
  if (!summary) return tr("contents unknown");
  const parts = [tr("{value} photos", {value: summary.images ?? 0})];
  if (summary.edited) parts.push(tr("{summaryEdited} edited", {summaryEdited: summary.edited}));
  if (summary.history) parts.push(tr("{summaryHistory} history steps", {summaryHistory: summary.history}));
  if (summary.collections) parts.push(tr("{summaryCollections} collections", {summaryCollections: summary.collections}));
  return parts.join(' · ');
}

export function installRecovery(ctx) {
  const { el, post, get, toast, sendNative } = ctx;
  const dialog = el('healthDialog');
  const banner = el('healthBanner');
  const overlay = el('healthOverlay');
  let status = null;
  let busy = false;
  let bootPid = null;
  let bannerDismissed = new Set();
  let reconnectTimer = null;
  let reconnectStarted = 0;
  try {
    bannerDismissed = new Set(JSON.parse(
      sessionStorage.getItem('lighttable-health-dismissed') || '[]'));
  } catch { bannerDismissed = new Set(); }

  /* ------------------------------------------------------------ helpers */

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function button(label, onClick, { quiet = true, arm = null } = {}) {
    const control = node('button', quiet ? 'quiet compact' : 'compact', label);
    control.type = 'button';
    if (!arm) {
      control.onclick = () => onClick();
      return control;
    }
    // Two-step confirmation without a native dialog: the first click arms
    // the button for a few seconds, the second click acts.
    let armed = null;
    control.onclick = () => {
      if (armed) {
        clearTimeout(armed); armed = null;
        control.textContent = label; control.classList.remove('armed');
        onClick();
        return;
      }
      control.textContent = arm; control.classList.add('armed');
      armed = setTimeout(() => {
        armed = null; control.textContent = label;
        control.classList.remove('armed');
      }, 6000);
    };
    return control;
  }

  function row(label, value, valueClass = '') {
    const line = node('div', 'health-row');
    line.appendChild(node('span', 'health-key', label));
    const cell = node('span', `health-value ${valueClass}`.trim());
    if (value instanceof Node) cell.appendChild(value); else cell.textContent = value;
    line.appendChild(cell);
    return line;
  }

  function setBusy(value) {
    busy = value;
    dialog.classList.toggle('busy', value);
    dialog.querySelectorAll('button').forEach((control) => {
      if (control.id !== 'healthClose') control.disabled = value;
    });
  }

  async function act(action, extra = {}) {
    if (busy) return null;
    setBusy(true);
    try {
      const result = await post('/api/recovery', { action, ...extra });
      if (result?.error) throw new Error(result.error);
      if (result?.restart) {
        showOverlay(tr("LightTable is restarting to open the recovered catalog…"));
        watchServer(true);
        return result;
      }
      await load();
      return result;
    } catch (error) {
      toast(((error?.message || String(error))));
      return null;
    } finally {
      setBusy(false);
    }
  }

  /* ------------------------------------------------------------- render */

  function renderDecision(container) {
    container.replaceChildren();
    const catalog = status.catalog || {};
    if (!catalog.damaged) { container.hidden = true; return; }
    container.hidden = false;
    const notice = catalog.notice || {};
    container.appendChild(node('h3', '', tr("The catalog is damaged")));
    container.appendChild(node('p', 'health-note',
      tr("{value} Photos are open in folder mode and nothing has been changed. Choose how to recover; whatever is at the catalog path now is kept in the Recovery folder.", {value: (notice.message || tr("SQLite could not read it."))})));
    const choices = node('div', 'health-choices');
    const salvage = status.salvage || {};
    const backups = (status.backups?.items || []);
    const newest = backups[0];
    if (salvage.available) {
      const card = node('div', 'health-choice');
      card.appendChild(node('strong', '', tr("Salvage the damaged catalog")));
      card.appendChild(node('p', '',
        tr("Copies every readable row into a fresh file. Readable now: {value}. Keeps edits made after the last backup; rows on damaged pages are lost.", {value: summaryLabel(salvage.readable)})));
      card.appendChild(button(tr("Salvage and restart"), () => act('rebuild'),
        { quiet: false, arm: tr("Confirm salvage") }));
      choices.appendChild(card);
    }
    if (newest) {
      const card = node('div', 'health-choice');
      card.appendChild(node('strong', '', tr("Restore the newest backup")));
      card.appendChild(node('p', '',
        tr("{value} · {value2}. Edits made after that backup are not in it.", {value: whenLabel(newest.modified), value2: summaryLabel(newest.summary)})));
      card.appendChild(button(tr("Restore and restart"),
        () => act('restore', { archive: newest.path }),
        { quiet: false, arm: tr("Confirm restore") }));
      choices.appendChild(card);
    }
    const fresh = node('div', 'health-choice');
    fresh.appendChild(node('strong', '', tr("Start a new catalog")));
    fresh.appendChild(node('p', '',
      tr("An empty catalog that rescans your folders. Ratings, edits, collections, and history are not carried over.")));
    fresh.appendChild(button(tr("Start fresh and restart"),
      () => act('reset', { confirm: true }), { arm: tr("Confirm new catalog") }));
    choices.appendChild(fresh);
    container.appendChild(choices);
    if (!salvage.available && !newest) {
      container.appendChild(node('p', 'health-note',
        tr("No backup is available and the file cannot be read at all. Starting fresh is the remaining option; the damaged file stays in Recovery.")));
    }
  }

  function renderStatus(container) {
    container.replaceChildren();
    const catalog = status.catalog || {};
    const verify = status.verify;
    const disk = status.disk || {};
    let state = tr("Healthy");
    let stateClass = 'ok';
    if (catalog.damaged) { state = tr("Damaged"); stateClass = 'bad'; }
    else if (!catalog.enabled && catalog.configured) {
      state = (catalog.notice?.status === 'incompatible' ? tr("Newer than this build") : tr("Not open"));
      stateClass = 'warn';
    } else if (!catalog.configured) { state = tr("Folder mode"); stateClass = ''; }
    else if (verify && !verify.ok) { state = tr("Needs attention"); stateClass = 'warn'; }
    container.appendChild(row(tr("Catalog"), state, stateClass));
    container.appendChild(row(tr("Location"), ((catalog.path || '—')), 'mono'));
    if (catalog.syncService) {
      container.appendChild(row(tr("Warning"),
        tr("The catalog sits inside {catalogSyncService}. Databases inside a syncing folder are a common cause of corruption.", {catalogSyncService: catalog.syncService}), 'warn'));
    }
    if (catalog.stats) {
      const s = catalog.stats;
      container.appendChild(row(tr("Contents"),
        tr("{value} photos · {value2} sources{value3}", {value: (s.images ?? 0), value2: (s.sources ?? 0), value3: s.missing ? tr(" · {sMissing} missing files", {sMissing: s.missing}) : ''})));
    }
    container.appendChild(row(tr("Last check"), verify
      ? tr('{time} · {result}', {time: whenLabel(verify.checkedAt),
        result: verify.ok ? tr('no problems') : (verify.problems || []).join('; ')})
      : tr("Not checked yet this session"), verify && !verify.ok ? 'warn' : ''));
    container.appendChild(row(tr("Disk"),
      disk.freeBytes !== undefined ? disk.low ? tr("{value} free of {value2} — almost full; backups are paused", {value: bytesLabel(disk.freeBytes), value2: bytesLabel(disk.totalBytes)}) : tr("{value} free of {value2}", {value: bytesLabel(disk.freeBytes), value2: bytesLabel(disk.totalBytes)}) : '—', disk.low ? 'bad' : ''));
    for (const doc of status.documents || []) {
      if (doc.status === 'ok' || doc.status === 'missing') continue;
      container.appendChild(row(doc.path.split('/').pop(), doc.message, 'warn'));
    }
    if (status.safeMode) {
      container.appendChild(row(tr("Mode"),
        tr("Safe Mode: scanning, watched folders, local AI, and engine warm-up are off. Use Help ▸ Diagnostics ▸ Restart Rendering Service to return to normal."), 'warn'));
    }
    if (status.launch?.status === 'folder-missing') {
      container.appendChild(row(tr("Folder"), status.launch.message, 'warn'));
    }
  }

  function renderActions(container) {
    container.replaceChildren();
    const catalog = status.catalog || {};
    const open = !!catalog.enabled;
    const verify = status.verify;
    container.appendChild(button(tr("Check now"), () => act('verify')));
    const repair = button(tr("Repair"), () => act('repair'), { arm: tr("Confirm repair") });
    repair.title = tr("Backs up first, then removes orphaned rows, rebuilds the search index, and checkpoints the log.");
    container.appendChild(repair);
    container.appendChild(button(tr("Rescan all folders"), () => act('rescan')));
    container.appendChild(button(tr("Rebuild search index"), () => act('rebuild-search')));
    const clearCaches = button(tr("Clear preview caches"), () => act('clear-caches'),
      { arm: tr("Confirm clear") });
    container.appendChild(clearCaches);
    if (open && verify && !verify.ok && !verify.repairable) {
      const rebuild = button(tr("Rebuild catalog file"),
        () => act('rebuild'), { arm: tr("Confirm rebuild") });
      rebuild.title = tr("SQLite reports page-level damage. This salvages every readable row into a fresh file and restarts.");
      container.appendChild(rebuild);
    }
    container.querySelectorAll('button').forEach((control) => {
      if (!open && control !== clearCaches) {
        control.disabled = true;
      }
    });
  }

  function renderVerify(container) {
    container.replaceChildren();
    const verify = status.verify;
    if (!verify) return;
    const lines = [];
    lines.push(tr("Structure: {value}", {value: (verify.integrity === 'ok' ? tr("ok") : [].concat(verify.integrity).slice(0, 3).join('; '))}));
    if (verify.foreignKeyViolations) lines.push(tr("Foreign keys: {verifyForeignKeyViolations} violations", {verifyForeignKeyViolations: verify.foreignKeyViolations}));
    const orphans = Object.entries(verify.orphans || {}).filter(([, n]) => n > 0);
    if (orphans.length) lines.push(tr("Orphaned rows: {value}", {value: orphans.map(([t, n]) => `${t} ${n}`).join(', ')}));
    const search = verify.searchIndex || {};
    lines.push(tr("Search index: {value}", {value: (search.ok ? tr("in step") : tr("{value} stale, {value2} missing", {value: search.stale || 0, value2: search.unindexed || 0}))}));
    if (verify.missingFiles) lines.push(tr("Missing files: {verifyMissingFiles} (kept with their edits)", {verifyMissingFiles: verify.missingFiles}));
    lines.push(tr("Write-ahead log: {value}", {value: bytesLabel(verify.walBytes)}));
    for (const line of lines) container.appendChild(node('div', 'health-line', line));
  }

  function renderBackups(container) {
    container.replaceChildren();
    const backups = status.backups || {};
    const items = backups.items || [];
    container.appendChild(node('div', 'health-line mono', ((backups.directory || ''))));
    if (!items.length) {
      container.appendChild(node('div', 'health-line', tr("No backups yet.")));
      return;
    }
    for (const item of items) {
      const line = node('div', 'health-item');
      const text = node('div', 'health-item-text');
      text.appendChild(node('strong', '', whenLabel(item.modified)));
      text.appendChild(node('span', '',
        `${bytesLabel(item.size)} · ${summaryLabel(item.summary)}`));
      line.appendChild(text);
      const restore = button(tr("Restore…"),
        () => act('restore', { archive: item.path }), { arm: tr("Confirm restore") });
      restore.title = status.catalog?.enabled ? tr("The current catalog is backed up first, then replaced by this one.") : tr("Replaces the damaged catalog with this backup.");
      line.appendChild(restore);
      container.appendChild(line);
    }
  }

  function renderQuarantine(container) {
    container.replaceChildren();
    const entries = status.quarantine || [];
    if (!entries.length) {
      container.appendChild(node('div', 'health-line',
        tr("None. A photo is set aside after processing it crashes LightTable twice.")));
      return;
    }
    for (const entry of entries) {
      const line = node('div', 'health-item');
      const text = node('div', 'health-item-text');
      text.appendChild(node('strong', '', entry.name));
      const details = {stage: entry.stage === 'manual' ? tr('set aside by hand') : entry.stage,
        time: whenLabel(entry.lastAt)};
      text.appendChild(node('span', '', entry.quarantined
        ? trn('{count} crash while {stage} · {time} · set aside', '{count} crashes while {stage} · {time} · set aside', entry.strikes, details)
        : trn('{count} crash while {stage} · {time} · still open', '{count} crashes while {stage} · {time} · still open', entry.strikes, details)));
      line.appendChild(text);
      line.appendChild(button(tr("Release"), () => act('release', { name: entry.name })));
      container.appendChild(line);
    }
  }

  function renderSessions(container) {
    container.replaceChildren();
    const session = status.session || {};
    const crashes = session.recentCrashes || [];
    if (session.previousCrash) {
      const inflight = session.previousCrash.inflight;
      container.appendChild(node('div', 'health-line warn',
        tr("The previous session did not end cleanly{value}.", {value: inflight?.name ? tr(" while {value} {inflightName}", {value: inflight.stage === 'decode' ? tr("decoding") : tr("{inflightStage}ing", {inflightStage: inflight.stage}), inflightName: inflight.name}) : ''})));
    }
    container.appendChild(node('div', 'health-line',
      trn("{value} unexpected exit in the last 24 hours.", "{value} unexpected exits in the last 24 hours.", session.crashes24h, {value: (session.crashes24h || 0)})));
    for (const crash of crashes.slice().reverse()) {
      container.appendChild(node('div', 'health-line muted',
        `${whenLabel(crash.detectedAt)}`
        + (crash.inflight?.name ? ` · ${crash.inflight.name}` : '')
        + (crash.exitStatus ? ` · exit ${crash.exitStatus}` : '')));
    }
  }

  function render() {
    if (!status) return;
    el('healthSubtitle').textContent = status.catalog?.path ? status.catalog.path.split('/').slice(-3, -1).join('/') : '';
    renderDecision(el('healthDecision'));
    renderStatus(el('healthStatus'));
    renderActions(el('healthActions'));
    renderVerify(el('healthVerify'));
    renderBackups(el('healthBackups'));
    renderQuarantine(el('healthQuarantine'));
    renderSessions(el('healthSessions'));
    renderBanner();
  }

  /* ------------------------------------------------------------- banner */

  function dismissKey(kind) { return `${kind}:${status?.session?.startedAt || ''}`; }

  function renderBanner() {
    if (!banner || !status) return;
    const catalog = status.catalog || {};
    const session = status.session || {};
    const messages = [];
    if (catalog.damaged) {
      messages.push({ kind: 'damaged', tone: 'bad',
        text: tr("The catalog is damaged. Photos are open in folder mode until you choose how to recover."),
        action: tr("Choose…") });
    } else if (catalog.notice?.status === 'recovered') {
      messages.push({ kind: 'recovered', tone: 'warn',
        text: tr("The catalog was restored from a verified backup; the damaged copy is in the Recovery folder."),
        action: tr("Details") });
    } else if (catalog.notice?.status === 'incompatible') {
      messages.push({ kind: 'incompatible', tone: 'warn',
        text: tr("This catalog was made by a newer LightTable. It was left unchanged; update the app to open it."),
        action: tr("Details") });
    }
    if (session.previousCrash) {
      const name = session.previousCrash.inflight?.name;
      const aside = (status.quarantine || []).find((entry) => entry.name === name && entry.quarantined);
      messages.push({ kind: 'crash', tone: 'warn',
        text: (aside ? tr("LightTable did not shut down cleanly last time. {name} crashed it repeatedly and has been set aside.", {name: name}) : tr("LightTable did not shut down cleanly last time.")),
        action: tr("Review") });
    }
    if (status.disk?.low) {
      messages.push({ kind: 'disk', tone: 'bad',
        text: tr("The disk holding the catalog is almost full. Backups are paused until space is freed."),
        action: tr("Details") });
    }
    if (status.launch?.status === 'folder-missing') {
      messages.push({ kind: 'folder', tone: 'warn',
        text: tr("The folder LightTable last opened is not available right now."), action: tr("Details") });
    }
    if (status.safeMode) {
      messages.push({ kind: 'safe', tone: 'warn',
        text: tr("Safe Mode: background services are off."), action: tr("Library Health") });
    }
    if (status.verify && !status.verify.ok && !catalog.damaged) {
      messages.push({ kind: 'verify', tone: 'warn',
        text: tr("The last catalog check found problems that Repair can fix."), action: tr("Review") });
    }
    const visible = messages.filter((item) => !bannerDismissed.has(dismissKey(item.kind)));
    banner.replaceChildren();
    if (!visible.length) { banner.hidden = true; return; }
    const first = visible[0];
    banner.hidden = false;
    banner.className = `health-banner ${first.tone}`;
    banner.appendChild(node('span', 'health-banner-text', first.text
      + (visible.length > 1 ? ` (+${visible.length - 1} more)` : '')));
    const act1 = node('button', 'compact', first.action);
    act1.type = 'button'; act1.onclick = () => open();
    banner.appendChild(act1);
    if (first.kind !== 'damaged') {
      const dismiss = node('button', 'quiet compact', tr("Dismiss"));
      dismiss.type = 'button';
      dismiss.onclick = () => {
        bannerDismissed.add(dismissKey(first.kind));
        try {
          sessionStorage.setItem('lighttable-health-dismissed',
            JSON.stringify([...bannerDismissed]));
        } catch { /* private mode */ }
        renderBanner();
      };
      banner.appendChild(dismiss);
    }
  }

  /* ---------------------------------------------------------- reconnect */

  function showOverlay(text) {
    if (!overlay) return;
    el('healthOverlayText').textContent = text;
    overlay.hidden = false;
  }

  function hideOverlay() { if (overlay) overlay.hidden = true; }

  function watchServer(expectRestart = false) {
    if (reconnectTimer) return;
    reconnectStarted = performance.now();
    const poll = async () => {
      let health = null;
      try { health = await get('/api/health'); } catch { health = null; }
      if (health?.ok && !health.restarting) {
        reconnectTimer = null;
        if (expectRestart || (bootPid && health.pid !== bootPid)) {
          location.reload();
          return;
        }
        hideOverlay();
        return;
      }
      const elapsed = performance.now() - reconnectStarted;
      if (elapsed > 90000) {
        showOverlay(tr("LightTable’s engine has not come back. Use Help ▸ Diagnostics ▸ Restart Rendering Service, or check the server log."));
      }
      reconnectTimer = setTimeout(poll, elapsed > 20000 ? 3000 : 1200);
    };
    reconnectTimer = setTimeout(poll, expectRestart ? 1500 : 800);
  }

  window.addEventListener('lighttable-server-connection', (event) => {
    const state = event.detail?.state;
    if (state === 'lost') {
      if (reconnectTimer) return;
      // A single dropped event stream reconnects on its own; only show
      // the overlay once the health check also fails.
      setTimeout(async () => {
        let health = null;
        try { health = await get('/api/health'); } catch { health = null; }
        if (health?.ok && !health.restarting) return;
        showOverlay(tr("Reconnecting to LightTable’s engine…"));
        watchServer(!!health?.restarting);
      }, 600);
    }
  });

  /* ------------------------------------------------------------- dialog */

  async function load({ backups = true } = {}) {
    status = await get(`/api/recovery?backups=${backups ? 1 : 0}`);
    if (status?.error) throw new Error(status.error);
    render();
    return status;
  }

  function open() {
    if (!dialog) return;
    dialog.classList.add('on');
    dialog.setAttribute('aria-hidden', 'false');
    load().catch((error) => toast(((error?.message || tr("Library Health is unavailable")))));
    el('healthClose')?.focus();
  }

  function close() {
    if (!dialog) return;
    dialog.classList.remove('on');
    dialog.setAttribute('aria-hidden', 'true');
  }

  if (dialog) {
    el('healthClose').onclick = close;
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) close();
    });
    dialog.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); close(); }
    });
    el('healthBackupNow').onclick = () => act('backup').then((result) => {
      if (result?.archive) toast(tr("Backup written"));
    });
    el('healthShowLog').onclick = () => {
      if (!sendNative('showServerLog')) toast(tr("The server log is available in the desktop app"));
    };
    el('healthRecoveryFolder').onclick = () => {
      if (!sendNative('openRecoveryFolder')) {
        toast(((status?.recoveryFolder || tr("Recovery folder is beside the catalog"))));
      }
    };
  }
  ['healthOpen', 'catalogHealth', 'settingsLibraryHealth'].forEach((id) => {
    const control = el(id);
    if (control) control.onclick = () => open();
  });

  // Learn which server this window belongs to, then show any banner that
  // the first health facts justify without opening the dialog.
  get('/api/health').then((health) => {
    bootPid = health?.pid || null;
    const library = health?.library || {};
    if (health.safeMode || library.status !== 'ok' || library.previousCrash || library.diskLow
        || library.launch || library.quarantined) {
      return load({ backups: false }).catch(() => {});
    }
    return null;
  }).catch(() => {});

  return { open, close, load, watchServer };
}
