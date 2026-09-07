export const FILE_TYPES = { raw: 'RAW', jpeg: 'JPEG', heic: 'HEIC / HEIF', tiff: 'TIFF', png: 'PNG' };

export function normalizeFileTypes(value) {
  return Array.isArray(value) ? Object.keys(FILE_TYPES).filter(type => value.includes(type)) : [];
}

export function photoFileType(image) {
  if (image.raw) return 'raw';
  const ext = String(image.sourceName || image.source || image.name || '').split('.').pop().toLowerCase();
  if (['jpg', 'jpeg', 'jpe'].includes(ext)) return 'jpeg';
  if (['heic', 'heif', 'hif'].includes(ext)) return 'heic';
  if (['tif', 'tiff'].includes(ext)) return 'tiff';
  return ext === 'png' ? 'png' : 'other';
}

export function photoHasEdits(image) {
  return !!(image.hasEdits || (image.params && Object.keys(image.params).length)
    || (image.grade && Object.keys(image.grade).length) || image.crop);
}

export function matchesLibraryFilters(image, types, editState) {
  if (types.length && !types.includes(photoFileType(image))) return false;
  if (editState === 'edited') return photoHasEdits(image);
  if (editState === 'unedited') return !photoHasEdits(image);
  if (editState === 'virtual') return !!image.virtual;
  return true;
}

// Keep old saved settings and automation's kind/status values readable. Every
// active restriction, including a legacy one, gets a removable chip.
export function filterChips(values, types) {
  const chips = types.map(type => ({ id: `type:${type}`, label: FILE_TYPES[type] }));
  const flags = { pending: 'Unflagged', approved: 'Picked', skipped: 'Rejected',
    rated: '1★ and up', unrated: 'Unrated', edited: 'Edited', unedited: 'Unedited', virtual: 'Virtual copies' };
  if (flags[values.filter]) chips.push({ id: 'filter', label: flags[values.filter] });
  if (values.ratingFilter === 'unrated') chips.push({ id: 'ratingFilter', label: 'Unrated' });
  else if (+values.ratingFilter > 0) chips.push({ id: 'ratingFilter', label: `${values.ratingFilter}★${+values.ratingFilter < 5 ? ' and up' : ''}` });
  if (values.labelFilter && values.labelFilter !== 'all') {
    const label = values.labelFilter;
    chips.push({ id: 'labelFilter', label: label === 'any' ? 'Any color label'
      : label === 'none' ? 'No color label' : `${label[0].toUpperCase()}${label.slice(1)} label` });
  }
  const edits = { edited: 'Edited', unedited: 'Unedited', virtual: 'Virtual copies' };
  if (edits[values.editFilter]) chips.push({ id: 'editFilter', label: edits[values.editFilter] });
  const kinds = { raw: 'RAW originals', processed: 'Processed files', virtual: 'Virtual copies' };
  if (kinds[values.kindFilter]) chips.push({ id: 'kindFilter', label: kinds[values.kindFilter] });
  return chips;
}

export function installLibraryFilters({ el, onChange, closeDropdown }) {
  const trigger = el('libraryFilterBtn'), panel = el('libraryFilterPanel');
  const row = el('activeLibraryFilters'), chipsHost = el('libraryFilterChips');
  const defaults = { filter: 'all', ratingFilter: '0', labelFilter: 'all', editFilter: 'all', kindFilter: 'all' };
  let types = [], paintKey = '';
  const values = () => Object.fromEntries(Object.keys(defaults).map(id => [id, el(id).value]));

  function setTypes(next) {
    types = normalizeFileTypes(next);
    panel.querySelectorAll('[data-file-type]').forEach(input => { input.checked = types.includes(input.dataset.fileType); });
  }
  function sync() {
    const chips = filterChips(values(), types), key = JSON.stringify(chips);
    if (paintKey === key) return;
    paintKey = key;
    el('libraryFilterLabel').textContent = chips.length ? `Filter · ${chips.length}` : 'Filter';
    trigger.classList.toggle('on', chips.length > 0);
    row.hidden = !chips.length;
    el('clearLibraryFilters').disabled = !chips.length;
    chipsHost.replaceChildren(...chips.map(chip => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'filter-chip';
      button.dataset.filterChip = chip.id;
      button.setAttribute('aria-label', `Remove ${chip.label} filter`);
      button.textContent = `${chip.label} ×`;
      button.onclick = () => {
        const index = [...chipsHost.children].indexOf(button);
        if (chip.id.startsWith('type:')) setTypes(types.filter(type => type !== chip.id.slice(5)));
        else el(chip.id).value = defaults[chip.id];
        sync(); onChange();
        (chipsHost.children[Math.min(index, chipsHost.children.length - 1)] || trigger).focus();
      };
      return button;
    }));
  }
  function clear() {
    setTypes([]);
    for (const [id, value] of Object.entries(defaults)) el(id).value = value;
    sync(); onChange();
  }
  function close(restoreFocus = false) {
    if (panel.hidden) return;
    closeDropdown();
    panel.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) trigger.focus();
  }
  function place() {
    if (panel.hidden) return;
    const rect = trigger.getBoundingClientRect();
    panel.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - panel.offsetWidth - 8))}px`;
    const top = Math.max(8, Math.min(rect.bottom + 6, innerHeight - panel.offsetHeight - 8));
    panel.style.top = `${top}px`;
  }
  trigger.onclick = () => {
    if (!panel.hidden) return close(true);
    closeDropdown(); sync(); panel.hidden = false;
    trigger.setAttribute('aria-expanded', 'true'); place();
    panel.querySelector('input').focus();
  };
  panel.addEventListener('change', event => {
    if (!event.target.matches('[data-file-type]')) return;
    setTypes([...panel.querySelectorAll('[data-file-type]:checked')].map(input => input.dataset.fileType));
    sync(); onChange();
  });
  el('clearLibraryFilters').onclick = () => { clear(); panel.querySelector('input').focus(); };
  el('clearActiveLibraryFilters').onclick = () => { clear(); trigger.focus(); };
  el('closeLibraryFilters').onclick = () => close(true);
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); }
    event.stopPropagation();
  });
  // Buttons must not pass typing/Space through to the photo-marking shortcuts.
  for (const host of [trigger, row]) host.addEventListener('keydown', event => event.stopPropagation());
  function inDropdown(target) {
    const dropdown = panel.querySelector('.dd-trigger[aria-expanded="true"]');
    return dropdown && el(dropdown.getAttribute('aria-controls'))?.contains(target);
  }
  document.addEventListener('pointerdown', event => {
    if (panel.hidden || panel.contains(event.target) || trigger.contains(event.target)) return;
    // Custom selects place their listbox on body, outside this panel.
    if (inDropdown(event.target)) return;
    close();
  }, true);
  document.addEventListener('focusin', event => {
    if (!panel.contains(event.target) && event.target !== trigger && !inDropdown(event.target)) close();
  });
  window.addEventListener('resize', place);
  el('library').addEventListener('scroll', place);
  return { types: () => types, setTypes, sync, close, clear };
}
