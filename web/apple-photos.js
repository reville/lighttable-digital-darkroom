// SPDX-License-Identifier: GPL-3.0-only
import { t, formatNumber } from './i18n.js';

export function photosBrowserAvailable(capabilities, platform, bridge) {
  return !!bridge && !['windows', 'linux'].includes(platform)
    && capabilities?.photosBrowserAvailable === true;
}

// Selection survives page/album changes. Pages and thumbnails are tagged so a
// late native reply cannot replace the current album or relabel a selection.
export function installApplePhotosBrowser({ el, sendNative, nativeBridge, onImported, onViewImported }) {
  const dialog = el('applePhotosDialog');
  const opener = el('applePhotosOpen');
  const grid = el('applePhotosGrid');
  const selected = new Set(), cards = new Map();
  let available = false, requestId = 0, offset = 0, total = 0, hasMore = false;
  let items = [], loading = false, importing = false, registering = false;
  let returnFocus = null, importedPath = '', lastImportId = null;
  let pageTimer = null;
  const inertElements = new Map();
  const isOpen = () => dialog.classList.contains('on');
  const status = message => { el('applePhotosStatus').textContent = message; };

  function sync() {
    el('applePhotosPrevious').disabled = loading || importing || offset === 0;
    el('applePhotosNext').disabled = loading || importing || !hasMore;
    el('applePhotosAlbum').disabled = loading || importing;
    el('applePhotosRefresh').disabled = importing;
    el('applePhotosSelectPage').disabled = loading || importing || !items.length;
    el('applePhotosClear').disabled = importing || !selected.size;
    el('applePhotosImport').disabled = !selected.size || importing || registering;
    el('applePhotosStop').hidden = !importing;
    el('applePhotosClose').disabled = importing || registering;
    el('applePhotosViewImported').hidden = !importedPath;
    el('applePhotosViewImported').disabled = importing || registering;
    el('applePhotosSelection').textContent = t('Selected: {count}', {count: formatNumber(selected.size)});
    el('applePhotosPage').textContent = total ? t('{start}–{end} of {total}', {
      start: formatNumber(offset + 1), end: formatNumber(offset + items.length), total: formatNumber(total),
    }) : '';
    grid.setAttribute('aria-busy', String(loading));
    for (const [id, card] of cards) {
      card.setAttribute('aria-pressed', String(selected.has(id)));
      card.disabled = importing;
    }
  }

  function request(includeAlbums = false) {
    if (!available || !isOpen() || importing) return;
    clearTimeout(pageTimer);
    loading = true;
    items = []; total = 0; hasMore = false; cards.clear(); grid.replaceChildren();
    status(t('Reading your Photos library…'));
    const token = ++requestId;
    sendNative('browseApplePhotos', {requestId: token, album: el('applePhotosAlbum').value,
      offset, includeAlbums});
    pageTimer = setTimeout(() => {
      if (token !== requestId || !loading) return;
      loading = false;
      status(t('Photos is taking longer than expected. Check Photos access and try Refresh.'));
      sync();
    }, 30000);
    sync();
  }

  function close() {
    if (importing || registering) return;
    clearTimeout(pageTimer);
    ++requestId;
    sendNative('closeApplePhotos');
    dialog.classList.remove('on');
    dialog.setAttribute('aria-hidden', 'true');
    opener.setAttribute('aria-expanded', 'false');
    for (const [node, inert] of inertElements) node.inert = inert;
    inertElements.clear();
    grid.replaceChildren(); cards.clear(); items = [];
    returnFocus?.focus?.({preventScroll: true});
  }

  function open() {
    if (!available || isOpen()) return;
    returnFocus = document.activeElement;
    dialog.classList.add('on');
    dialog.setAttribute('aria-hidden', 'false');
    opener.setAttribute('aria-expanded', 'true');
    for (const node of document.body.children) {
      if (node === dialog || node.tagName === 'SCRIPT') continue;
      inertElements.set(node, node.inert); node.inert = true;
    }
    el('applePhotosTitle').focus();
    request(true);
  }

  function page(event) {
    if (!isOpen() || event.requestId !== requestId) return;
    clearTimeout(pageTimer); loading = false;
    if (event.error) { status(event.error); sync(); return; }
    if (Array.isArray(event.albums)) {
      const current = el('applePhotosAlbum').value;
      const options = [{id: '', name: t('All Photos')}, ...event.albums].map(album => {
        const option = document.createElement('option');
        option.value = album.id; option.textContent = album.name; return option;
      });
      el('applePhotosAlbum').replaceChildren(...options);
      el('applePhotosAlbum').value = options.some(option => option.value === current) ? current : '';
    }
    items = event.items || []; offset = event.offset || 0; total = event.total || 0; hasMore = !!event.hasMore;
    for (const item of items) {
      const card = document.createElement('button');
      card.type = 'button'; card.className = 'apple-photos-card';
      card.setAttribute('aria-label', item.name);
      const preview = document.createElement('span'); preview.className = 'apple-photos-preview';
      const placeholder = document.createElement('span'); placeholder.textContent = t('Preview unavailable');
      const img = document.createElement('img'); img.alt = ''; img.hidden = true;
      preview.append(placeholder, img);
      const name = document.createElement('span'); name.className = 'apple-photos-name'; name.textContent = item.name;
      card.append(preview, name);
      card.onclick = () => {
        if (importing) return;
        if (selected.has(item.id)) selected.delete(item.id);
        else if (selected.size < 500) selected.add(item.id);
        else status(t('Select up to 500 photos per import.'));
        sync();
      };
      cards.set(item.id, card); grid.append(card);
    }
    status(items.length ? '' : t('No photos in this album.'));
    sync();
  }

  async function importEvent(event) {
    importing = event.state === 'running';
    const progress = el('applePhotosProgress');
    progress.hidden = !importing;
    progress.max = Math.max(1, event.total || 1);
    progress.value = (event.completed || 0) + (event.resourceProgress || 0);
    if (importing) {
      status(t('{message} {completed} / {total}', {message: event.message || t('Importing originals…'),
        completed: formatNumber(event.completed || 0), total: formatNumber(event.total || 0)}));
    } else {
      status(event.message || t('Import stopped'));
      if (event.importId && event.importId !== lastImportId
          && (event.imported || event.existing) && event.path) {
        lastImportId = event.importId;
        registering = true;
        sync();
        try {
          await onImported(event.path);
          importedPath = event.path;
          // Keep a partial selection for an explicit retry; deterministic
          // destination paths skip copies that already completed.
          if (event.state === 'completed' && !event.failures) selected.clear();
          status(t('Imported: {imported} · Already added: {existing} · Unavailable: {failures}', {
            imported: formatNumber(event.imported || 0), existing: formatNumber(event.existing || 0),
            failures: formatNumber(event.failures || 0),
          }));
        } catch (error) {
          lastImportId = null;
          status(error.message);
        } finally { registering = false; }
      }
    }
    el('applePhotosStop').disabled = false;
    sync();
  }

  opener.onclick = open;
  el('applePhotosClose').onclick = close;
  el('applePhotosAlbum').onchange = () => { offset = 0; request(); };
  el('applePhotosRefresh').onclick = () => { offset = 0; el('applePhotosAlbum').value = ''; request(true); };
  el('applePhotosPrevious').onclick = () => { offset = Math.max(0, offset - 60); request(); };
  el('applePhotosNext').onclick = () => { offset += 60; request(); };
  el('applePhotosSelectPage').onclick = () => {
    for (const item of items) if (selected.size < 500) selected.add(item.id);
    if (items.some(item => !selected.has(item.id))) status(t('Select up to 500 photos per import.'));
    sync();
  };
  el('applePhotosClear').onclick = () => { selected.clear(); sync(); };
  el('applePhotosImport').onclick = () => {
    if (!available || !selected.size || importing || registering) return;
    importing = true; status(t('Importing originals…')); sync();
    sendNative('importBrowsedApplePhotos', {assetIds: [...selected]});
  };
  el('applePhotosStop').onclick = () => {
    sendNative('cancelBrowsedApplePhotosImport');
    el('applePhotosStop').disabled = true; status(t('Stopping import…'));
  };
  el('applePhotosViewImported').onclick = async () => {
    if (importing || registering || !importedPath) return;
    try { await onViewImported(importedPath); close(); }
    catch (error) { status(error.message); }
  };
  dialog.addEventListener('keydown', event => {
    if (!isOpen()) return;
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const targets = [...dialog.querySelectorAll('button, select, [tabindex="0"]')]
        .filter(node => !node.disabled && node.getClientRects().length);
      const index = targets.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); targets.at(-1)?.focus(); }
      else if (!event.shiftKey && (index < 0 || index === targets.length - 1)) {
        event.preventDefault(); targets[0]?.focus();
      }
    }
  });
  sync();
  return {
    open, close,
    nativeEvent(event) {
      if (event?.type === 'sources') {
        available = photosBrowserAvailable(event, window.__LIGHTTABLE_PLATFORM__, nativeBridge());
        opener.hidden = !available;
        el('importPhotosBtn').hidden = !available;
      } else if (event?.type === 'applePhotosPage') page(event);
      else if (event?.type === 'applePhotosThumbnail' && isOpen() && event.requestId === requestId) {
        const card = cards.get(event.id);
        if (!card || !/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(event.data || '')) return;
        const img = card.querySelector('img'); img.src = event.data; img.hidden = false;
        card.querySelector('.apple-photos-preview > span').hidden = true;
      } else if (event?.type === 'applePhotosChanged' && isOpen() && !importing) {
        status(t('Your Photos library changed. Refresh to see the latest photos and albums.'));
      } else if (event?.type === 'applePhotosImport') void importEvent(event);
    },
  };
}
