const SOURCE_LABELS = {
  lighttable: 'LightTable', lightroom: 'Lightroom / Camera Raw',
  'capture-one': 'Capture One',
};

/** One preview at a time, including when an old request ignores cancellation. */
export function createPresetPreviewQueue({ render, onResult, onError }) {
  let generation = 0, pending = [], running = false, controller;
  async function drain() {
    if (running) return;
    running = true;
    try {
      while (pending.length) {
        const item = pending.shift();
        const request = generation, signal = controller.signal;
        try {
          const result = await render(item, { signal });
          if (request === generation && !signal.aborted) onResult(item, result);
        } catch (error) {
          if (request === generation && !signal.aborted) onError?.(item, error);
        }
      }
    } finally {
      running = false;
    }
  }
  return {
    replace(items) {
      generation++;
      controller?.abort();
      controller = new AbortController();
      pending = [...items];
      void drain();
    },
    cancel() {
      generation++;
      controller?.abort();
      pending = [];
    },
  };
}

export const presetKey = (preset) => preset.id || preset.name;

/** Upgrade names only when they match known presets; keep IDs for offline entries. */
export function migratePresetFavorites(favorites, presets) {
  return [...new Set(favorites.flatMap((value) => {
    if (presets.some((preset) => preset.id === value)) return [value];
    const matches = presets.filter((preset) => preset.name === value);
    return matches.length ? matches.map(presetKey) : [value];
  }))];
}

export function filterPresets(presets, {
  query = '', favorites = [], favoritesOnly = false, collection, tag = '', hidden = [], showHidden = false,
} = {}) {
  const text = query.trim().toLocaleLowerCase();
  const favoriteIds = new Set(favorites), hiddenIds = new Set(hidden);
  return presets.filter((preset) => {
    if (preset.collection === 'builtin' && !showHidden && hiddenIds.has(presetKey(preset))) return false;
    if (collection && (preset.collection || 'yours') !== collection) return false;
    if (favoritesOnly && !favoriteIds.has(presetKey(preset))) return false;
    if (tag && !(preset.tags || []).includes(tag)) return false;
    const source = SOURCE_LABELS[preset.source] || preset.source || 'LightTable';
    return !text || [preset.name, source, preset.presetType, preset.description,
      preset.author?.name, ...(preset.tags || [])].join(' ').toLocaleLowerCase().includes(text);
  });
}

/** Only use remote URLs as data, never executable links or markup. */
export function presetWebURL(value) {
  try { const url = new URL(value); return url.protocol === 'https:' ? url.href : null; }
  catch { return null; }
}

/** Calendar dates have no timezone; timestamps retain the viewer's local date. */
export function formatPresetCatalogDate(value, locale) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return date.toLocaleDateString(locale, dateOnly ? { timeZone: 'UTC' } : undefined);
}

/** Image previews are read-only. Selection, installation and application are separate. */
export function createPresetBrowser({
  container, getPresets, getPhoto, getSelectedName = () => '',
  onSelect = () => {}, onApply, canApply = () => true,
  getFavorites = () => [], onFavoritesChange = () => {},
  getHidden = () => [], onHiddenChange = () => {},
  getPreview, onManage, getCommunity = () => ({}), loadCommunity,
  getRecipe = async (preset) => preset, onInstall, onSubmit, onDuplicate,
  getSubmission, onDownloadExample,
  canUndo = () => false, onUndo = () => {},
}) {
  let active = false, destroyed = false, loading = false, page = 0, favoritesOnly = false;
  let collection = 'builtin', selected = null, snapshot, searchTimer, showHidden = false;
  let communityRequested = false, communityLoading = false, detailGeneration = 0;
  let communityPromise = null, detailController = null;
  const pageSize = 6, urls = new Set(), cards = new Map();
  const root = document.createElement('div');
  root.className = 'preset-browser';
  root.innerHTML = `<div class="preset-browser-tabs" role="tablist" aria-label="Preset collection">
      <button type="button" role="tab" data-collection="builtin" aria-selected="true">Built-in</button>
      <button type="button" role="tab" data-collection="yours" aria-selected="false">Yours</button>
      <button type="button" role="tab" data-collection="community" aria-selected="false">Community</button></div>
    <label class="preset-browser-search"><span class="sr-only">Find a preset</span><input type="search" placeholder="Search presets" autocomplete="off"></label>
    <div class="preset-browser-toolbar"><button type="button" data-filter="favorites" aria-pressed="false">☆ Favorites</button><button type="button" data-filter="hidden" aria-pressed="false">Show hidden</button><button type="button" data-action="manage">Manage…</button></div>
    <div class="preset-browser-community-controls" hidden><label>Collection<select data-order><option value="featured">Featured</option><option value="new">New</option><option value="all">All</option></select></label><label>Subject<select data-tag><option value="">All subjects</option></select></label><button type="button" data-action="refresh" aria-label="Refresh community presets">Refresh</button></div>
    <p class="preset-browser-network" role="status" hidden></p>
    <p class="preset-browser-status" role="status"></p>
    <section class="preset-browser-detail" aria-label="Selected preset" hidden></section>
    <div class="preset-browser-grid" aria-label="Preset previews"></div>
    <div class="preset-browser-pages"><button type="button" data-page="previous" aria-label="Previous presets">Previous</button><span></span><button type="button" data-page="next" aria-label="Next presets">Next</button></div>`;
  container.append(root);
  const search = root.querySelector('input');
  const status = root.querySelector('.preset-browser-status');
  const network = root.querySelector('.preset-browser-network');
  const grid = root.querySelector('.preset-browser-grid');
  const detail = root.querySelector('.preset-browser-detail');
  const pages = root.querySelector('.preset-browser-pages');
  const previous = root.querySelector('[data-page="previous"]');
  const next = root.querySelector('[data-page="next"]');
  const favoriteFilter = root.querySelector('[data-filter="favorites"]');
  const hiddenFilter = root.querySelector('[data-filter="hidden"]');
  const order = root.querySelector('[data-order]'), tag = root.querySelector('[data-tag]');
  const refresh = root.querySelector('[data-action="refresh"]');
  const tabs = [...root.querySelectorAll('[data-collection]')];
  root.querySelector('[data-action="manage"]').onclick = onManage;
  const queue = createPresetPreviewQueue({
    render: (item, options) => getPreview(item.preset, item.photo, { ...options, width: item.width || 320 }),
    onResult: ({ image, previewStatus, after, download }, value) => {
      if (download) { void download(value); return; }
      if (!value) { previewStatus.textContent = 'Preview unavailable'; return; }
      const url = value instanceof Blob ? URL.createObjectURL(value) : value;
      if (value instanceof Blob) urls.add(url);
      image.src = url; image.hidden = false; previewStatus.hidden = true;
      after?.();
    },
    onError: ({ previewStatus, onError }) => { previewStatus.hidden = false; previewStatus.textContent = 'Preview unavailable. Try again.'; onError?.(); },
  });
  function cancel() {
    clearTimeout(searchTimer); queue.cancel();
    detailGeneration++; detailController?.abort();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
  }
  function select(id = getSelectedName()) {
    for (const [key, button] of cards) button.setAttribute('aria-pressed', String(key === id));
  }
  function updateFavoriteButton(button, preset) {
    const favorite = getFavorites().includes(presetKey(preset));
    button.textContent = favorite ? '★' : '☆';
    button.setAttribute('aria-pressed', String(favorite));
    button.setAttribute('aria-label', `${favorite ? 'Remove' : 'Add'} ${preset.name} ${favorite ? 'from' : 'to'} favorites`);
    button.title = favorite ? 'Remove from favorites' : 'Add to favorites';
  }
  function toggleFavorite(preset) {
    const key = presetKey(preset), ids = [...getFavorites()];
    onFavoritesChange(ids.includes(key) ? ids.filter((id) => id !== key) : [...ids, key]);
    render();
  }
  function element(tagName, className, text) {
    const el = document.createElement(tagName); el.className = className;
    if (text) el.textContent = text;
    return el;
  }
  function link(label, url) {
    const href = presetWebURL(url);
    if (!href) return null;
    const el = element('a', 'preset-browser-link', label);
    el.href = href; el.target = '_blank'; el.rel = 'noopener noreferrer';
    return el;
  }
  function button(label, action, className = '') {
    const el = element('button', className, label); el.type = 'button'; el.onclick = action;
    return el;
  }
  async function ensureCommunity(force = false) {
    if (!loadCommunity) return;
    if (communityLoading) return communityPromise;
    communityLoading = true; communityRequested = true; render();
    communityPromise = (async () => {
      try { await loadCommunity(force); }
      catch (error) { network.textContent = error.message || 'Community is unavailable'; }
      finally { communityLoading = false; render(); }
    })();
    return communityPromise;
  }
  function photoUnchanged(photo) {
    return photo && JSON.stringify(getPhoto()) === JSON.stringify(photo);
  }
  function showDetail(preset) {
    selected = preset; onSelect(preset); render();
    detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  async function renderDetail(preset, photo) {
    const request = ++detailGeneration;
    detailController = new AbortController();
    const signal = detailController.signal;
    const valid = () => request === detailGeneration && !signal.aborted && active;
    detail.hidden = false; detail.replaceChildren();
    const heading = element('div', 'preset-browser-detail-heading');
    heading.append(element('strong', '', preset.name), button('×', () => { selected = null; render(); }, 'preset-browser-close'));
    heading.lastChild.setAttribute('aria-label', 'Close preset details');
    const credit = element('p', 'preset-browser-credit');
    credit.append(link(preset.author?.name || 'LightTable', preset.author?.url) || document.createTextNode(preset.author?.name || SOURCE_LABELS[preset.source] || 'LightTable'));
    const version = preset.version ? `Version ${preset.version}` : '';
    if (version) credit.append(document.createTextNode(` · ${version}`));
    const description = element('p', 'preset-browser-description', preset.description || 'Preview these adjustments on your photo before applying.');
    const mode = preset.filmMode || (preset.includeFilm ? 'on' : 'preserve');
    const behavior = element('p', 'preset-browser-hint', `Film ${mode === 'preserve' ? 'unchanged' : mode}. ${preset.scope === 'look' ? 'Exposure, white balance and photo corrections are kept.' : 'Applies the included adjustments.'}`);
    detail.append(heading, credit, description, behavior);
    const examples = (preset.previews || []).filter((example) => presetWebURL(example.after));
    if (examples.length) {
      const figure = element('figure', 'preset-browser-example');
      const img = element('img', ''); img.loading = 'lazy';
      img.onerror = () => { figure.hidden = true; };
      const label = element('figcaption', '');
      const controls = element('div', 'preset-browser-compare');
      const showExample = (example) => {
        figure.hidden = false;
        img.alt = example.label || `${preset.name} example`;
        img.src = presetWebURL(example.after);
        label.textContent = example.label || 'Creator example';
        if (example.credit?.name) {
          label.append(document.createTextNode(' · Photo: '));
          label.append(link(example.credit.name, example.credit.url) || document.createTextNode(example.credit.name));
          if (example.credit.license) label.append(document.createTextNode(` · ${example.credit.license}`));
        }
        controls.replaceChildren();
        const beforeURL = presetWebURL(example.before);
        if (beforeURL) {
          controls.append(button('Before', () => { img.src = beforeURL; }), button('With preset', () => { img.src = presetWebURL(example.after); }));
        }
      };
      figure.append(img, label, controls);
      if (examples.length > 1) {
        const chooser = element('select', 'preset-browser-example-choice');
        chooser.setAttribute('aria-label', 'Example photo');
        examples.forEach((example, index) => chooser.add(new Option(example.label || `Example ${index + 1}`, String(index))));
        chooser.onchange = () => showExample(examples[Number(chooser.value)]);
        figure.append(chooser);
      }
      showExample(examples[0]); detail.append(figure);
    }
    const preview = element('div', 'preset-browser-try'); preview.hidden = true;
    const previewImage = element('img', ''); previewImage.alt = 'Preset preview on your photo'; previewImage.hidden = true;
    const previewStatus = element('span', 'preset-browser-preview-status');
    preview.append(previewImage, previewStatus); detail.append(preview);
    const compare = element('div', 'preset-browser-compare'); compare.hidden = true; detail.append(compare);
    const actions = element('div', 'preset-browser-detail-actions');
    const message = element('p', 'preset-browser-status'); message.setAttribute('role', 'status');
    let recipe = null, prepareSubmission = null;
    const tryButton = button('Try on this photo', async () => {
      if (!photoUnchanged(photo)) { render(); return; }
      queue.cancel(); preview.hidden = false; previewImage.hidden = true;
      previewStatus.hidden = false; previewStatus.textContent = 'Rendering on this computer…';
      compare.hidden = true; compare.replaceChildren();
      const beforeImage = element('img', ''), beforeStatus = element('span', '');
      queue.replace([
        { preset: recipe, photo, image: previewImage, previewStatus, width: 960, after() {
          if (!valid()) return;
          const afterURL = previewImage.src;
          message.textContent = 'Preview only. Your edit has not changed.';
          compare.hidden = false;
          const before = button('Before', () => { if (beforeImage.src) previewImage.src = beforeImage.src; });
          before.disabled = !beforeImage.src;
          beforeImage.onload = () => { before.disabled = false; };
          compare.append(before, button('With preset', () => { previewImage.src = afterURL; }));
        } },
        { preset: null, photo, image: beforeImage, previewStatus: beforeStatus, width: 960 },
      ]);
    });
    const apply = button('Apply', async () => {
      if (!photoUnchanged(photo)) { render(); return; }
      try {
        await onApply(recipe, photo);
        render();
      } catch (error) { message.textContent = error.message || 'Could not apply preset'; }
    }, 'primary-btn');
    tryButton.disabled = true; apply.disabled = true;
    const undoButton = button('Undo', () => { onUndo(); render(); });
    undoButton.disabled = !canUndo();
    actions.append(tryButton, apply, undoButton);
    if (preset.collection === 'community' && onInstall) {
      const installed = getPresets().find((item) => item.community?.id === preset.id);
      const upToDate = installed?.community?.version === preset.version;
      const install = button(preset.compatible === false ? 'Update LightTable' : upToDate ? 'Added to Yours' : installed ? 'Update in Yours' : 'Add to Yours', async () => {
        install.disabled = true;
        try { await onInstall(preset); if (valid()) render(); }
        catch (error) { if (valid()) { install.disabled = false; message.textContent = error.message || 'Could not install preset'; } }
      });
      install.disabled = upToDate || preset.compatible === false; actions.append(install);
    }
    if (preset.collection === 'builtin' && onDuplicate) {
      const duplicate = button('Save a copy…', async () => {
        duplicate.disabled = true;
        try { await onDuplicate(preset); }
        catch (error) { if (valid()) message.textContent = error.message || 'Could not save a copy'; }
        finally { duplicate.disabled = false; }
      });
      actions.append(duplicate);
    }
    if (preset.collection === 'builtin') {
      const isHidden = getHidden().includes(presetKey(preset));
      actions.append(button(isHidden ? 'Restore to Built-in' : 'Hide preset', () => {
        const ids = getHidden(), id = presetKey(preset);
        onHiddenChange(isHidden ? ids.filter((value) => value !== id) : [...ids, id]);
        if (!isHidden) selected = null;
        render();
      }));
    }
    detail.append(actions, message);
    const more = element('div', 'preset-browser-detail-links');
    const pageLink = link('Preset page ↗', preset.pageUrl);
    if (pageLink) more.append(pageLink);
    if (preset.license) more.append(element('span', 'preset-browser-license', preset.license));
    if (preset.parentId) {
      const parentURL = /^[a-z0-9][a-z0-9-]{0,39}\/[a-z0-9][a-z0-9-]{0,59}$/.test(preset.parentId)
        ? `https://lighttable.app/presets/${preset.parentId}/` : null;
      more.append(link(`From ${preset.parentId}`, parentURL) || element('span', 'preset-browser-license', `From ${preset.parentId}`));
    }
    if (preset.collection !== 'community' && preset.collection !== 'builtin' && onSubmit) {
      const submission = element('section', 'preset-browser-submission');
      submission.append(element('strong', '', 'Community submission'));
      const scope = element('div', 'preset-browser-submission-scope');
      const submissionStatus = element('p', 'preset-browser-status', 'Preparing the included-settings summary…');
      submissionStatus.setAttribute('role', 'status');
      const rights = element('p', 'preset-browser-hint', 'Only share photos you have permission to publish');
      const submit = button('Prepare community submission…', async () => {
        submit.disabled = true;
        submissionStatus.textContent = 'Preparing the preset and three licensed example pairs…';
        try {
          const result = await onSubmit(preset);
          if (!valid()) return;
          submissionStatus.textContent = 'Submission package prepared. Review its examples and rights notes, then complete the submission form.';
          const submissionLink = link('Open submission form ↗', result.submissionUrl);
          if (submissionLink) submission.append(submissionLink);
        } catch (error) { if (valid()) submissionStatus.textContent = error.message || 'Could not prepare submission'; }
        finally { if (valid()) submit.disabled = false; }
      });
      submit.disabled = true;
      const example = button('Download example on this photo', () => {
        if (!photoUnchanged(photo)) { render(); return; }
        example.disabled = true; tryButton.disabled = true;
        submissionStatus.textContent = 'Rendering a 960-pixel JPEG on this computer…';
        queue.replace([{ preset: sharedRecipe, photo, width: 960, previewStatus: submissionStatus,
          onError() { example.disabled = false; tryButton.disabled = !photo?.name || !canApply(recipe); },
          async download(value) {
            if (!valid()) return;
            try {
              await onDownloadExample(preset, value);
              if (valid()) submissionStatus.textContent = 'Example download prepared. Your edit has not changed.';
            } catch (error) { if (valid()) submissionStatus.textContent = error.message || 'Could not save the example'; }
            finally { if (valid()) { example.disabled = false; tryButton.disabled = !photo?.name || !canApply(recipe); } }
          },
        }]);
      });
      example.disabled = true;
      let sharedRecipe = null;
      prepareSubmission = async () => {
        try {
          const draft = await getSubmission(preset, { signal });
          if (!valid()) return;
          sharedRecipe = JSON.parse(draft.content).presets?.[0];
          if (!sharedRecipe || !Array.isArray(sharedRecipe.includedGrade) || !Array.isArray(sharedRecipe.includedFilm)) throw new Error('The submission scope is unavailable');
          const behavior = sharedRecipe.filmMode === 'preserve' ? 'unchanged' : sharedRecipe.filmMode;
          scope.replaceChildren(element('p', '', `Film ${behavior}.`));
          const fields = element('dl', '');
          for (const [label, keys] of [['Develop settings', sharedRecipe.includedGrade], ['Film settings', sharedRecipe.includedFilm]]) {
            fields.append(element('dt', '', label), element('dd', '', keys.length ? keys.join(', ') : 'None'));
          }
          scope.append(fields, element('p', 'preset-browser-hint', 'Keeps Edit Exposure, Temp/Tint, capture white balance, RAW settings, film format, crop, geometry, masks, Remove, lens corrections, sharpening and noise reduction.'));
          submit.disabled = false;
          example.disabled = !photo?.name || !getPreview || !onDownloadExample;
          submissionStatus.textContent = 'The package includes this recipe, three licensed example pairs, and rights notes. Nothing is uploaded.';
        } catch (error) { if (valid()) submissionStatus.textContent = error.message || 'Could not inspect the submission settings'; }
      };
      submission.append(scope, submissionStatus, submit, example, rights);
      detail.append(submission);
    }
    detail.append(more);
    if (preset.compatible === false) {
      message.textContent = 'Update LightTable to use this preset.';
      return;
    }
    if (preset.collection === 'community') message.textContent = 'Loading recipe…';
    try {
      recipe = await getRecipe(preset, { signal });
      if (!valid()) return;
      const compatible = canApply(recipe);
      tryButton.disabled = !photo?.name || !compatible || !getPreview;
      apply.disabled = !photo?.name || !compatible;
      message.textContent = !compatible ? 'No compatible adjustments.' : !photo?.name ? 'Select a photo to try this preset.' : '';
      if (prepareSubmission) await prepareSubmission();
    } catch (error) {
      if (valid()) message.textContent = error.message || 'Recipe unavailable. Refresh the community catalog and try again.';
    }
  }
  function render() {
    cancel();
    if (!active || destroyed) return;
    snapshot = getPhoto(); snapshot = snapshot ? structuredClone(snapshot) : null;
    const favorites = [...getFavorites()];
    const remote = getCommunity() || {};
    const inventory = collection === 'community' ? (remote.presets || []).map((preset) => ({ ...preset, collection: 'community' })) : getPresets() || [];
    const isCommunity = collection === 'community';
    const hiddenIds = getHidden();
    hiddenFilter.hidden = collection !== 'builtin';
    hiddenFilter.disabled = !hiddenIds.length;
    hiddenFilter.textContent = hiddenIds.length ? `Show hidden (${hiddenIds.length})` : 'Show hidden';
    hiddenFilter.setAttribute('aria-pressed', String(showHidden));
    root.querySelector('.preset-browser-community-controls').hidden = !isCommunity;
    network.hidden = !isCommunity;
    refresh.disabled = communityLoading;
    network.textContent = communityLoading ? 'Refreshing community…' : remote.offline
      ? (remote.presets?.length ? 'Offline · showing saved catalog' : 'Community is offline. Built-in and saved presets remain available.')
      : remote.error ? remote.error : remote.updatedAt ? `Catalog updated ${formatPresetCatalogDate(remote.updatedAt)}` : '';
    const keptTag = tag.value;
    tag.replaceChildren(new Option('All subjects', ''));
    for (const value of [...new Set(inventory.flatMap((preset) => preset.tags || []))].sort()) tag.add(new Option(value, value));
    tag.value = keptTag;
    if (tag.selectedIndex < 0) tag.value = '';
    let filtered = filterPresets(inventory, { query: search.value, favorites, favoritesOnly,
      collection: isCommunity ? null : collection, tag: isCommunity ? tag.value : '', hidden: hiddenIds, showHidden });
    if (isCommunity && order.value === 'featured') filtered = filtered.filter((preset) => preset.featured);
    if (isCommunity && order.value === 'new') filtered.sort((a, b) => String(b.publishedAt || b.updatedAt || '').localeCompare(String(a.publishedAt || a.updatedAt || '')));
    page = Math.min(page, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
    const shown = filtered.slice(page * pageSize, (page + 1) * pageSize);
    previous.disabled = page === 0; next.disabled = (page + 1) * pageSize >= filtered.length;
    pages.hidden = filtered.length <= pageSize;
    pages.querySelector('span').textContent = `${page * pageSize + 1}–${page * pageSize + shown.length} of ${filtered.length}`;
    tabs.forEach((tab) => { const current = tab.dataset.collection === collection; tab.setAttribute('aria-selected', String(current)); tab.tabIndex = current ? 0 : -1; });
    favoriteFilter.setAttribute('aria-pressed', String(favoritesOnly));
    const busy = !isCommunity && loading;
    status.textContent = busy ? 'Loading presets…' : !filtered.length
      ? favoritesOnly ? 'No favorites here yet. Star a preset to save it.'
        : search.value || tag.value ? 'No matching presets.'
        : collection === 'yours' ? 'Save your current look, import presets, or add one from Community.'
        : communityLoading ? '' : 'No presets in this collection yet.'
      : `${filtered.length} preset${filtered.length === 1 ? '' : 's'} · select one to try`;
    grid.replaceChildren(); cards.clear(); detail.hidden = !selected;
    if (selected) {
      // Refresh an installed listing after updates while retaining remote identity.
      selected = inventory.find((preset) => presetKey(preset) === presetKey(selected)) || selected;
      void renderDetail(selected, snapshot);
    }
    if (busy) return;
    const requests = [];
    for (const preset of shown) {
      const article = element('article', 'preset-browser-card');
      const choose = button('', () => showDetail(preset), 'preset-browser-apply');
      choose.setAttribute('aria-label', `View ${preset.name}`);
      const frame = element('span', 'preset-browser-image');
      const img = element('img', ''); img.alt = `${preset.name} preview`; img.hidden = true;
      const previewStatus = element('span', 'preset-browser-preview-status');
      const example = (preset.previews || []).find((item) => presetWebURL(item.after));
      previewStatus.textContent = example ? '' : !snapshot?.name ? 'Select a photo to preview' : 'Select to preview';
      if (example) { img.src = presetWebURL(example.after); img.hidden = false; img.loading = 'lazy'; previewStatus.hidden = true; }
      img.onerror = () => { img.hidden = true; previewStatus.hidden = false; previewStatus.textContent = 'Preview unavailable'; };
      frame.append(img, previewStatus);
      choose.append(frame, element('strong', 'preset-browser-name', preset.name),
        element('span', 'preset-browser-source', preset.author?.name || SOURCE_LABELS[preset.source] || 'LightTable'));
      if (hiddenIds.includes(presetKey(preset))) choose.append(element('span', 'preset-browser-source', 'Hidden'));
      const ignored = preset.conversion?.ignored || [];
      if (ignored.length) choose.append(element('span', 'preset-browser-warning', `${ignored.length} unsupported settings skipped`));
      const favorite = button('', () => toggleFavorite(preset), 'preset-browser-favorite');
      updateFavoriteButton(favorite, preset);
      article.append(choose, favorite); grid.append(article); cards.set(presetKey(preset), choose);
      if (!isCommunity && !example && !selected && getPreview && canApply(preset) && snapshot?.name) {
        previewStatus.textContent = 'Rendering preview…';
        requests.push({ preset: structuredClone(preset), photo: snapshot, image: img, previewStatus });
      }
    }
    select(selected ? presetKey(selected) : getSelectedName()); queue.replace(requests);
  }
  search.addEventListener('input', () => { cancel(); page = 0; searchTimer = setTimeout(render, 160); });
  hiddenFilter.onclick = () => { showHidden = !showHidden; page = 0; render(); };
  favoriteFilter.onclick = () => { favoritesOnly = !favoritesOnly; page = 0; render(); };
  tabs.forEach((tab, index) => {
    tab.onclick = () => {
      collection = tab.dataset.collection; selected = null; page = 0; render();
      if (collection === 'community' && !communityRequested) void ensureCommunity();
    };
    tab.onkeydown = (event) => {
      const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!delta) return;
      event.preventDefault(); const target = tabs[(index + delta + tabs.length) % tabs.length]; target.click(); target.focus();
    };
  });
  order.onchange = tag.onchange = () => { page = 0; render(); };
  refresh.onclick = () => void ensureCommunity(true);
  previous.onclick = () => { page--; render(); }; next.onclick = () => { page++; render(); };
  return {
    refresh(options = {}) { loading = options.loading ?? loading; render(); },
    setActive(value) { active = !!value; if (active) render(); else cancel(); },
    select,
    async openPreset(id) {
      active = true;
      const local = (getPresets() || []).find((preset) => presetKey(preset) === id);
      if (local) { collection = local.collection || 'yours'; showDetail(local); return; }
      collection = 'community';
      await ensureCommunity();
      const preset = (getCommunity()?.presets || []).find((item) => item.id === id);
      if (preset) showDetail({ ...preset, collection: 'community' });
      else { render(); status.textContent = 'This preset is unavailable. Refresh the catalog and try again.'; }
    },
    destroy() { destroyed = true; cancel(); root.remove(); },
  };
}
