import { normalizePresetPacks, defaultPresetPack, groupPresetPacks, movePresetToPack } from './preset-packs.js';
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
    append(items) {
      if (!controller || controller.signal.aborted) controller = new AbortController();
      pending.push(...items);
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

/** Rows apply immediately; details and organization never change the photo. */
export function createPresetBrowser({
  container, getPresets, getPhoto, getSelectedName = () => '',
  onSelect = () => {}, onApply, canApply = () => true,
  getFavorites = () => [], onFavoritesChange = () => {},
  getHidden = () => [], onHiddenChange = () => {},
  getPreview, managementSection, getCommunity = () => ({}), loadCommunity,
  getRecipe = async (preset) => preset, onInstall, onSubmit, onDuplicate,
  getSubmission, onDownloadExample,
  getAdjustment = () => null, onAmount = () => {},
  getOrganization = () => ({}), onOrganizationChange = () => {},
  requestPackName,
}) {
  let active = false, destroyed = false, loading = false, favoritesOnly = false;
  let collection = 'builtin', selected = null, snapshot, searchTimer, showHidden = false;
  let communityRequested = false, communityLoading = false, detailGeneration = 0;
  let communityPromise = null, detailController = null;
  let activationGeneration = 0, activationController = null;
  let selectedPhotoName = null, detailDismissed = true, lastAdjustmentId = null;
  const previewCache = new Map();
  const urls = new Set(), cards = new Map(), amountRows = new Map();
  let previewObserver, draggedPreset = null;
  const root = document.createElement('div');
  root.className = 'preset-browser';
  root.innerHTML = `<div class="preset-browser-tabs" role="tablist" aria-label="${i18nHTML(tr("Preset collection"))}">
      <button type="button" role="tab" data-collection="builtin" aria-selected="true">${i18nHTML(tr("Built-in"))}</button>
      <button type="button" role="tab" data-collection="yours" aria-selected="false">${i18nHTML(tr("Yours"))}</button>
      <button type="button" role="tab" data-collection="community" aria-selected="false">${i18nHTML(tr("Community"))}</button></div>
    <label class="preset-browser-search"><span class="sr-only">${i18nHTML(tr("Find a preset"))}</span><input type="search" placeholder="${i18nHTML(tr("Search presets"))}" autocomplete="off"></label>
    <div class="preset-browser-toolbar"><button type="button" data-filter="favorites" aria-pressed="false">${i18nHTML(tr("☆ Favorites"))}</button><button type="button" data-filter="hidden" aria-pressed="false">${i18nHTML(tr("Show hidden"))}</button><button type="button" data-action="new-pack">${i18nHTML(tr("New pack…"))}</button><button type="button" data-action="manage">${i18nHTML(tr("Manage…"))}</button></div>
    <div class="preset-browser-community-controls" hidden><label>${i18nHTML(tr("Collection"))}<select data-order><option value="featured">${i18nHTML(tr("Featured"))}</option><option value="new">${i18nHTML(tr("New"))}</option><option value="all">${i18nHTML(tr("All"))}</option></select></label><label>${i18nHTML(tr("Subject"))}<select data-tag><option value="">${i18nHTML(tr("All subjects"))}</option></select></label><button type="button" data-action="refresh" aria-label="${i18nHTML(tr("Refresh community presets"))}">${i18nHTML(tr("Refresh"))}</button></div>
    <p class="preset-browser-network" role="status" hidden></p>
    <p class="preset-browser-status" role="status"></p>
    <section class="preset-browser-detail" aria-label="${i18nHTML(tr("Selected preset"))}" hidden></section>
    <div class="preset-browser-packs" aria-label="${i18nHTML(tr("Preset packs"))}"></div>`;
  container.append(root);
  root.addEventListener('keydown', event => {
    if ([' ', 'Enter'].includes(event.key) && event.target.closest('button')) event.stopPropagation();
  });
  const search = root.querySelector('input');
  const status = root.querySelector('.preset-browser-status');
  const network = root.querySelector('.preset-browser-network');
  const grid = root.querySelector('.preset-browser-packs');
  const detail = root.querySelector('.preset-browser-detail');
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
    clearTimeout(searchTimer); queue.cancel(); previewObserver?.disconnect();
    detailGeneration++; detailController?.abort();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
  }
  function select(id = getSelectedName()) {
    const adjustment = getAdjustment();
    const photoName = getPhoto()?.name;
    for (const [key, button] of cards) {
      const enabled = adjustment?.id === key && adjustment.enabled;
      button.setAttribute('aria-pressed', String(!!enabled));
      button.title = enabled ? tr('Click to disable preset') : tr('Click to apply preset');
      button.closest('.preset-browser-card').classList.toggle('is-active', !!enabled);
    }
    for (const update of amountRows.values()) update(adjustment, photoName);
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
  function moveToPack(preset, packId) {
    const state = movePresetToPack(getOrganization(), preset, packId);
    state.collapsed = state.collapsed.filter(id => id !== (packId || defaultPresetPack(preset).id));
    onOrganizationChange(state); render();
  }
  root.querySelector('[data-action="new-pack"]').onclick = async () => {
    const targetCollection = collection;
    const name = await requestPackName?.('');
    if (!name?.trim() || destroyed) return;
    const state = normalizePresetPacks(getOrganization());
    state.packs.push({ id: `pack-${crypto.randomUUID()}`, name: name.trim().slice(0, 80), collection: targetCollection });
    search.value = ''; favoritesOnly = false; tag.value = '';
    onOrganizationChange(state); render();
  };
  function createAmountRow(preset, photo, adjustment) {
    const row = element('div', 'preset-browser-amount');
    const label = element('label', '', tr('Amount'));
    const amount = element('input', ''); amount.type = 'range';
    amount.id = `preset-amount-${amountRows.size}`; label.htmlFor = amount.id;
    amount.min = '0'; amount.max = '100'; amount.step = '1';
    const output = element('output', ''); output.htmlFor = amount.id;
    const update = (adjustment, photoName) => {
      const matches = adjustment?.id === presetKey(preset);
      // At zero, keep the slider available so the user can bring the look back.
      row.hidden = !matches || (!adjustment.enabled && adjustment.amount !== 0);
      amount.disabled = !matches || photoName !== photo?.name;
      amount.value = String(matches ? adjustment.amount : 100);
      output.textContent = formatNumber(Number(amount.value) / 100, { style: 'percent' });
      amount.setAttribute('aria-valuetext', output.textContent);
    };
    amount.oninput = () => { onAmount(presetKey(preset), photo?.name, amount.value, false); select(); };
    amount.onchange = () => { onAmount(presetKey(preset), photo?.name, amount.value, true); select(); };
    amount.onkeydown = event => event.stopPropagation();
    amount.ondblclick = () => { onAmount(presetKey(preset), photo?.name, 100, true); select(); };
    row.append(label, amount, output);
    amountRows.set(presetKey(preset), update); update(adjustment, photo?.name);
    return row;
  }
  function showDetail(preset, reveal = true) {
    selected = preset; detailDismissed = !reveal; onSelect(preset); render();
    if (reveal) detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  async function activatePreset(preset) {
    const photo = getPhoto();
    showDetail(preset, false);
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
    const enabled = getAdjustment()?.id === presetKey(preset) && getAdjustment().enabled;
    const toggle = button(enabled ? tr('Click to disable preset') : tr('Click to apply preset'),
      () => void activatePreset(preset), 'preset-browser-toggle');
    toggle.setAttribute('aria-pressed', String(!!enabled));
    toggle.disabled = !photo?.name || preset.compatible === false;
    actions.append(toggle);
    if (preset.collection !== 'applied') {
      const packLabel = element('label', 'preset-browser-pack-choice', tr('Move to pack'));
      const chooser = element('select', '');
      chooser.add(new Option(defaultPresetPack(preset).name, ''));
      const organization = normalizePresetPacks(getOrganization());
      for (const pack of organization.packs.filter(pack => pack.collection === (preset.collection || 'yours'))) {
        chooser.add(new Option(pack.name, pack.id));
      }
      chooser.value = organization.assignments[presetKey(preset)] || '';
      chooser.onchange = () => moveToPack(preset, chooser.value);
      packLabel.append(chooser); detail.append(packLabel);
    }
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
      selectedPhotoName = snapshot?.name; selected = null; detailDismissed = true; lastAdjustmentId = null;
    }
    const adjustment = getAdjustment();
    if (adjustment && ((!selected && !detailDismissed) || adjustment.id !== lastAdjustmentId)) {
      selected = (presetKey(selected || {}) === adjustment.id ? selected : null) ||
        (getPresets() || []).find(item => presetKey(item) === adjustment.id) ||
        (getCommunity()?.presets || []).map(item => ({ ...item, collection: 'community' })).find(item => presetKey(item) === adjustment.id) ||
        { id: adjustment.id, name: adjustment.name, collection: 'applied', description: tr('Saved with this photo.') };
      collection = selected.collection === 'applied' ? 'yours' : selected.collection || 'yours';
      onSelect(selected);
    }
    lastAdjustmentId = adjustment?.id || null;
    const favorites = [...getFavorites()];
    const remote = getCommunity() || {};
    const inventory = collection === 'community' ? (remote.presets || []).map((preset) => ({ ...preset, collection: 'community' })) : getPresets() || [];
    const isCommunity = collection === 'community';
    const hiddenIds = getHidden();
    hiddenFilter.hidden = collection !== 'builtin' || !hiddenIds.length;
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
    tabs.forEach((tab) => { const current = tab.dataset.collection === collection; tab.setAttribute('aria-selected', String(current)); tab.tabIndex = current ? 0 : -1; });
    favoriteFilter.setAttribute('aria-pressed', String(favoritesOnly));
    const busy = !isCommunity && loading;
    status.textContent = busy ? tr('Loading presets…') : !filtered.length
      ? favoritesOnly ? tr('No favorites here yet. Star a preset to save it.')
        : search.value || tag.value ? tr('No matching presets.')
        : collection === 'yours' ? tr('Save your current look, import presets, or add one from Community.')
        : communityLoading ? '' : tr('No presets in this collection yet.')
      : '';
    grid.replaceChildren(); cards.clear(); amountRows.clear(); detail.hidden = !selected || detailDismissed;
    if (selected && !detailDismissed) {
      // Refresh an installed listing after updates while retaining remote identity.
      selected = inventory.find((preset) => presetKey(preset) === presetKey(selected)) || selected;
      void renderDetail(selected, snapshot);
    }
    if (busy) return;
    if (adjustment && !(getPresets() || []).some(item => presetKey(item) === adjustment.id) &&
        !search.value && !favoritesOnly && collection === 'yours') {
      filtered.unshift({ id: adjustment.id, name: adjustment.name, collection: 'applied', description: tr('Saved with this photo.') });
      status.textContent = '';
    }
    const organization = normalizePresetPacks(getOrganization());
    const searching = !!search.value.trim() || favoritesOnly || !!(isCommunity && tag.value);
    const packs = groupPresetPacks(filtered, organization, collection, { includeEmpty: !searching });
    queue.replace([]);
    const previewRequests = new Map();
    const observedPhoto = snapshot;
    const observer = new IntersectionObserver(entries => {
      if (!active || destroyed || snapshot !== observedPhoto) return;
      const visible = [];
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const request = previewRequests.get(entry.target);
        if (request) { visible.push(request); previewRequests.delete(entry.target); }
        observer.unobserve(entry.target);
      }
      queue.append(visible);
    }, { rootMargin: '120px' });
    previewObserver = observer;
    for (const pack of packs) {
      const section = element('section', 'preset-browser-pack');
      const header = element('div', 'preset-browser-pack-header');
      const expanded = searching || !organization.collapsed.includes(pack.id);
      const contents = element('div', 'preset-browser-pack-list');
      contents.id = `preset-pack-${grid.childElementCount}`;
      const disclosure = button('', () => {
        const state = normalizePresetPacks(getOrganization());
        const open = disclosure.getAttribute('aria-expanded') === 'true';
        state.collapsed = open ? [...new Set([...state.collapsed, pack.id])] : state.collapsed.filter(id => id !== pack.id);
        onOrganizationChange(state); render();
        [...grid.querySelectorAll('[data-pack-id]')].find(el => el.dataset.packId === pack.id)?.focus({ preventScroll: true });
      }, 'preset-browser-pack-disclosure');
      disclosure.dataset.packId = pack.id;
      disclosure.setAttribute('aria-expanded', String(expanded));
      disclosure.setAttribute('aria-controls', contents.id);
      const chevron = element('span', 'preset-browser-pack-chevron', '›'); chevron.setAttribute('aria-hidden', 'true');
      disclosure.append(chevron, element('strong', '', pack.name), element('span', 'preset-browser-pack-count', formatNumber(pack.presets.length)));
      header.append(disclosure);
      if (pack.custom) {
        const rename = button('…', async () => {
          const name = await requestPackName?.(pack.name);
          if (!name?.trim() || destroyed) return;
          const state = normalizePresetPacks(getOrganization());
          state.packs = state.packs.map(item => item.id === pack.id ? { ...item, name: name.trim().slice(0, 80) } : item);
          onOrganizationChange(state); render();
        }, 'preset-browser-pack-rename');
        rename.setAttribute('aria-label', tr('Rename pack'));
        header.append(rename);
        const remove = button('×', () => {
          const state = normalizePresetPacks(getOrganization());
          state.packs = state.packs.filter(item => item.id !== pack.id);
          state.collapsed = state.collapsed.filter(id => id !== pack.id);
          onOrganizationChange(normalizePresetPacks(state)); render();
        }, 'preset-browser-pack-remove');
        remove.setAttribute('aria-label', tr('Remove pack')); header.append(remove);
        header.ondragover = event => {
          if (!draggedPreset || (draggedPreset.collection || 'yours') !== collection) return;
          event.preventDefault(); event.dataTransfer.dropEffect = 'move'; header.classList.add('is-drop-target');
        };
        header.ondragleave = () => header.classList.remove('is-drop-target');
        header.ondrop = event => {
          event.preventDefault(); header.classList.remove('is-drop-target');
          if (draggedPreset) moveToPack(draggedPreset, pack.id);
        };
      }
      section.append(header, contents); grid.append(section);
      contents.hidden = !expanded;
      if (!expanded) continue;
      if (!pack.presets.length) contents.append(element('p', 'preset-browser-empty-pack', tr('Drag presets here, or use Move to pack in preset details.')));
      for (const preset of pack.presets) {
        const article = element('article', 'preset-browser-card');
        article.dataset.presetId = presetKey(preset);
        const choose = button('', () => void activatePreset(preset), 'preset-browser-apply');
        choose.setAttribute('aria-label', tr('Toggle {name}', {name: preset.name}));
        choose.disabled = !snapshot?.name || preset.compatible === false ||
          (preset.collection !== 'community' && adjustment?.id !== presetKey(preset) && !canApply(preset));
        choose.draggable = preset.collection !== 'applied';
        choose.ondragstart = event => {
          draggedPreset = preset; event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', preset.name);
        };
        choose.ondragend = () => { draggedPreset = null; grid.querySelectorAll('.is-drop-target').forEach(el => el.classList.remove('is-drop-target')); };
        const frame = element('span', 'preset-browser-image');
        const img = element('img', ''); img.alt = tr('{name} preview', {name: preset.name}); img.hidden = true; img.draggable = false;
        const previewStatus = element('span', 'preset-browser-preview-status', '◈');
        previewStatus.title = !snapshot?.name ? tr('Select a photo to preview') : tr('Click to apply');
        const example = (preset.previews || []).find(item => presetWebURL(item.after));
        if (example) { img.src = presetWebURL(example.after); img.hidden = false; img.loading = 'lazy'; previewStatus.hidden = true; }
        img.onerror = () => { img.hidden = true; previewStatus.hidden = false; previewStatus.title = tr('Preview unavailable'); };
        frame.append(img, previewStatus);
        const copy = element('span', 'preset-browser-row-copy');
        copy.append(element('strong', 'preset-browser-name', preset.name),
          element('span', 'preset-browser-source', preset.author?.name || SOURCE_LABELS[preset.source] || 'LightTable'));
        if (hiddenIds.includes(presetKey(preset))) copy.append(element('span', 'preset-browser-source', tr('Hidden')));
        const ignored = preset.conversion?.ignored || [];
        if (ignored.length) copy.append(element('span', 'preset-browser-warning', trn('{count} unsupported setting skipped', '{count} unsupported settings skipped', ignored.length)));
        choose.append(frame, copy);
        const favorite = button('', () => toggleFavorite(preset), 'preset-browser-favorite');
        updateFavoriteButton(favorite, preset);
        const info = button('…', () => showDetail(preset), 'preset-browser-info');
        info.setAttribute('aria-label', tr('Preset details: {name}', {name: preset.name}));
        article.append(choose, favorite, info, createAmountRow(preset, snapshot, adjustment));
        contents.append(article); cards.set(presetKey(preset), choose);
        if (!isCommunity && !example && getPreview && canApply(preset) && snapshot?.name) {
          const cacheKey = JSON.stringify([snapshot.name, snapshot.engine, adjustment?.base || snapshot.state, preset]);
          const cached = previewCache.get(cacheKey);
          if (cached) {
            img.src = cached instanceof Blob ? URL.createObjectURL(cached) : cached;
            if (cached instanceof Blob) urls.add(img.src);
            img.hidden = false; previewStatus.hidden = true;
          } else {
            previewRequests.set(frame, { preset: structuredClone(preset), photo: snapshot, image: img, previewStatus, cacheKey, width: 160 });
            previewObserver.observe(frame);
          }
        }
      }
    }
    select();
  }
  search.addEventListener('input', () => { cancel(); searchTimer = setTimeout(render, 160); });
  hiddenFilter.onclick = () => { showHidden = !showHidden; render(); };
  favoriteFilter.onclick = () => { favoritesOnly = !favoritesOnly; render(); };
  tabs.forEach((tab, index) => {
    tab.onclick = () => {
      collection = tab.dataset.collection; selected = null; detailDismissed = true; render();
      if (collection === 'community' && !communityRequested) void ensureCommunity();
    };
    tab.onkeydown = (event) => {
      const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!delta) return;
      event.preventDefault(); event.stopPropagation(); const target = tabs[(index + delta + tabs.length) % tabs.length]; target.click(); target.focus();
    };
  });
  order.onchange = tag.onchange = () => { render(); };
  refresh.onclick = () => void ensureCommunity(true);
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
