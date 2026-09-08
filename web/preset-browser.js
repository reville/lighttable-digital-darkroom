import { t as tr, tn as trn, currentLocale, formatNumber } from './i18n.js';

const i18nHTML = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll("\"", "&quot;").replaceAll("'", "&#39;");
const SOURCE_LABELS = {
  lighttable: 'LightTable', lightroom: 'Lightroom / Camera Raw',
  'capture-one': 'Capture One',
};

function filmBehaviorLabel(mode) {
  if (mode === 'preserve') return tr('Film unchanged.');
  if (mode === 'on') return tr('Film on.');
  if (mode === 'off') return tr('Film off.');
  return tr('Film {mode}.', {mode});
}

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
export function formatPresetCatalogDate(value, locale = currentLocale()) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return date.toLocaleDateString(locale, dateOnly ? { timeZone: 'UTC' } : undefined);
}

/** Cards apply immediately; remote deep links only open their details. */
export function createPresetBrowser({
  container, getPresets, getPhoto, getSelectedName = () => '',
  onSelect = () => {}, onApply, canApply = () => true,
  getFavorites = () => [], onFavoritesChange = () => {},
  getHidden = () => [], onHiddenChange = () => {},
  getPreview, managementSection, getCommunity = () => ({}), loadCommunity,
  getRecipe = async (preset) => preset, onInstall, onSubmit, onDuplicate,
  getSubmission, onDownloadExample,
  getAdjustment = () => null, onAmount = () => {},
}) {
  let active = false, destroyed = false, loading = false, page = 0, favoritesOnly = false;
  let collection = 'builtin', selected = null, snapshot, searchTimer, showHidden = false;
  let communityRequested = false, communityLoading = false, detailGeneration = 0;
  let communityPromise = null, detailController = null;
  let activationGeneration = 0, activationController = null;
  let selectedPhotoName = null, detailDismissed = false, lastAdjustmentId = null;
  const previewCache = new Map();
  const pageSize = 6, urls = new Set(), cards = new Map();
  const root = document.createElement('div');
  root.className = 'preset-browser';
  root.innerHTML = `<div class="preset-browser-tabs" role="tablist" aria-label="${i18nHTML(tr("Preset collection"))}">
      <button type="button" role="tab" data-collection="builtin" aria-selected="true">${i18nHTML(tr("Built-in"))}</button>
      <button type="button" role="tab" data-collection="yours" aria-selected="false">${i18nHTML(tr("Yours"))}</button>
      <button type="button" role="tab" data-collection="community" aria-selected="false">${i18nHTML(tr("Community"))}</button></div>
    <label class="preset-browser-search"><span class="sr-only">${i18nHTML(tr("Find a preset"))}</span><input type="search" placeholder="${i18nHTML(tr("Search presets"))}" autocomplete="off"></label>
    <div class="preset-browser-toolbar"><button type="button" data-filter="favorites" aria-pressed="false">${i18nHTML(tr("☆ Favorites"))}</button><button type="button" data-filter="hidden" aria-pressed="false">${i18nHTML(tr("Show hidden"))}</button><button type="button" data-action="manage">${i18nHTML(tr("Manage…"))}</button></div>
    <div class="preset-browser-community-controls" hidden><label>${i18nHTML(tr("Collection"))}<select data-order><option value="featured">${i18nHTML(tr("Featured"))}</option><option value="new">${i18nHTML(tr("New"))}</option><option value="all">${i18nHTML(tr("All"))}</option></select></label><label>${i18nHTML(tr("Subject"))}<select data-tag><option value="">${i18nHTML(tr("All subjects"))}</option></select></label><button type="button" data-action="refresh" aria-label="${i18nHTML(tr("Refresh community presets"))}">${i18nHTML(tr("Refresh"))}</button></div>
    <p class="preset-browser-network" role="status" hidden></p>
    <p class="preset-browser-status" role="status"></p>
    <section class="preset-browser-detail" aria-label="${i18nHTML(tr("Selected preset"))}" hidden></section>
    <div class="preset-browser-grid" aria-label="${i18nHTML(tr("Preset previews"))}"></div>
    <div class="preset-browser-pages"><button type="button" data-page="previous" aria-label="${i18nHTML(tr("Previous presets"))}">${i18nHTML(tr("Previous"))}</button><span></span><button type="button" data-page="next" aria-label="${i18nHTML(tr("Next presets"))}">${i18nHTML(tr("Next"))}</button></div>`;
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
  const manage = root.querySelector('[data-action="manage"]');
  manage.classList.add('preset-browser-manage');
  let syncManagement;
  if (managementSection) {
    manage.setAttribute('aria-label', tr("Manage presets"));
    manage.setAttribute('aria-controls', managementSection.id);
    syncManagement = () => manage.setAttribute('aria-expanded', String(managementSection.open));
    syncManagement();
    managementSection.addEventListener('toggle', syncManagement);
    manage.onclick = () => {
      managementSection.open = !managementSection.open;
      syncManagement();
      if (managementSection.open) {
        managementSection.querySelector('summary').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    };
  } else manage.hidden = true;
  const queue = createPresetPreviewQueue({
    render: (item, options) => getPreview(item.preset, item.photo, { ...options, width: item.width || 320 }),
    onResult: ({ image, previewStatus, after, download, cacheKey }, value) => {
      if (download) { void download(value); return; }
      if (!value) { previewStatus.textContent = tr('Preview unavailable'); return; }
      if (cacheKey) {
        previewCache.set(cacheKey, value);
        if (previewCache.size > 36) previewCache.delete(previewCache.keys().next().value);
      }
      const url = value instanceof Blob ? URL.createObjectURL(value) : value;
      if (value instanceof Blob) urls.add(url);
      image.src = url; image.hidden = false; previewStatus.hidden = true;
      after?.();
    },
    onError: ({ previewStatus, onError }) => { previewStatus.hidden = false; previewStatus.textContent = tr("Preview unavailable. Try again."); onError?.(); },
  });
  function cancel() {
    clearTimeout(searchTimer); queue.cancel();
    detailGeneration++; detailController?.abort();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
  }
  function select(id = getSelectedName()) {
    const adjustment = getAdjustment();
    for (const [key, button] of cards) {
      const enabled = adjustment?.id === key && adjustment.enabled;
      button.setAttribute('aria-pressed', String(!!enabled));
      button.title = enabled ? tr('Click to disable preset') : tr('Click to apply preset');
    }
  }
  function updateFavoriteButton(button, preset) {
    const favorite = getFavorites().includes(presetKey(preset));
    button.textContent = favorite ? '★' : '☆';
    button.setAttribute('aria-pressed', String(favorite));
    button.setAttribute('aria-label', favorite ? tr('Remove {name} from favorites', {name: preset.name}) : tr('Add {name} to favorites', {name: preset.name}));
    button.title = favorite ? tr("Remove from favorites") : tr("Add to favorites");
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
      catch (error) { network.textContent = error.message || tr("Community is unavailable"); }
      finally { communityLoading = false; render(); }
    })();
    return communityPromise;
  }
  function photoUnchanged(photo) {
    return photo && JSON.stringify(getPhoto()) === JSON.stringify(photo);
  }
  function showDetail(preset) {
    selected = preset; detailDismissed = false; onSelect(preset); render();
    detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  async function activatePreset(preset) {
    const photo = getPhoto();
    showDetail(preset);
    const request = ++activationGeneration;
    activationController?.abort(); activationController = new AbortController();
    if (!photo?.name || preset.compatible === false) return;
    try {
      const alreadySelected = getAdjustment()?.id === presetKey(preset);
      const recipe = alreadySelected ? preset : await getRecipe(preset, { signal: activationController.signal });
      if (request !== activationGeneration || !active || destroyed ||
          presetKey(selected || {}) !== presetKey(preset) || !photoUnchanged(photo)) return;
      if (!alreadySelected && !canApply(recipe)) return;
      await onApply(recipe, photo, presetKey(preset));
      render();
      cards.get(presetKey(preset))?.focus({ preventScroll: true });
    } catch (error) {
      if (request === activationGeneration && active && !destroyed && error.name !== 'AbortError') {
        status.textContent = error.message || tr('Could not apply preset. Try again.');
      }
    }
  }
  async function renderDetail(preset, photo) {
    const request = ++detailGeneration;
    detailController = new AbortController();
    const signal = detailController.signal;
    const valid = () => request === detailGeneration && !signal.aborted && active;
    detail.hidden = false; detail.replaceChildren();
    const heading = element('div', 'preset-browser-detail-heading');
    heading.append(element('strong', '', preset.name), button('×', () => { selected = null; detailDismissed = true; render(); }, 'preset-browser-close'));
    heading.lastChild.setAttribute('aria-label', tr('Close preset details'));
    const credit = element('p', 'preset-browser-credit');
    credit.append(link(preset.author?.name || 'LightTable', preset.author?.url) || document.createTextNode(preset.author?.name || SOURCE_LABELS[preset.source] || 'LightTable'));
    const version = preset.version ? tr('Version {version}', {version: preset.version}) : '';
    if (version) credit.append(document.createTextNode(` · ${version}`));
    const description = element('p', 'preset-browser-description', preset.description || tr('Click the preset to apply it to your photo.'));
    const mode = preset.filmMode || (preset.includeFilm ? 'on' : 'preserve');
    const behavior = element('p', 'preset-browser-hint', `${filmBehaviorLabel(mode)} ${preset.scope === 'look' ? tr('Exposure, white balance and photo corrections are kept.') : tr('Applies the included adjustments.')}`);
    detail.append(heading, credit, description, behavior);
    if (preset.collection === 'applied') { credit.hidden = true; behavior.hidden = true; }
    const examples = (preset.previews || []).filter((example) => presetWebURL(example.after));
    if (examples.length) {
      const figure = element('figure', 'preset-browser-example');
      const img = element('img', ''); img.loading = 'lazy';
      img.onerror = () => { figure.hidden = true; };
      const label = element('figcaption', '');
      const controls = element('div', 'preset-browser-compare');
      const showExample = (example) => {
        figure.hidden = false;
        img.alt = example.label || tr('{name} example', {name: preset.name});
        img.src = presetWebURL(example.after);
        label.textContent = example.label || tr("Creator example");
        if (example.credit?.name) {
          label.append(document.createTextNode(` · ${tr('Photo:')} `));
          label.append(link(example.credit.name, example.credit.url) || document.createTextNode(example.credit.name));
          if (example.credit.license) label.append(document.createTextNode(` · ${example.credit.license}`));
        }
        controls.replaceChildren();
        const beforeURL = presetWebURL(example.before);
        if (beforeURL) {
          controls.append(button(tr("Before"), () => { img.src = beforeURL; }), button(tr("With preset"), () => { img.src = presetWebURL(example.after); }));
        }
      };
      figure.append(img, label, controls);
      if (examples.length > 1) {
        const chooser = element('select', 'preset-browser-example-choice');
        chooser.setAttribute('aria-label', tr("Example photo"));
        examples.forEach((example, index) => chooser.add(new Option(example.label || tr('Example {number}', {number: formatNumber(index + 1)}), String(index))));
        chooser.onchange = () => showExample(examples[Number(chooser.value)]);
        figure.append(chooser);
      }
      showExample(examples[0]); detail.append(figure);
    }
    const actions = element('div', 'preset-browser-detail-actions');
    const message = element('p', 'preset-browser-status'); message.setAttribute('role', 'status');
    let recipe = null, prepareSubmission = null;
    const amountRow = element('div', 'preset-browser-amount');
    const amountLabel = element('label', '', tr('Amount')); amountLabel.htmlFor = 'presetAmount';
    const amount = element('input', ''); amount.type = 'range'; amount.id = 'presetAmount';
    amount.min = '0'; amount.max = '100'; amount.step = '1';
    const output = element('output', ''); output.htmlFor = amount.id;
    const toggle = button(tr('Off'), () => void activatePreset(preset), 'preset-browser-toggle');
    const updateAmount = () => {
      const adjustment = getAdjustment();
      const matches = adjustment?.id === presetKey(preset);
      amount.disabled = !matches;
      amount.value = String(matches ? adjustment.amount : 100);
      const amountText = formatNumber(Number(amount.value) / 100, {style: 'percent'});
      output.textContent = amountText;
      amount.setAttribute('aria-valuetext', amountText);
      toggle.textContent = matches && adjustment.enabled ? tr('On') : tr('Off');
      toggle.setAttribute('aria-pressed', String(!!(matches && adjustment.enabled)));
      toggle.setAttribute('aria-label', matches && adjustment.enabled
        ? tr('Disable {name}', {name: preset.name}) : tr('Enable {name}', {name: preset.name}));
    };
    amount.oninput = () => {
      onAmount(presetKey(preset), photo?.name, amount.value, false);
      updateAmount(); select();
    };
    amount.onchange = () => {
      onAmount(presetKey(preset), photo?.name, amount.value, true);
      updateAmount(); select();
    };
    amount.onkeydown = event => event.stopPropagation();
    amount.ondblclick = () => {
      onAmount(presetKey(preset), photo?.name, 100, true);
      updateAmount(); select();
    };
    amountRow.append(amountLabel, output, toggle, amount);
    detail.insertBefore(amountRow, credit);
    updateAmount(); toggle.disabled = !photo?.name || getAdjustment()?.id !== presetKey(preset);
    if (preset.collection === 'community' && onInstall) {
      const installed = getPresets().find((item) => item.community?.id === preset.id);
      const upToDate = installed?.community?.version === preset.version;
      const install = button(preset.compatible === false ? tr("Update LightTable") : upToDate ? tr("Added to Yours") : installed ? tr("Update in Yours") : tr("Add to Yours"), async () => {
        install.disabled = true;
        try { await onInstall(preset); if (valid()) render(); }
        catch (error) { if (valid()) { install.disabled = false; message.textContent = error.message || tr("Could not install preset"); } }
      });
      install.disabled = upToDate || preset.compatible === false; actions.append(install);
    }
    if (preset.collection === 'builtin' && onDuplicate) {
      const duplicate = button(tr("Save a copy…"), async () => {
        duplicate.disabled = true;
        try { await onDuplicate(preset); }
        catch (error) { if (valid()) message.textContent = error.message || tr("Could not save a copy"); }
        finally { duplicate.disabled = false; }
      });
      actions.append(duplicate);
    }
    if (preset.collection === 'builtin') {
      const isHidden = getHidden().includes(presetKey(preset));
      actions.append(button(isHidden ? tr("Restore to Built-in") : tr("Hide preset"), () => {
        const ids = getHidden(), id = presetKey(preset);
        onHiddenChange(isHidden ? ids.filter((value) => value !== id) : [...ids, id]);
        if (!isHidden) selected = null;
        render();
      }));
    }
    detail.append(actions, message);
    const more = element('div', 'preset-browser-detail-links');
    const pageLink = link(tr("Preset page ↗"), preset.pageUrl);
    if (pageLink) more.append(pageLink);
    if (preset.license) more.append(element('span', 'preset-browser-license', preset.license));
    if (preset.parentId) {
      const parentURL = /^[a-z0-9][a-z0-9-]{0,39}\/[a-z0-9][a-z0-9-]{0,59}$/.test(preset.parentId)
        ? `https://lighttable.app/presets/${preset.parentId}/` : null;
      more.append(link(tr('From {parentId}', {parentId: preset.parentId}), parentURL) || element('span', 'preset-browser-license', tr('From {parentId}', {parentId: preset.parentId})));
    }
    if (preset.collection !== 'community' && preset.collection !== 'builtin' && preset.collection !== 'applied' && onSubmit) {
      const submission = element('section', 'preset-browser-submission');
      submission.append(element('strong', '', tr("Community submission")));
      const scope = element('div', 'preset-browser-submission-scope');
      const submissionStatus = element('p', 'preset-browser-status', tr("Preparing the included-settings summary…"));
      submissionStatus.setAttribute('role', 'status');
      const rights = element('p', 'preset-browser-hint', tr("Only share photos you have permission to publish"));
      const submit = button(tr("Prepare community submission…"), async () => {
        submit.disabled = true;
        submissionStatus.textContent = tr("Preparing the preset and three licensed example pairs…");
        try {
          const result = await onSubmit(preset);
          if (!valid()) return;
          submissionStatus.textContent = tr("Submission package prepared. Review its examples and rights notes, then complete the submission form.");
          const submissionLink = link(tr("Open submission form ↗"), result.submissionUrl);
          if (submissionLink) submission.append(submissionLink);
        } catch (error) { if (valid()) submissionStatus.textContent = error.message || tr("Could not prepare submission"); }
        finally { if (valid()) submit.disabled = false; }
      });
      submit.disabled = true;
      const example = button(tr("Download example on this photo"), () => {
        if (!photoUnchanged(photo)) { render(); return; }
        example.disabled = true;
        submissionStatus.textContent = tr('Rendering a 960-pixel JPEG on this computer…');
        queue.replace([{ preset: sharedRecipe, photo, width: 960, previewStatus: submissionStatus,
          onError() { example.disabled = false; },
          async download(value) {
            if (!valid()) return;
            try {
              await onDownloadExample(preset, value);
              if (valid()) submissionStatus.textContent = tr('Example download prepared. Your edit has not changed.');
            } catch (error) { if (valid()) submissionStatus.textContent = error.message || tr('Could not save the example'); }
            finally { if (valid()) example.disabled = false; }
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
          if (!sharedRecipe || !Array.isArray(sharedRecipe.includedGrade) || !Array.isArray(sharedRecipe.includedFilm)) throw new Error(tr("The submission scope is unavailable"));
          scope.replaceChildren(element('p', '', filmBehaviorLabel(sharedRecipe.filmMode)));
          const fields = element('dl', '');
          for (const [label, keys] of [[tr("Develop settings"), sharedRecipe.includedGrade], [tr("Film settings"), sharedRecipe.includedFilm]]) {
            fields.append(element('dt', '', label), element('dd', '', keys.length ? keys.join(', ') : tr("None")));
          }
          scope.append(fields, element('p', 'preset-browser-hint', tr("Keeps Edit Exposure, Temp/Tint, capture white balance, RAW settings, film format, crop, geometry, masks, Remove, lens corrections, sharpening and noise reduction.")));
          submit.disabled = false;
          example.disabled = !photo?.name || !getPreview || !onDownloadExample;
          submissionStatus.textContent = tr("The package includes this recipe, three licensed example pairs, and rights notes. Nothing is uploaded.");
        } catch (error) { if (valid()) submissionStatus.textContent = error.message || tr("Could not inspect the submission settings"); }
      };
      submission.append(scope, submissionStatus, submit, example, rights);
      detail.append(submission);
    }
    detail.append(more);
    if (preset.compatible === false) {
      message.textContent = tr("Update LightTable to use this preset.");
      return;
    }
    if (preset.collection === 'community') message.textContent = tr("Loading recipe…");
    try {
      recipe = await getRecipe(preset, { signal });
      if (!valid()) return;
      const compatible = getAdjustment()?.id === presetKey(preset) || canApply(recipe);
      toggle.disabled = !photo?.name || !compatible;
      message.textContent = !compatible ? tr('No compatible adjustments.') : !photo?.name ? tr('Select a photo to apply this preset.') : '';
      if (prepareSubmission) await prepareSubmission();
    } catch (error) {
      if (valid()) message.textContent = error.message || tr("Recipe unavailable. Refresh the community catalog and try again.");
    }
  }
  function render() {
    cancel();
    if (!active || destroyed) return;
    snapshot = getPhoto(); snapshot = snapshot ? structuredClone(snapshot) : null;
    if (selectedPhotoName !== snapshot?.name) {
      selectedPhotoName = snapshot?.name; selected = null; detailDismissed = false;
    }
    const adjustment = getAdjustment();
    if (adjustment && ((!selected && !detailDismissed) || adjustment.id !== lastAdjustmentId)) {
      selected = (getPresets() || []).find(item => presetKey(item) === adjustment.id) ||
        { id: adjustment.id, name: adjustment.name, collection: 'applied', description: tr('Saved with this photo.') };
      if (selected.collection !== 'applied') collection = selected.collection || 'yours';
      onSelect(selected);
    }
    lastAdjustmentId = adjustment?.id || null;
    const favorites = [...getFavorites()];
    const remote = getCommunity() || {};
    const inventory = collection === 'community' ? (remote.presets || []).map((preset) => ({ ...preset, collection: 'community' })) : getPresets() || [];
    const isCommunity = collection === 'community';
    const hiddenIds = getHidden();
    hiddenFilter.hidden = collection !== 'builtin';
    hiddenFilter.disabled = !hiddenIds.length;
    hiddenFilter.textContent = hiddenIds.length ? tr('Show hidden ({count})', {count: formatNumber(hiddenIds.length)}) : tr("Show hidden");
    hiddenFilter.setAttribute('aria-pressed', String(showHidden));
    root.querySelector('.preset-browser-community-controls').hidden = !isCommunity;
    network.hidden = !isCommunity;
    refresh.disabled = communityLoading;
    network.textContent = communityLoading ? tr("Refreshing community…") : remote.offline
      ? (remote.presets?.length ? tr("Offline · showing saved catalog") : tr("Community is offline. Built-in and saved presets remain available."))
      : remote.error ? remote.error : remote.updatedAt ? tr('Catalog updated {date}', {date: formatPresetCatalogDate(remote.updatedAt)}) : '';
    const keptTag = tag.value;
    tag.replaceChildren(new Option(tr("All subjects"), ''));
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
    pages.querySelector('span').textContent = tr('{first}–{last} of {total}', {first: formatNumber(page * pageSize + 1), last: formatNumber(page * pageSize + shown.length), total: formatNumber(filtered.length)});
    tabs.forEach((tab) => { const current = tab.dataset.collection === collection; tab.setAttribute('aria-selected', String(current)); tab.tabIndex = current ? 0 : -1; });
    favoriteFilter.setAttribute('aria-pressed', String(favoritesOnly));
    const busy = !isCommunity && loading;
    status.textContent = busy ? tr('Loading presets…') : !filtered.length
      ? favoritesOnly ? tr('No favorites here yet. Star a preset to save it.')
        : search.value || tag.value ? tr('No matching presets.')
        : collection === 'yours' ? tr('Save your current look, import presets, or add one from Community.')
        : communityLoading ? '' : tr('No presets in this collection yet.')
      : trn('{count} preset · click to apply, click again to turn off',
        '{count} presets · click to apply, click again to turn off', filtered.length);
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
      const choose = button('', () => void activatePreset(preset), 'preset-browser-apply');
      choose.setAttribute('aria-label', tr('Toggle {name}', {name: preset.name}));
      const frame = element('span', 'preset-browser-image');
      const img = element('img', ''); img.alt = tr('{name} preview', {name: preset.name}); img.hidden = true;
      const previewStatus = element('span', 'preset-browser-preview-status');
      const example = (preset.previews || []).find((item) => presetWebURL(item.after));
      previewStatus.textContent = example ? '' : !snapshot?.name ? tr('Select a photo to preview') : tr('Click to apply');
      if (example) { img.src = presetWebURL(example.after); img.hidden = false; img.loading = 'lazy'; previewStatus.hidden = true; }
      img.onerror = () => { img.hidden = true; previewStatus.hidden = false; previewStatus.textContent = tr("Preview unavailable"); };
      frame.append(img, previewStatus);
      choose.append(frame, element('strong', 'preset-browser-name', preset.name),
        element('span', 'preset-browser-source', preset.author?.name || SOURCE_LABELS[preset.source] || 'LightTable'));
      if (hiddenIds.includes(presetKey(preset))) choose.append(element('span', 'preset-browser-source', tr("Hidden")));
      const ignored = preset.conversion?.ignored || [];
      if (ignored.length) choose.append(element('span', 'preset-browser-warning', trn('{count} unsupported setting skipped', '{count} unsupported settings skipped', ignored.length)));
      const favorite = button('', () => toggleFavorite(preset), 'preset-browser-favorite');
      updateFavoriteButton(favorite, preset);
      article.append(choose, favorite); grid.append(article); cards.set(presetKey(preset), choose);
      if (!isCommunity && !example && getPreview && canApply(preset) && snapshot?.name) {
        const adjustment = getAdjustment();
        const cacheKey = JSON.stringify([snapshot.name, snapshot.engine,
          adjustment?.base || snapshot.state, preset]);
        const cached = previewCache.get(cacheKey);
        if (cached) {
          img.src = cached instanceof Blob ? URL.createObjectURL(cached) : cached;
          if (cached instanceof Blob) urls.add(img.src);
          img.hidden = false; previewStatus.hidden = true;
        } else {
          previewStatus.textContent = tr('Rendering preview…');
          requests.push({ preset: structuredClone(preset), photo: snapshot, image: img, previewStatus, cacheKey });
        }
      }
    }
    select(selected ? presetKey(selected) : getSelectedName()); queue.replace(requests);
  }
  search.addEventListener('input', () => { cancel(); page = 0; searchTimer = setTimeout(render, 160); });
  hiddenFilter.onclick = () => { showHidden = !showHidden; page = 0; render(); };
  favoriteFilter.onclick = () => { favoritesOnly = !favoritesOnly; page = 0; render(); };
  tabs.forEach((tab, index) => {
    tab.onclick = () => {
      collection = tab.dataset.collection; selected = null; detailDismissed = true; page = 0; render();
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
    setActive(value) { active = !!value; if (active) render(); else { cancel(); activationGeneration++; activationController?.abort(); } },
    select,
    async openPreset(id) {
      active = true;
      const local = (getPresets() || []).find((preset) => presetKey(preset) === id);
      if (local) { collection = local.collection || 'yours'; showDetail(local); return; }
      collection = 'community';
      await ensureCommunity();
      const preset = (getCommunity()?.presets || []).find((item) => item.id === id);
      if (preset) showDetail({ ...preset, collection: 'community' });
      else { render(); status.textContent = tr("This preset is unavailable. Refresh the catalog and try again."); }
    },
    destroy() {
      destroyed = true; cancel(); activationGeneration++; activationController?.abort(); previewCache.clear();
      if (syncManagement) managementSection.removeEventListener('toggle', syncManagement);
      root.remove();
    },
  };
}
