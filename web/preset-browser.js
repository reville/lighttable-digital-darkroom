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

export function filterPresets(presets, { query = '', favorites = [], favoritesOnly = false } = {}) {
  const text = query.trim().toLocaleLowerCase();
  const favoriteNames = new Set(favorites);
  return presets.filter((preset) => {
    if (favoritesOnly && !favoriteNames.has(preset.name)) return false;
    const source = SOURCE_LABELS[preset.source] || preset.source || 'LightTable';
    return !text || `${preset.name} ${source} ${preset.presetType || ''}`.toLocaleLowerCase().includes(text);
  });
}

/**
 * getPhoto returns a snapshot {name, state, engine, label}. getPreview receives
 * (preset, photoSnapshot, {signal}) and returns an image Blob or URL without
 * changing saved edits. onApply receives (preset, photoSnapshot), so the caller
 * can reject a click after photo navigation. Favorites use the app's preferences.
 * Call setActive(false) when leaving the pane to cancel previews and release URLs.
 */
export function createPresetBrowser({
  container, getPresets, getPhoto, getSelectedName = () => '',
  onSelect = () => {}, onApply, canApply = () => true,
  getFavorites = () => [], onFavoritesChange = () => {},
  getPreview, managementSection,
}) {
  let active = false, destroyed = false, loading = false, page = 0, favoritesOnly = false;
  let snapshot, searchTimer;
  const pageSize = 6, urls = new Set(), cards = new Map();
  const root = document.createElement('div');
  root.className = 'preset-browser';
  root.innerHTML = `<label class="preset-browser-search">Find a preset<input type="search" placeholder="Search presets" autocomplete="off"></label>
    <div class="preset-browser-toolbar"><div class="preset-browser-filters" role="group" aria-label="Filter presets"><button type="button" data-filter="all" aria-pressed="true">All</button><button type="button" data-filter="favorites" aria-pressed="false">Favorites</button></div></div>
    <p class="preset-browser-hint">Select a preview to apply its adjustments. Other edits are kept.</p>
    <p class="preset-browser-status" role="status"></p>
    <div class="preset-browser-grid" aria-label="Preset previews"></div>
    <div class="preset-browser-pages"><button type="button" data-page="previous" aria-label="Previous presets">Previous</button><span></span><button type="button" data-page="next" aria-label="Next presets">Next</button></div>`;
  container.append(root);
  const search = root.querySelector('input');
  const status = root.querySelector('.preset-browser-status');
  const grid = root.querySelector('.preset-browser-grid');
  const pages = root.querySelector('.preset-browser-pages');
  const previous = root.querySelector('[data-page="previous"]');
  const next = root.querySelector('[data-page="next"]');
  const filterButtons = [...root.querySelectorAll('[data-filter]')];
  let syncManagement;
  if (managementSection) {
    const manage = document.createElement('button');
    manage.type = 'button'; manage.className = 'preset-browser-manage';
    manage.textContent = 'Manage…'; manage.setAttribute('aria-label', 'Manage presets');
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
    root.querySelector('.preset-browser-toolbar').append(manage);
  }

  const queue = createPresetPreviewQueue({
    render: (item, options) => getPreview(item.preset, item.photo, options),
    onResult: ({ image, previewStatus }, value) => {
      if (!value) {
        previewStatus.textContent = 'Preview unavailable';
        return;
      }
      const url = value instanceof Blob ? URL.createObjectURL(value) : value;
      if (value instanceof Blob) urls.add(url);
      image.src = url; image.hidden = false;
      previewStatus.hidden = true;
    },
    onError: ({ previewStatus }) => { previewStatus.textContent = 'Preview unavailable'; },
  });
  function cancel() {
    clearTimeout(searchTimer);
    queue.cancel();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
  }
  function select(name = getSelectedName()) {
    for (const [presetName, button] of cards) {
      button.setAttribute('aria-pressed', String(presetName === name));
    }
  }
  function updateFavoriteButton(button, name, favorites) {
    const favorite = favorites.includes(name);
    button.textContent = favorite ? '★' : '☆';
    button.setAttribute('aria-pressed', String(favorite));
    button.setAttribute('aria-label', `${favorite ? 'Remove' : 'Add'} ${name} ${favorite ? 'from' : 'to'} favorites`);
    button.title = favorite ? 'Remove from favorites' : 'Add to favorites';
  }
  function render() {
    cancel();
    if (!active || destroyed) return;
    snapshot = getPhoto();
    // Callers may return their current state: never give a renderer live objects.
    snapshot = snapshot ? structuredClone(snapshot) : null;
    const favorites = [...getFavorites()];
    const presets = getPresets() || [];
    const filtered = filterPresets(presets, { query: search.value, favorites, favoritesOnly });
    page = Math.min(page, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
    const shown = filtered.slice(page * pageSize, (page + 1) * pageSize);
    previous.disabled = page === 0;
    next.disabled = (page + 1) * pageSize >= filtered.length;
    pages.hidden = filtered.length <= pageSize;
    pages.querySelector('span').textContent = `${page * pageSize + 1}–${page * pageSize + shown.length} of ${filtered.length}`;
    filterButtons.forEach((button) => button.setAttribute('aria-pressed', String((button.dataset.filter === 'favorites') === favoritesOnly)));
    status.textContent = loading ? 'Loading presets…'
      : !presets.length ? 'No presets yet. Import a preset or save your current edit from Manage.'
      : !filtered.length ? favoritesOnly && !search.value.trim() ? 'No favorites yet. Use the star on any preset to add one.' : 'No matching presets.'
      : !snapshot?.name ? 'Select a photo to preview and apply presets.'
      : `${filtered.length} preset${filtered.length === 1 ? '' : 's'} · previews use this photo’s edits`;
    grid.replaceChildren(); cards.clear();
    if (loading) return;
    const requests = [];
    for (const preset of shown) {
      const compatible = canApply(preset);
      const article = document.createElement('article');
      article.className = 'preset-browser-card';
      const apply = document.createElement('button');
      apply.type = 'button'; apply.className = 'preset-browser-apply';
      apply.disabled = !compatible || !snapshot?.name;
      apply.setAttribute('aria-label', `Apply ${preset.name}`);
      apply.title = compatible ? `Apply ${preset.name}, keeping other edits` : 'No compatible adjustments';
      const frame = document.createElement('span'); frame.className = 'preset-browser-image';
      const image = document.createElement('img'); image.alt = `${preset.name} preview`; image.hidden = true;
      const previewStatus = document.createElement('span'); previewStatus.className = 'preset-browser-preview-status';
      previewStatus.textContent = !compatible ? 'Not compatible' : !snapshot?.name ? 'Select a photo' : getPreview ? 'Rendering preview…' : 'Select to apply';
      image.onerror = () => { image.hidden = true; previewStatus.hidden = false; previewStatus.textContent = 'Preview unavailable'; };
      frame.append(image, previewStatus);
      const title = document.createElement('strong'); title.className = 'preset-browser-name'; title.textContent = preset.name;
      const source = document.createElement('span'); source.className = 'preset-browser-source'; source.textContent = SOURCE_LABELS[preset.source] || preset.source || 'LightTable';
      apply.append(frame, title, source);
      const ignored = preset.conversion?.ignored || [];
      if (ignored.length || !compatible) {
        const warning = document.createElement('span'); warning.className = 'preset-browser-warning';
        warning.textContent = compatible ? `${ignored.length} unsupported setting${ignored.length === 1 ? '' : 's'} skipped` : 'No compatible adjustments';
        warning.title = compatible ? `Unsupported settings: ${ignored.join(', ')}` : 'This preset cannot be applied.';
        apply.append(warning);
      }
      const photo = snapshot;
      apply.onclick = () => {
        onSelect(preset); select(preset.name);
        onApply(preset, photo);
      };
      const favorite = document.createElement('button');
      favorite.type = 'button'; favorite.className = 'preset-browser-favorite';
      updateFavoriteButton(favorite, preset.name, favorites);
      favorite.onclick = () => {
        const names = [...getFavorites()];
        const updated = names.includes(preset.name) ? names.filter((name) => name !== preset.name) : [...names, preset.name];
        onFavoritesChange(updated);
        if (favoritesOnly) render();
        else updateFavoriteButton(favorite, preset.name, updated);
      };
      article.append(apply, favorite); grid.append(article); cards.set(preset.name, apply);
      if (getPreview && compatible && snapshot?.name) requests.push({ preset: structuredClone(preset), photo, image, previewStatus });
    }
    select(); queue.replace(requests);
  }
  search.addEventListener('input', () => {
    cancel(); page = 0;
    searchTimer = setTimeout(render, 160);
  });
  filterButtons.forEach((button) => { button.onclick = () => { favoritesOnly = button.dataset.filter === 'favorites'; page = 0; render(); }; });
  previous.onclick = () => { page--; render(); };
  next.onclick = () => { page++; render(); };
  return {
    refresh(options = {}) { loading = options.loading ?? loading; render(); },
    setActive(value) { active = !!value; if (active) render(); else cancel(); },
    select,
    destroy() {
      destroyed = true; cancel();
      if (syncManagement) managementSection.removeEventListener('toggle', syncManagement);
      root.remove();
    },
  };
}
