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
  let importPoll = null;
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
    const open = el('importCatalogBtn');
    const dialog = el('importDialog');
    const pathInput = el('importPath');
    const summary = el('importSummary');
    const options = el('importOptions');
    const run = el('importRun');
    if (!open || !dialog) return;

    const show = (visible) => {
      dialog.setAttribute('aria-hidden', visible ? 'false' : 'true');
      dialog.classList.toggle('on', visible);
    };

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
      const path = pathInput.value.trim();
      if (!path) return;
      summary.textContent = 'Reading…';
      try {
        const result = await post('/api/import/catalog',
                                  { path, inspectOnly: true });
        if (result.error) { summary.textContent = result.error; return; }
        const missing = (result.roots || []).filter((r) => !r.exists);
        summary.innerHTML = `<strong>${result.images || 0} photos</strong>`
          + ` · ${result.keywords || 0} keywords`
          + ` · ${result.collections || 0} collections`
          + ` · ${result.stacks || 0} stacks<br>`
          + `${(result.roots || []).length} folder root(s)`
          + (missing.length
            ? `, <em>${missing.length} not found on this machine</em>` : '')
          + ((result.warnings || []).length
            ? `<br>Notes: ${result.warnings.join('; ')}` : '');
        options.hidden = false;
        run.disabled = false;
      } catch (error) {
        summary.textContent = 'That file could not be read as a catalog.';
      }
    }

    if (pathInput) pathInput.addEventListener('change', inspect);

    if (run) {
      run.addEventListener('click', async () => {
        run.disabled = true;
        const body = {
          path: pathInput.value.trim(),
          options: {
            metadata: el('impMetadata').checked,
            keywords: el('impKeywords').checked,
            collections: el('impCollections').checked,
            stacks: el('impStacks').checked,
            develop: el('impDevelop').checked,
            history: el('impHistory').checked,
            conflict: el('impConflict').value,
          },
        };
        await post('/api/import/catalog', body);
        summary.textContent = 'Importing…';
        clearInterval(importPoll);
        importPoll = setInterval(async () => {
          const status = await get('/api/import/status');
          if (status.running) {
            summary.textContent = `${status.stage || 'Working'}… `
              + `${status.done || 0}/${status.total || 0}`;
            return;
          }
          clearInterval(importPoll);
          run.disabled = false;
          if (status.error) {
            summary.textContent = `Import failed: ${status.error}`;
            return;
          }
          const result = status.result || {};
          summary.innerHTML = `<strong>Imported ${result.images || 0} photos.`
            + `</strong><br>${result.matched || 0} matched on disk, `
            + `${result.unmatched || 0} not found.<br>`
            + `${result.keywords || 0} keywords, `
            + `${result.collections || 0} collections, `
            + `${result.stacks || 0} stacks, ${result.history || 0} history steps.`
            + ((result.warnings || []).length
              ? `<br>Skipped: ${result.warnings.join('; ')}` : '');
          await refresh();
          if (ctx.onLibraryChanged) ctx.onLibraryChanged();
        }, 700);
      });
    }

    return { setPath(path) { pathInput.value = path; inspect(); show(true); } };
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
            + `, ${(ingestPlan.bytes / 1e9).toFixed(2)} GB`;
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
        el('renamePreview').innerHTML = (result.preview || [])
          .map((row) => `<span>${row.from} → <strong>${row.to}</strong></span>`)
          .join('') + (result.total > 3
            ? `<span class="muted">…and ${result.total - 3} more</span>` : '');
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
