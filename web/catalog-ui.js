/* The catalog pane: sources, migration, ingest, rename, and maintenance.
 *
 * This is where a library that spans several folders is managed, and where
 * someone arriving from another editor brings their work across. Everything
 * here talks to the server; the browser holds no library state of its own
 * beyond what it is currently showing.
 */

// Source names, card filenames, and watch names are text from disks and
// people, never markup.
const escapeHTML = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export function createCatalogUI(ctx) {
  const { el, post, get, toast, sendNative } = ctx;
  let catalog = null;
  let ingestPlan = null;
  let ingestPoll = null;
  let watchPoll = null;
  let watches = [];
  const watchSeen = new Map();
  let recoveryShown = false;
  const notifyCompletion = (title, message) => {
    if (document.body.dataset.completionNotifications === '1') {
      sendNative('notify', { title, message });
    }
  };

  /* ------------------------------------------------------------- sources */

  function renderSources() {
    const container = el('catalogSources');
    const summary = el('catalogSummary');
    const maintenance = el('catalogMaintenance');
    if (!container) return;
    if (!catalog || !catalog.enabled) {
      container.textContent = 'Running on a single folder.';
      if (summary) summary.textContent = 'Folder mode';
      if (maintenance && catalog?.recovery?.status === 'damaged') {
        maintenance.textContent = 'The catalog is damaged. Photos are open in '
          + 'folder mode and nothing has been changed. Open Library Health to '
          + 'salvage it, restore a backup, or start a new catalog.';
      } else if (maintenance && catalog?.recovery?.status === 'unavailable') {
        maintenance.textContent = 'The catalog could not be opened and no valid '
          + 'backup was available. Photos are open in safe folder mode; the '
          + 'damaged catalog has not been overwritten.';
      } else if (maintenance && catalog?.recovery?.status === 'incompatible') {
        maintenance.textContent = 'This catalog was created by a newer '
          + 'LightTable build. It was left unchanged; update the app to reopen it.';
      }
      return;
    }
    const sources = catalog.sources || [];
    const stats = catalog.stats || {};
    if (summary) {
      summary.textContent = `${stats.files || 0} photos · ${sources.length} `
        + `source${sources.length === 1 ? '' : 's'}`
        + (stats.missing ? ` · ${stats.missing} missing` : '');
    }
    container.innerHTML = sources.map((source) => `
      <div class="source-row${source.available ? '' : ' unavailable'}"
           data-id="${escapeHTML(source.id)}">
        <button class="source-star${source.favorite ? ' on' : ''}"
                data-act="favorite" title="Favourite">★</button>
        <span class="source-name" title="${escapeHTML(source.path)}">${escapeHTML(source.name)}</span>
        <span class="source-count">${Number(source.count) || 0}</span>
        <button class="source-act" data-act="rescan" title="Rescan">⟳</button>
        <button class="source-act" data-act="remove" title="Remove from catalog">×</button>
      </div>`).join('') || 'No sources yet.';
    if (maintenance && catalog.recovery?.status === 'recovered') {
      maintenance.textContent = 'Recovered the catalog from a verified backup. '
        + 'The damaged database was preserved in the Recovery folder.';
      if (!recoveryShown) {
        toast('Catalog recovered from a verified backup');
        recoveryShown = true;
      }
    }
  }

  async function refresh() {
    try {
      catalog = await get('/api/catalog');
    } catch (error) {
      catalog = null;
    }
    renderSources();
    return catalog;
  }

  function bindSources() {
    const container = el('catalogSources');
    if (container) {
      container.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-act]');
        if (!button) return;
        const row = button.closest('.source-row');
        const id = Number(row.dataset.id);
        const action = button.dataset.act;
        if (action === 'remove') {
          const source = (catalog.sources || []).find((s) => s.id === id);
          if (!window.confirm(
            `Remove “${source ? source.name : 'this folder'}” from the `
            + 'catalog? Photos and edits stay recoverable; adding the same '
            + 'folder again restores them.')) {
            return;
          }
          await post('/api/catalog/sources', { action: 'remove', id });
        } else if (action === 'favorite') {
          const source = (catalog.sources || []).find((s) => s.id === id);
          await post('/api/catalog/sources',
                     { action: 'favorite', id, favorite: !(source && source.favorite) });
        } else if (action === 'rescan') {
          await post('/api/catalog/scan', { sourceId: id });
          toast('Scanning…');
        }
        await refresh();
        if (ctx.onLibraryChanged) ctx.onLibraryChanged();
      });
    }

    const add = el('catalogAddSource');
    if (add) {
      add.addEventListener('click', () => {
        /* The host owns the folder picker; it answers with `addFolder`. */
        if (!sendNative('addFolder', {})) {
          toast('Adding folders needs the desktop app');
        }
      });
    }

    const rescan = el('catalogRescan');
    if (rescan) {
      rescan.addEventListener('click', async () => {
        await post('/api/catalog/scan', {});
        toast('Rescanning every source…');
      });
    }

    const backup = el('catalogBackup');
    if (backup) {
      backup.addEventListener('click', async () => {
        const result = await post('/api/catalog/backup', {});
        const target = el('catalogMaintenance');
        if (target) {
          target.textContent = result.archive
            ? `Backed up to ${result.archive}` : (result.error || 'Failed');
        }
      });
    }

    const duplicates = el('catalogDuplicates');
    if (duplicates) {
      duplicates.addEventListener('click', async () => {
        const result = await get('/api/catalog/duplicates');
        const groups = (result && result.groups) || [];
        const target = el('catalogMaintenance');
        if (!target) return;
        if (!groups.length) {
          target.textContent = 'No duplicate files found.';
          return;
        }
        const total = groups.reduce((sum, g) => sum + g.files.length, 0);
        target.innerHTML = `<strong>${groups.length} duplicate `
          + `group${groups.length === 1 ? '' : 's'}</strong> covering `
          + `${total} files.<br>`
          + groups.slice(0, 20).map((group) =>
            group.files.map((file) => file.relpath).join(' = ')).join('<br>');
      });
    }
  }

  /* ------------------------------------------------------ sidecar import */

  function bindSidecarImport() {
    const button = el('importSidecarsBtn');
    if (!button) return;
    button.addEventListener('click', async () => {
      const report = el('importReport');
      if (report) report.textContent = 'Reading sidecars…';
      try {
        const result = await post('/api/import/sidecars', {
          apply: { metadata: true, develop: false, crop: false },
        });
        if (report) {
          const ignored = Object.entries(result.ignored || {});
          report.innerHTML = `Read ${result.read} sidecars, applied `
            + `${result.applied}. ${result.missing} photos had none.`
            + (ignored.length
              ? `<br>Skipped: ${ignored.map(([k, n]) => `${k} (${n})`)
                .join(', ')}` : '');
        }
        if (ctx.onLibraryChanged) ctx.onLibraryChanged();
      } catch (error) {
        if (report) report.textContent = 'Sidecar import needs the catalog.';
      }
    });
  }

  /* ------------------------------------------------------ catalog import */

  function bindCatalogImport() {
    const open = el('importCatalogBtn'), dialog = el('importDialog');
    const pathInput = el('importPath'), summary = el('importSummary');
    const options = el('importOptions'), run = el('importRun');
    const preview = el('importPreview'), trial = el('importTrial');
    if (!open || !dialog) return;
    let inspected = '', previewed = '', busy = false, inspection = 0;
    const requestBody = () => ({path: pathInput.value.trim(), options: {
      metadata: el('impMetadata').checked, keywords: el('impKeywords').checked,
      collections: el('impCollections').checked, stacks: el('impStacks').checked,
      develop: el('impDevelop').checked, history: el('impHistory').checked,
      conflict: el('impConflict').value, referenceRoot: el('impReferenceRoot').value.trim(),
    }});
    function controls() {
      const ready = inspected && inspected === pathInput.value.trim();
      preview.disabled = trial.disabled = busy || !ready;
      run.disabled = busy || !ready || previewed !== JSON.stringify(requestBody());
      pathInput.disabled = busy;
      el('importChoose').disabled = busy;
      options.querySelectorAll('input, select').forEach(control => { control.disabled = busy; });
    }
    const show = visible => {
      dialog.setAttribute('aria-hidden', String(!visible));
      dialog.classList.toggle('on', visible);
    };
    open.addEventListener('click', () => show(true));
    el('importCancel').addEventListener('click', () => show(false));
    dialog.addEventListener('keydown', event => {
      event.stopPropagation();
      if (event.key === 'Escape') { event.preventDefault(); show(false); }
    });
    el('importChoose')?.addEventListener('click', () => {
      if (!sendNative('chooseCatalogFile', {})) toast('Paste the catalog file path to continue');
    });
    async function inspect() {
      const path = pathInput.value.trim(), ticket = ++inspection;
      inspected = previewed = '';
      controls();
      el('importCoverage').hidden = true;
      if (!path) return;
      summary.textContent = 'Reading catalog…';
      try {
        const result = await post('/api/import/catalog', {path, inspectOnly: true});
        if (ticket !== inspection || path !== pathInput.value.trim()) return;
        if (result.error) throw new Error(result.error);
        summary.textContent = `${result.images || 0} photos · ${result.collections || 0} collections. `
          + (result.warnings || []).join('; ');
        inspected = path;
        options.hidden = false;
      } catch (error) { if (ticket === inspection) summary.textContent = error.message; }
      controls();
    }
    pathInput.addEventListener('change', inspect);
    options.addEventListener('change', controls);
    function showReport(result) {
      const prefix = result.previewOnly ? 'Compatibility preview' : result.trial ? 'Trial variants created' : 'Import complete';
      summary.textContent = `${prefix}: ${result.matched || 0} matched, ${result.unmatched || 0} not found. `
        + `${result.collections || 0} collections; ${result.history || 0} history steps. `
        + (result.warnings || []).join('; ')
        + (result.beforeBackup ? ` Backup before import: ${result.beforeBackup}` : '');
      el('importCoverage').hidden = false;
      el('importCoverageRows').replaceChildren(...(result.photos || []).map(photo => {
        const row = document.createElement('p');
        row.textContent = `${photo.sourcePath || photo.sourceId} → ${photo.targetName || 'No match'} · ${photo.outcome}. `
          + `Mapped: ${(photo.mapped || []).join(', ') || 'none'}. `
          + (photo.skipped?.length ? `Skipped: ${photo.skipped.join(', ')}. ` : '')
          + (photo.reference ? `Reference: ${photo.reference.path || photo.reference.note}. ` : '')
          + (photo.appearance || '');
        return row;
      }));
      const link = el('importReportDownload');
      link.hidden = !result.reportId;
      if (result.reportId) link.href = `/api/import/report?id=${encodeURIComponent(result.reportId)}`;
    }
    async function begin(mode) {
      if (busy) return;
      const body = requestBody(), signature = JSON.stringify(body);
      if (mode === 'import' && signature !== previewed) return;
      body.previewOnly = mode === 'preview';
      body.options.trial = mode === 'trial';
      busy = true; controls(); summary.textContent = 'Preparing…';
      try {
        const started = await post('/api/import/catalog', body);
        if (started.error || !started.jobId) throw new Error(started.error || 'Could not start import');
        for (let attempt = 0; attempt < 7200; attempt++) {
          const record = await get(`/api/jobs/${started.jobId}`);
          if (record.error) throw new Error(record.error);
          if (record.state === 'failed') throw new Error(record.errors?.join('; ') || 'Import failed');
          if (record.state === 'done') {
            const result = record.result || {};
            if (mode === 'preview') previewed = signature;
            showReport(result);
            if (mode !== 'preview') {
              await refresh();
              if (ctx.onLibraryChanged) ctx.onLibraryChanged();
            }
            return;
          }
          const status = record.result || {};
          summary.textContent = `${status.stage || 'Working'}… ${status.done || 0}/${status.total || 0}`;
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        throw new Error('The import is still running. Check its job status before starting another import.');
      } catch (error) { summary.textContent = error.message; }
      finally { busy = false; controls(); }
    }
    preview.onclick = () => begin('preview');
    trial.onclick = () => begin('trial');
    run.onclick = () => begin('import');
    return {setPath(path) { if (busy) return; pathInput.value = path; inspect(); show(true); }};
  }

  /* -------------------------------------------------------------- ingest */

  function ingestRequest() {
    return {
      destination: el('ingestDest').value.trim(),
      folderTemplate: el('ingestFolderTemplate').value.trim(),
      filenameTemplate: el('ingestFilenameTemplate').value.trim(),
      custom: el('ingestCustom').value.trim(),
      startNumber: Number(el('ingestStart').value) || 1,
      backupDestination: el('ingestBackup').value.trim() || null,
      verify: el('ingestVerify').value,
      onDuplicate: el('ingestDuplicates').value,
    };
  }

  function bindIngest() {
    const dialog = el('ingestDialog');
    const open = el('ingestOpen');
    if (!dialog || !open) return;
    const show = (visible) => {
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
    };
    open.addEventListener('click', () => show(true));
    const cancel = el('ingestCancel');
    if (cancel) cancel.addEventListener('click', () => { show(false); });

    ['ingestChoose', 'ingestChooseDest', 'ingestChooseBackup'].forEach((id) => {
      const button = el(id);
      if (!button) return;
      button.addEventListener('click', () => {
        const field = { ingestChoose: 'ingestSource',
                        ingestChooseDest: 'ingestDest',
                        ingestChooseBackup: 'ingestBackup' }[id];
        if (!sendNative('chooseIngestFolder', { field })) {
          toast('Choosing a folder needs the desktop app; paste a path instead');
        }
      });
    });

    const scan = el('ingestScan');
    if (scan) {
      scan.addEventListener('click', async () => {
        const path = el('ingestSource').value.trim();
        if (!path) { toast('Choose a card first'); return; }
        el('ingestStatus').textContent = 'Scanning…';
        try {
          const result = await post('/api/ingest/scan',
                                    { path, request: ingestRequest() });
          ingestPlan = result.plan;
          const grid = el('ingestGrid');
          grid.innerHTML = (ingestPlan.items || []).slice(0, 200).map((item) => `
            <label class="ingest-cell">
              <input type="checkbox" checked data-source="${encodeURIComponent(item.source)}">
              <span class="ingest-cell-name">${escapeHTML(item.name)}</span>
              <span class="ingest-cell-dest">${escapeHTML(String(item.destination || '').split('/').slice(-2).join('/'))}</span>
            </label>`).join('');
          el('ingestStatus').textContent =
            `${ingestPlan.total} photos to copy`
            + (ingestPlan.duplicates
              ? `, ${ingestPlan.duplicates} already in the catalog` : '')
            + `, ${(ingestPlan.bytes / 1e9).toFixed(2)} GB`
            + ((ingestPlan.skipped || []).some((item) => item.reason === 'cloud-only')
              ? `. ${(ingestPlan.skipped || []).filter((item) => item.reason === 'cloud-only').length} cloud-only photos skipped; download them in Finder and scan again.` : '');
          el('ingestStart2').disabled = !ingestPlan.total;
          const first = (ingestPlan.items || [])[0];
          el('ingestExample').textContent = first
            ? `First file lands at ${first.destination}` : '';
        } catch (error) {
          el('ingestStatus').textContent = String(error.message || error);
        }
      });
    }

    const start = el('ingestStart2');
    if (start) {
      start.addEventListener('click', async () => {
        if (!ingestPlan) return;
        const checked = new Set(
          Array.from(el('ingestGrid').querySelectorAll('input:checked'))
            .map((input) => decodeURIComponent(input.dataset.source)));
        const plan = {
          ...ingestPlan,
          items: ingestPlan.items.filter((item) => checked.has(item.source)),
        };
        start.disabled = true;
        await post('/api/ingest', { plan, request: ingestRequest(),
                                    path: el('ingestSource').value.trim() });
        clearInterval(ingestPoll);
        ingestPoll = setInterval(async () => {
          const status = await get('/api/ingest/status');
          el('ingestStatus').textContent =
            `Copied ${status.copied}/${status.total}`
            + (status.errors && status.errors.length
              ? ` · ${status.errors.length} failed` : '');
          if (!status.running) {
            clearInterval(ingestPoll);
            start.disabled = false;
            await refresh();
            if (ctx.onLibraryChanged) ctx.onLibraryChanged();
            if (status.errors && status.errors.length) {
              el('ingestStatus').textContent +=
                ' — the originals on the card were not touched.';
            }
            notifyCompletion('Import complete',
              `${status.copied || 0} photo${status.copied === 1 ? '' : 's'} copied`
              + ((status.errors || []).length ? ` · ${status.errors.length} failed` : ''));
          }
        }, 600);
      });
    }
    return { show };
  }

  /* ------------------------------------------------------------- watches */

  function renderWatches(statuses = []) {
    const byId = new Map(statuses.map((status) => [status.id, status]));
    const list = el('watchList');
    if (list) {
      list.innerHTML = watches.map((watch) => {
        const status = byId.get(watch.id) || {};
        const detail = status.available === false
          ? 'Unavailable' : `${status.handled || 0} new`;
        return `<button class="source-row" data-watch-id="${escapeHTML(watch.id)}">`
          + `<span class="source-name">${escapeHTML(watch.name)}</span>`
          + `<span class="source-count">${detail}</span></button>`;
      }).join('');
    }
    const enabled = watches.filter((watch) => watch.enabled);
    const pill = el('watchPill');
    if (!pill) return;
    pill.hidden = !enabled.length;
    if (!enabled.length) return;
    const first = byId.get(enabled[0].id) || {};
    const handled = statuses.reduce(
      (sum, status) => sum + (status.handled || 0), 0);
    pill.textContent = `Watching ${enabled[0].name} · ${handled} new`;
    pill.classList.toggle('unavailable', first.available === false);
  }

  async function refreshWatchStatus({ arrivals = true } = {}) {
    if (!watches.some((watch) => watch.enabled)) return;
    const result = await get('/api/watch/status')
      .catch(() => ({ watches: [] }));
    const statuses = result.watches || [];
    for (const status of statuses) {
      const before = watchSeen.get(status.id);
      watchSeen.set(status.id, status.handled || 0);
      if (arrivals && before !== undefined && status.handled > before
          && ctx.onWatchArrival) {
        await ctx.onWatchArrival(status);
      }
    }
    renderWatches(statuses);
  }

  function startWatchPolling() {
    clearInterval(watchPoll);
    watchPoll = null;
    if (!watches.some((watch) => watch.enabled)) {
      renderWatches();
      return;
    }
    refreshWatchStatus({ arrivals: false });
    watchPoll = setInterval(refreshWatchStatus, 2000);
  }

  async function loadWatches() {
    const result = await get('/api/watch').catch(() => ({ watches: [] }));
    watches = result.watches || [];
    (result.status || []).forEach((status) => {
      watchSeen.set(status.id, status.handled || 0);
    });
    renderWatches(result.status || []);
    startWatchPolling();
  }

  function bindWatches() {
    const dialog = el('watchDialog');
    const open = el('watchOpen');
    if (!dialog || !open) return null;
    const show = (visible) => {
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
    };
    const syncMode = () => {
      el('watchDestRow').hidden = el('watchMode').value !== 'ingest';
    };
    const populatePreset = async (selected = '') => {
      const presets = await get('/api/presets').catch(() => []);
      const select = el('watchPreset');
      select.innerHTML = '<option value="">None</option>'
        + presets.map((preset) => `<option value="${escapeHTML(preset.id)}">`
          + `${escapeHTML(preset.name)}</option>`).join('');
      select.value = selected;
      select.dispatchEvent(new Event('change'));
    };
    const edit = (watch = null) => {
      const current = watch || {};
      el('watchId').value = current.id || '';
      el('watchName').value = current.name || '';
      el('watchPath').value = current.path || '';
      el('watchMode').value = current.mode || 'catalog';
      el('watchDest').value = current.request?.destination || '';
      el('watchRecursive').checked = !!current.recursive;
      el('watchFollow').checked = !!current.follow;
      el('watchDelete').hidden = !current.id;
      el('watchStatus').textContent =
        'The watched folder is never changed, moved, or emptied.';
      syncMode();
      populatePreset(current.presetId || '');
      show(true);
    };
    open.addEventListener('click', () => edit());
    el('watchPill')?.addEventListener('click', () => edit(watches[0]));
    el('watchList')?.addEventListener('click', (event) => {
      const row = event.target.closest('[data-watch-id]');
      if (row) {
        edit(watches.find((watch) => watch.id === row.dataset.watchId));
      }
    });
    el('watchCancel')?.addEventListener('click', () => show(false));
    el('watchMode')?.addEventListener('change', syncMode);
    [['watchChoose', 'watchPath'], ['watchChooseDest', 'watchDest']]
      .forEach(([buttonId, field]) => el(buttonId)?.addEventListener('click', () => {
        if (!sendNative('chooseIngestFolder', { field })) {
          toast('Choosing a folder needs the desktop app; paste a path instead');
        }
      }));
    el('watchSave')?.addEventListener('click', async () => {
      const mode = el('watchMode').value;
      const watch = {
        id: el('watchId').value || undefined,
        name: el('watchName').value.trim(), path: el('watchPath').value.trim(),
        enabled: true, recursive: el('watchRecursive').checked, mode,
        presetId: el('watchPreset').value, follow: el('watchFollow').checked,
        request: mode === 'ingest' ? {
          destination: el('watchDest').value.trim(),
          folderTemplate: '{yyyy}/{yyyy}-{mm}-{dd}',
          filenameTemplate: '{filename}', verify: 'hash', onDuplicate: 'skip',
        } : {},
      };
      try {
        const result = await post('/api/watch', { action: 'save', watch });
        watches = result.watches || [];
        show(false);
        startWatchPolling();
      } catch (error) {
        el('watchStatus').textContent = String(error.message || error);
      }
    });
    el('watchDelete')?.addEventListener('click', async () => {
      const result = await post('/api/watch', {
        action: 'delete', id: el('watchId').value,
      });
      watches = result.watches || [];
      show(false);
      startWatchPolling();
    });
    return { edit };
  }

  /* -------------------------------------------------------------- rename */

  function bindRename() {
    const dialog = el('renameDialog');
    if (!dialog) return null;
    let names = [];
    let returnFocus = null;
    let previewSequence = 0;
    const apply = el('renameApply');
    const show = (visible) => {
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
      if (visible) {
        returnFocus = document.activeElement;
        el('renameTemplate').focus();
        el('renameTemplate').select();
      } else {
        previewSequence++;
        returnFocus?.focus?.({ preventScroll: true });
        returnFocus = null;
      }
    };

    async function preview() {
      const request = ++previewSequence;
      apply.disabled = true;
      try {
        const result = await post('/api/photos/rename', {
          names,
          template: el('renameTemplate').value,
          custom: el('renameCustom').value,
          start: Number(el('renameStart').value) || 1,
          preview: true,
        });
        if (request !== previewSequence) return;
        const rows = (result.preview || []).map(row => {
          const line = document.createElement('span');
          line.textContent = `${row.sourcePath || row.from} → ${row.destinationPath || row.to}`;
          for (const companion of row.companions || []) {
            const detail = document.createElement('small');
            detail.textContent = `${companion.source} → ${companion.target || companion.action}${companion.sharedWith?.length ? " (shared metadata retained)" : ""}`;
            detail.style.display = 'block'; line.append(detail);
          }
          return line;
        });
        const policy = document.createElement('span');
        policy.textContent = result.error || result.collisionPolicy || '';
        el('renamePreview').replaceChildren(policy, ...rows);
        apply.disabled = Boolean(result.error);
      } catch (error) {
        if (request === previewSequence) toast('Could not preview the rename');
      }
    }

    dialog.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Escape') {
        event.preventDefault(); show(false); return;
      }
      if (event.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll(
        'button:not([disabled]), input:not([disabled])'
      )].filter((element) => !element.hidden && element.offsetParent !== null);
      if (!controls.length) return;
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    });
    ['renameTemplate', 'renameCustom', 'renameStart'].forEach((id) => {
      const input = el(id);
      if (input) input.addEventListener('input', preview);
    });
    const cancel = el('renameCancel');
    if (cancel) cancel.addEventListener('click', () => show(false));
    if (apply) {
      apply.addEventListener('click', async () => {
        apply.disabled = true;
        try {
          const result = await post('/api/photos/rename', {
            names,
            template: el('renameTemplate').value,
            custom: el('renameCustom').value,
            start: Number(el('renameStart').value) || 1,
          });
          show(false);
          toast(result.ok ? `Renamed ${result.renamed} photos`
            : `Rename stopped: ${result.error}`);
          if (ctx.onLibraryChanged) ctx.onLibraryChanged();
        } catch (error) {
          toast('Could not rename photos');
          apply.disabled = false;
        }
      });
    }
    return { open() {
      names = [...ctx.selection()];
      if (!names.length) { toast('Select photos first'); return; }
      show(true); preview();
    } };
  }

  bindSources();
  bindSidecarImport();
  const importDialog = bindCatalogImport();
  const ingest = bindIngest();
  bindWatches();
  const rename = bindRename();
  loadWatches();

  return {
    refresh,
    renderSources,
    openIngest: () => ingest && ingest.show(true),
    openRename: () => rename && rename.open(),
    setCatalogPath: (path) => importDialog && importDialog.setPath(path),
    setIngestField: (field, value) => { if (el(field)) el(field).value = value; },
    get catalog() { return catalog; },
  };
}
