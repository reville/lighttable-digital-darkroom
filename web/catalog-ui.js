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

  let catalogResultVersion = 0;
  function showCatalogResult(title, message) {
    const version = ++catalogResultVersion;
    el('catalogResultTitle').textContent = title;
    el('catalogResultBody').textContent = message;
    const dialog = el('catalogResultDialog');
    dialog.setAttribute('aria-hidden', 'false');
    dialog.classList.add('on');
    el('catalogResultClose').focus();
    return message => {
      if (version === catalogResultVersion) el('catalogResultBody').textContent = message;
    };
  }

  const resultDialog = el('catalogResultDialog');
  const closeResult = () => {
    resultDialog?.setAttribute('aria-hidden', 'true');
    resultDialog?.classList.remove('on');
    el('localLibraryMenuBtn')?.focus();
  };
  el('catalogResultClose')?.addEventListener('click', closeResult);
  resultDialog?.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); closeResult(); }
    if (event.key === 'Tab') { event.preventDefault(); el('catalogResultClose').focus(); }
  });

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
        backup.disabled = true;
        const report = showCatalogResult('Back up catalog', 'Creating backup…');
        try {
          const result = await post('/api/catalog/backup', {});
          if (result.error || !result.archive) throw new Error(result.error || 'Could not create backup');
          report(`Backed up to ${result.archive}`);
        } catch (error) { report(String(error.message || error)); }
        finally { backup.disabled = false; }
      });
    }

    const duplicates = el('catalogDuplicates');
    if (duplicates) {
      duplicates.addEventListener('click', async () => {
        duplicates.disabled = true;
        const report = showCatalogResult('Find duplicates', 'Looking for duplicate files…');
        try {
          const result = await get('/api/catalog/duplicates');
          if (result.error) throw new Error(result.error);
          const groups = result.groups || [];
          const total = groups.reduce((sum, group) => sum + group.files.length, 0);
          report(!groups.length ? 'No duplicate files found.'
            : `${groups.length} duplicate group${groups.length === 1 ? '' : 's'} covering ${total} files.\n\n`
              + groups.map(group => group.files.map(file => file.relpath).join(' = ')).join('\n'));
        } catch (error) { report(String(error.message || error)); }
        finally { duplicates.disabled = false; }
      });
    }
  }

  /* ------------------------------------------------------ sidecar import */

  function bindSidecarImport() {
    const button = el('importSidecarsBtn');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      const report = showCatalogResult('Import sidecars', 'Reading sidecars…');
      try {
        const result = await post('/api/import/sidecars', {
          apply: { metadata: true, develop: false, crop: false },
        });
        if (result.error) throw new Error(result.error);
        const ignored = Object.entries(result.ignored || {});
        report(`Read ${result.read} sidecars, applied `
          + `${result.applied}. ${result.missing} photos had none.`
          + (ignored.length ? `\nSkipped: ${ignored.map(([key, count]) => `${key} (${count})`).join(', ')}` : '')
          + (result.errors?.length ? `\nErrors: ${result.errors.map(error => error.error || error).join('; ')}` : ''));
        if (ctx.onLibraryChanged) ctx.onLibraryChanged();
      } catch (error) { report(String(error.message || error)); }
      finally { button.disabled = false; }
    });
  }

  /* ------------------------------------------------------ catalog import */

  function bindCatalogImport() {
    const open = el('importCatalogBtn'), dialog = el('importDialog');
    const pathInput = el('importPath'), summary = el('importSummary');
    const options = el('importOptions'), run = el('importRun');
    const preview = el('importPreview'), trial = el('importTrial');
    if (!open || !dialog) return;
    let inspected = '', previewed = '', busy = false, inspection = 0, inspectTimer = null;
    const requestBody = () => ({path: pathInput.value.trim(),
      addSources: el('impAddSources').checked, options: {
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

    const background = new Map();
    let returnFocus = null;
    const show = (visible) => {
      const wasVisible = dialog.classList.contains('on');
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
      if (visible && !wasVisible) {
        returnFocus = document.activeElement;
        for (const node of document.body.children) {
          if (node === dialog || node.tagName === 'SCRIPT') continue;
          background.set(node, node.inert);
          node.inert = true;
        }
        pathInput.focus();
      } else if (!visible && wasVisible) {
        for (const [node, inert] of background) node.inert = inert;
        background.clear();
        returnFocus?.focus?.();
        ctx.onCatalogImportClosed?.();
      }
    };
    dialog.addEventListener('keydown', (event) => {
      if (!dialog.classList.contains('on')) return;
      event.stopPropagation();
      if (event.key === 'Escape') {
        event.preventDefault(); show(false);
      } else if (event.key === 'Tab') {
        const targets = [...dialog.querySelectorAll('button, input, select')]
          .filter((node) => !node.disabled && node.getClientRects().length);
        const index = targets.indexOf(document.activeElement);
        if (event.shiftKey && index <= 0) {
          event.preventDefault(); targets.at(-1)?.focus();
        } else if (!event.shiftKey && (index < 0 || index === targets.length - 1)) {
          event.preventDefault(); targets[0]?.focus();
        }
      }
    });

    open.addEventListener('click', () => { show(true); });
    const cancel = el('importCancel');
    if (cancel) cancel.addEventListener('click', () => show(false));

    const choose = el('importChoose');
    if (choose) {
      choose.addEventListener('click', () => {
        if (!sendNative('chooseCatalogFile', {})) {
          toast('Choosing a file needs the desktop app; paste a path instead');
        }
      });
    }

    async function inspect() {
      clearTimeout(inspectTimer);
      if (busy) return;
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
    pathInput.addEventListener('input', () => {
      ++inspection; inspected = previewed = ''; options.hidden = true; controls();
      clearTimeout(inspectTimer);
      inspectTimer = setTimeout(inspect, 300);
    });
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
              ctx.onCatalogImportCompleted?.(result);
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
    const dialog = el('ingestDialog'), open = el('ingestOpen');
    if (!dialog || !open) return;
    const start = el('ingestStart2'), cancel = el('ingestCancel');
    const pageSize = 200, selected = new Set();
    let page = 0, busy = false, scanning = false, jobId = null, cancelling = false;
    const fields = ['ingestSource', 'ingestDest', 'ingestFolderTemplate',
      'ingestFilenameTemplate', 'ingestCustom', 'ingestStart', 'ingestBackup',
      'ingestVerify', 'ingestDuplicates'];
    const show = (visible) => {
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
    };
    const items = () => ingestPlan?.items || [];
    const chosen = () => items().filter(item => selected.has(item.source));
    function controls() {
      const locked = busy || scanning;
      for (const id of [...fields, 'ingestChoose', 'ingestChooseDest', 'ingestChooseBackup']) {
        el(id).disabled = locked;
      }
      el('ingestScan').disabled = locked || !el('ingestSource').value.trim()
        || !el('ingestDest').value.trim();
      start.disabled = locked || !selected.size || !ingestPlan;
      cancel.disabled = scanning || (busy && (!jobId || cancelling));
      el('ingestSelectAll').disabled = locked || selected.size === items().length;
      el('ingestSelectNone').disabled = locked || !selected.size;
      el('ingestPrevious').disabled = locked || !page;
      el('ingestNext').disabled = locked || (page + 1) * pageSize >= items().length;
      for (const input of el('ingestGrid').querySelectorAll('input')) input.disabled = locked;
    }
    function selectionStatus() {
      const selectedItems = chosen();
      const bytes = selectedItems.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
      const cloud = (ingestPlan?.skipped || []).filter(item => item.reason === 'cloud-only').length;
      el('ingestStatus').textContent = `${selectedItems.length} of ${items().length} photos selected`
        + `, ${(bytes / 1e9).toFixed(2)} GB`
        + (ingestPlan?.duplicates ? ` · ${ingestPlan.duplicates} already in the catalog` : '')
        + (cloud ? `. ${cloud} cloud-only photos skipped; download them in Finder and scan again.` : '');
      el('ingestExample').textContent = selectedItems[0]
        ? `First file lands at ${selectedItems[0].destination}` : '';
      controls();
    }
    function renderPage() {
      el('ingestSelection').hidden = !items().length;
      el('ingestPage').textContent = items().length
        ? `Page ${page + 1} of ${Math.ceil(items().length / pageSize)}` : '';
      el('ingestGrid').innerHTML = items().slice(page * pageSize, (page + 1) * pageSize)
        .map(item => `<label class="ingest-cell">`
          + `<input type="checkbox" ${selected.has(item.source) ? 'checked' : ''} data-source="${encodeURIComponent(item.source)}">`
          + `<span class="ingest-cell-name">${escapeHTML(item.name)}</span>`
          + `<span class="ingest-cell-dest">${escapeHTML(String(item.destination || '').split('/').slice(-2).join('/'))}</span></label>`).join('');
      controls();
    }
    function invalidate() {
      if (busy || scanning) return;
      ingestPlan = null; selected.clear(); page = 0;
      renderPage();
      el('ingestExample').textContent = '';
      el('ingestStatus').textContent = 'Scan the source to review photos and destinations.';
    }
    for (const id of fields) {
      el(id).addEventListener('input', invalidate);
      el(id).addEventListener('change', invalidate);
    }
    el('ingestGrid').addEventListener('change', event => {
      if (busy || scanning || !event.target.dataset.source) return;
      const source = decodeURIComponent(event.target.dataset.source);
      if (event.target.checked) selected.add(source); else selected.delete(source);
      selectionStatus();
    });
    el('ingestSelectAll').addEventListener('click', () => {
      if (busy || scanning) return;
      items().forEach(item => selected.add(item.source)); renderPage(); selectionStatus();
    });
    el('ingestSelectNone').addEventListener('click', () => {
      if (busy || scanning) return;
      selected.clear(); renderPage(); selectionStatus();
    });
    for (const [id, step] of [['ingestPrevious', -1], ['ingestNext', 1]]) {
      el(id).addEventListener('click', () => {
        if (busy || scanning) return;
        page = Math.max(0, Math.min(Math.ceil(items().length / pageSize) - 1, page + step));
        renderPage();
      });
    }
    open.addEventListener('click', () => show(true));
    cancel.addEventListener('click', async () => {
      if (!busy) { show(false); return; }
      if (!jobId || cancelling) return;
      cancelling = true; controls();
      el('ingestStatus').textContent = 'Stopping after the current file finishes copying and verification…';
      try {
        const result = await post(`/api/jobs/${jobId}/cancel`, {});
        if (result.error) throw new Error(result.error);
      } catch (error) {
        cancelling = false; controls();
        el('ingestStatus').textContent = `Could not cancel import: ${error.message || error}`;
      }
    });
    ['ingestChoose', 'ingestChooseDest', 'ingestChooseBackup'].forEach(id => {
      el(id).addEventListener('click', () => {
        const field = { ingestChoose: 'ingestSource', ingestChooseDest: 'ingestDest',
          ingestChooseBackup: 'ingestBackup' }[id];
        if (!sendNative('chooseIngestFolder', { field })) {
          toast('Choosing a folder needs the desktop app; paste a path instead');
        }
      });
    });
    el('ingestScan').addEventListener('click', async () => {
      if (busy || scanning) return;
      const path = el('ingestSource').value.trim();
      if (!path || !el('ingestDest').value.trim()) {
        el('ingestStatus').textContent = 'Choose a source and destination first.'; return;
      }
      invalidate(); scanning = true; controls();
      el('ingestStatus').textContent = 'Scanning…';
      try {
        const result = await post('/api/ingest/scan', { path, request: ingestRequest() });
        if (result.error || !Array.isArray(result.plan?.items)) {
          throw new Error(result.error || 'Could not scan the source');
        }
        ingestPlan = result.plan;
        items().forEach(item => selected.add(item.source));
        renderPage(); selectionStatus();
      } catch (error) {
        el('ingestStatus').textContent = String(error.message || error);
      } finally { scanning = false; controls(); }
    });
    start.addEventListener('click', async () => {
      if (busy || scanning || !ingestPlan || !selected.size) return;
      const selectedItems = chosen();
      if (!selectedItems.length) return;
      const plan = { ...ingestPlan, items: selectedItems, total: selectedItems.length,
        bytes: selectedItems.reduce((sum, item) => sum + (Number(item.size) || 0), 0) };
      busy = true; cancelling = false; controls();
      el('ingestStatus').textContent = 'Starting import…';
      try {
        const result = await post('/api/ingest', { plan, request: ingestRequest(),
          path: el('ingestSource').value.trim() });
        if (result.error || !result.jobId) throw new Error(result.error || 'Could not start import');
        jobId = result.jobId; controls();
      } catch (error) {
        busy = false; controls();
        el('ingestStatus').textContent = String(error.message || error); return;
      }
      clearTimeout(ingestPoll);
      let attempts = 0;
      const poll = async () => {
        try {
          const record = await get(`/api/jobs/${jobId}`);
          if (record.error) throw new Error(record.error);
          const status = record.result || {};
          const terminal = ['done', 'failed', 'cancelled'].includes(record.state);
          el('ingestStatus').textContent =
            (record.state === 'cancelled' ? 'Import cancelled. ' : cancelling && !terminal ? 'Stopping after the current file… ' : '')
            + `Copied ${status.copied || 0}/${record.total}`
            + (record.errors?.length ? ` · ${record.errors.length} failed` : '');
          if (terminal) {
            busy = false; jobId = null; cancelling = false;
            ingestPlan = null; selected.clear(); page = 0; renderPage();
            el('ingestExample').textContent = '';
            if (record.state !== 'done') el('ingestStatus').textContent += ' — the originals on the card were not touched.';
            await refresh();
            if (ctx.onLibraryChanged) ctx.onLibraryChanged();
            notifyCompletion(record.state === 'cancelled' ? 'Import cancelled' : record.state === 'failed' ? 'Import finished with errors' : 'Import complete',
              `${status.copied || 0} photo${status.copied === 1 ? '' : 's'} copied`
              + (record.errors?.length ? ` · ${record.errors.length} failed` : ''));
            return;
          }
        } catch (error) {
          el('ingestStatus').textContent = `Could not read import progress: ${error.message || error}`;
        }
        if (++attempts < 7200) ingestPoll = setTimeout(poll, 1000);
        else el('ingestStatus').textContent = 'Import is still running. Use Jobs to check its progress or cancel it.';
      };
      await poll();
    });
    controls();
    return { show, setField(field, value) {
      if (busy || scanning) return;
      if (fields.includes(field)) { el(field).value = value; invalidate(); }
    } };
  }

  /* ------------------------------------------------------------- watches */

  function renderWatches(statuses = []) {
    const byId = new Map(statuses.map((status) => [status.id, status]));
    const existing = el('watchExisting');
    if (existing) {
      const selectedId = el('watchId').value;
      el('watchExistingRow').hidden = !watches.length;
      const options = '<option value="">New watched folder</option>'
        + watches.map(watch => `<option value="${escapeHTML(watch.id)}">${escapeHTML(watch.name)}${watch.enabled ? '' : ' · Paused'}</option>`).join('');
      if (existing.innerHTML !== options) existing.innerHTML = options;
      if (existing.value !== selectedId) existing.value = selectedId;
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
    let saving = false;
    const syncMode = () => {
      const needsDestination = el('watchMode').value === 'ingest';
      el('watchDestRow').hidden = !needsDestination;
      el('watchSave').disabled = saving || !el('watchPath').value.trim()
        || (needsDestination && !el('watchDest').value.trim());
      el('watchDelete').disabled = saving;
      for (const id of ['watchExisting', 'watchName', 'watchPath', 'watchMode', 'watchDest', 'watchPreset', 'watchRecursive', 'watchFollow', 'watchChoose', 'watchChooseDest', 'watchCancel']) {
        el(id).disabled = saving;
      }
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
      if (saving) return;
      const current = watch || {};
      el('watchId').value = current.id || '';
      el('watchExisting').value = current.id || '';
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
    el('watchPill')?.addEventListener('click', () => edit(watches.find(watch => watch.enabled)));
    el('watchExisting')?.addEventListener('change', () => edit(watches.find(watch => watch.id === el('watchExisting').value)));
    for (const id of ['watchPath', 'watchDest']) {
      el(id).addEventListener('input', syncMode);
      el(id).addEventListener('change', syncMode);
    }
    el('watchCancel')?.addEventListener('click', () => show(false));
    el('watchMode')?.addEventListener('change', syncMode);
    [['watchChoose', 'watchPath'], ['watchChooseDest', 'watchDest']]
      .forEach(([buttonId, field]) => el(buttonId)?.addEventListener('click', () => {
        if (!sendNative('chooseIngestFolder', { field })) {
          toast('Choosing a folder needs the desktop app; paste a path instead');
        }
      }));
    el('watchSave')?.addEventListener('click', async () => {
      if (saving) return;
      const mode = el('watchMode').value;
      if (!el('watchPath').value.trim() || (mode === 'ingest' && !el('watchDest').value.trim())) {
        el('watchStatus').textContent = mode === 'ingest'
          ? 'Choose a watched folder and library destination first.' : 'Choose a folder to watch first.';
        syncMode(); return;
      }
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
      saving = true; syncMode();
      try {
        const result = await post('/api/watch', { action: 'save', watch });
        if (result.error || !result.ok) throw new Error(result.error || 'Could not save watched folder');
        watches = result.watches || [];
        show(false);
        startWatchPolling();
      } catch (error) {
        el('watchStatus').textContent = String(error.message || error);
      } finally { saving = false; syncMode(); }
    });
    el('watchDelete')?.addEventListener('click', async () => {
      if (saving || !el('watchId').value) return;
      saving = true; syncMode();
      try {
        const result = await post('/api/watch', { action: 'delete', id: el('watchId').value });
        if (result.error || !result.ok) throw new Error(result.error || 'Could not delete watched folder');
        watches = result.watches || [];
        show(false); startWatchPolling();
      } catch (error) { el('watchStatus').textContent = String(error.message || error); }
      finally { saving = false; syncMode(); }
    });
    return { edit, setField(field, value) {
      if (saving || !['watchPath', 'watchDest'].includes(field)) return;
      el(field).value = value; syncMode();
    } };
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
  const watchDialog = bindWatches();
  const rename = bindRename();
  loadWatches();

  return {
    refresh,
    renderSources,
    openIngest: () => ingest && ingest.show(true),
    openRename: () => rename && rename.open(),
    setCatalogPath: (path) => importDialog && importDialog.setPath(path),
    setIngestField: (field, value) => {
      if (field.startsWith('watch')) watchDialog?.setField(field, value);
      else ingest?.setField(field, value);
    },
    get catalog() { return catalog; },
  };
}
