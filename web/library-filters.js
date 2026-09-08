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

export function matchesLibraryFilters(image, types, editState, metadata = {}) {
  if (!matchesMetadataFilters(image, metadata)) return false;
  if (types.length && !types.includes(photoFileType(image))) return false;
  if (editState === 'edited') return photoHasEdits(image);
  if (editState === 'unedited') return !photoHasEdits(image);
  if (editState === 'virtual') return !!image.virtual;
  return true;
}

export const METADATA_FIELDS = {
  camera: 'Camera', lens: 'Lens', keyword: 'Keyword', dateFrom: 'From', dateTo: 'Through',
  isoMin: 'ISO ≥', isoMax: 'ISO ≤', focalLengthMin: 'Focal length ≥', focalLengthMax: 'Focal length ≤',
  apertureMin: 'Aperture ≥', apertureMax: 'Aperture ≤', shutterMin: 'Exposure ≥', shutterMax: 'Exposure ≤',
};
const EXPOSURE = {iso:'iso', focalLength:'focalLength', aperture:'aperture', shutter:'shutterSeconds'};
export function positiveNumber(value) {
  if (value == null || typeof value === 'boolean' || String(value).trim() === '') return null;
  const parts = String(value).trim().split('/');
  const result = parts.length === 2 ? Number(parts[0]) / Number(parts[1]) : parts.length === 1 ? Number(parts[0]) : NaN;
  return Number.isFinite(result) && result > 0 ? result : null;
}
export function cleanMetadataFilters(values) {
  const rules = {};
  for (const key of Object.keys(METADATA_FIELDS)) {
    if (values[key] == null || String(values[key]).trim() === '') continue;
    if (key.endsWith('Min') || key.endsWith('Max')) {
      const value = positiveNumber(values[key]);
      if (value == null) throw new Error(`${METADATA_FIELDS[key]} needs a positive number.`);
      rules[key] = value;
    } else rules[key] = String(values[key]).trim().replace(/\s+/g, ' ').slice(0, 200);
  }
  for (const key of Object.keys(EXPOSURE)) {
    if (rules[key + 'Min'] != null && rules[key + 'Max'] != null && rules[key + 'Min'] > rules[key + 'Max']) {
      throw new Error(`${METADATA_FIELDS[key + 'Min'].replace(' ≥', '')}: minimum must not exceed maximum.`);
    }
  }
  if (rules.dateFrom && rules.dateTo && rules.dateFrom > rules.dateTo) throw new Error('Capture date start must not follow end.');
  return rules;
}
export function matchesMetadataFilters(image, rules) {
  for (const key of ['camera', 'lens']) {
    if (rules[key] && !String(image[key] || '').toLocaleLowerCase().includes(String(rules[key]).toLocaleLowerCase())) return false;
  }
  if (rules.keyword && !(image.keywords || []).some(path => path === rules.keyword || path.startsWith(rules.keyword + ' > '))) return false;
  const captured = String(image.captureTime || image.date || '').slice(0, 10);
  if (rules.dateFrom && (!captured || captured < rules.dateFrom.slice(0, 10))) return false;
  if (rules.dateTo && (!captured || captured > rules.dateTo.slice(0, 10))) return false;
  for (const [field, property] of Object.entries(EXPOSURE)) {
    const value = positiveNumber(image[property]);
    for (const bound of ['Min', 'Max']) {
      if (rules[field + bound] == null) continue;
      const limit = positiveNumber(rules[field + bound]);
      if (value == null || limit == null || (bound === 'Min' ? value < limit : value > limit)) return false;
    }
  }
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
  for (const [key, value] of Object.entries(values.metadata || {})) {
    if (METADATA_FIELDS[key]) chips.push({id: `metadata:${key}`, label: `${METADATA_FIELDS[key]} ${value}${key.startsWith('focalLength') ? ' mm' : key.startsWith('shutter') ? ' s' : ''}`});
  }
  return chips;
}

export function installLibraryFilters({ el, onChange, closeDropdown }) {
  const trigger = el('libraryFilterBtn'), panel = el('libraryFilterPanel');
  const row = el('activeLibraryFilters'), chipsHost = el('libraryFilterChips');
  const defaults = { filter: 'all', ratingFilter: '0', labelFilter: 'all', editFilter: 'all', kindFilter: 'all' };
  let types = [], paintKey = '', metadataRules = {};
  const metadataInputs = [...panel.querySelectorAll('[data-metadata-filter]')];
  const metadata = () => ({...metadataRules});
  function setMetadata(next) {
    try { metadataRules = cleanMetadataFilters(next || {}); } catch { metadataRules = {}; }
    for (const input of metadataInputs) input.value = metadataRules[input.dataset.metadataFilter] ?? '';
    el('metadataFilterError').textContent = '';
  }
  const values = () => Object.fromEntries(Object.keys(defaults).map(id => [id, el(id).value]));

  function setTypes(next) {
    types = normalizeFileTypes(next);
    panel.querySelectorAll('[data-file-type]').forEach(input => { input.checked = types.includes(input.dataset.fileType); });
  }
  function sync() {
    const chips = filterChips({...values(), metadata: metadataRules}, types), key = JSON.stringify(chips);
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
      button.title = `Remove ${chip.label} filter`;
      button.textContent = `${chip.label} ×`;
      button.onclick = () => {
        const index = [...chipsHost.children].indexOf(button);
        if (chip.id.startsWith('type:')) setTypes(types.filter(type => type !== chip.id.slice(5)));
        else if (chip.id.startsWith('metadata:')) {
          const next = metadata(); delete next[chip.id.slice(9)]; setMetadata(next);
        } else el(chip.id).value = defaults[chip.id];
        sync(); onChange();
        (chipsHost.children[Math.min(index, chipsHost.children.length - 1)] || trigger).focus();
      };
      return button;
    }));
  }
  function clear() {
    setTypes([]); setMetadata({});
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
  let metadataTimer;
  function applyMetadata() {
    clearTimeout(metadataTimer);
    try {
      const next = cleanMetadataFilters(Object.fromEntries(metadataInputs.map(input => [input.dataset.metadataFilter, input.value])));
      el('metadataFilterError').textContent = '';
      if (JSON.stringify(next) === JSON.stringify(metadataRules)) return;
      metadataRules = next; sync(); onChange();
    } catch (error) { el('metadataFilterError').textContent = error.message; }
  }
  panel.addEventListener('input', event => {
    if (event.target.matches('[data-metadata-filter]')) {
      clearTimeout(metadataTimer); metadataTimer = setTimeout(applyMetadata, 250);
    }
  });
  panel.addEventListener('toggle', place, true);
  panel.addEventListener('change', event => {
    if (event.target.matches('[data-metadata-filter]')) { applyMetadata(); return; }
    if (!event.target.matches('[data-file-type]')) return;
    setTypes([...panel.querySelectorAll('[data-file-type]:checked')].map(input => input.dataset.fileType));
    sync(); onChange();
  });
  el('clearLibraryFilters').onclick = () => { clear(); panel.querySelector('input').focus(); };
  el('clearActiveLibraryFilters').onclick = () => { clear(); trigger.focus(); };
  el('closeLibraryFilters').onclick = () => close(true);
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); }
    if (event.key === 'Enter' && event.target.matches('[data-metadata-filter]')) { event.preventDefault(); applyMetadata(); }
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
  return { types: () => types, setTypes, metadata, setMetadata, sync, close, clear };
}
