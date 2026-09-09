import { t, tn, formatNumber } from '/web/i18n.js';
const html = text => String(text).replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const btn = (text, action, className = 'quiet') => {
  const node = el('button', text, className);
  node.type = 'button'; node.onclick = action;
  return node;
};
const faceImage = (id, className = '') => {
  const image = el('img', null, className);
  image.src = `/api/people/thumbnail?face=${encodeURIComponent(id)}`;
  image.alt = ''; image.loading = 'lazy'; image.decoding = 'async';
  return image;
};
export const personName = group => group?.name || t('Unnamed person');
export const photoCount = group => tn('{count} photo', '{count} photos', group.photoCount);
export const visiblePeople = (groups, view, query = '') => groups.filter(g =>
  (view === 'hidden' ? g.hidden : !g.hidden) && (view !== 'unnamed' || !g.name) &&
  (!query.trim() || personName(g).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));

export function createPeoplePanel({ api, onLabels, onPhoto }) {
  const dialog = el('div', null, 'modal-backdrop people-backdrop');
  dialog.id = 'peopleDialog'; dialog.setAttribute('aria-hidden', 'true');
  dialog.innerHTML = `<section class="people-panel" role="dialog" aria-modal="true" aria-labelledby="peopleTitle">
    <header class="people-header"><div><span class="people-eyebrow">${html(t("LIGHTTABLE / LIBRARY"))}</span><h1 id="peopleTitle">${html(t("People"))}</h1></div><button type="button" class="quiet people-close" aria-label="${html(t("Close People"))}">${html(t("Done"))}</button></header>
    <div class="people-status" role="status" aria-live="polite"><span class="people-dot"></span><span class="people-status-text"></span><progress max="1" value="0" hidden></progress><button type="button" class="quiet compact people-scan">${html(t("Scan new photos"))}</button><button type="button" class="quiet compact people-pause">${html(t("Pause"))}</button></div>
    <nav class="people-nav" aria-label="${html(t("People views"))}"><div class="people-tabs"><button type="button" data-people-view="all">${html(t("Everyone"))}</button><button type="button" data-people-view="unnamed">${html(t("Unnamed"))}</button><button type="button" data-people-view="review">${html(t("Review matches"))}</button><button type="button" data-people-view="hidden">${html(t("Hidden"))}</button></div><input type="search" class="people-search" aria-label="${html(t("Find a person"))}" placeholder="${html(t("Find a person"))}" autocomplete="off"></nav>
    <p class="people-error" role="alert" hidden></p><div class="people-content"></div>
    <footer class="people-footer"><span>${html(t("Names and face data stay on this device."))}</span><button type="button" class="quiet compact people-undo" hidden>${html(t("Undo last change"))}</button><button type="button" class="quiet compact people-delete">${html(t("Delete face data\u2026"))}</button></footer>
  </section>`;
  document.body.append(dialog);
  const find = selector => dialog.querySelector(selector);
  const content = find('.people-content'), error = find('.people-error');
  let status = {}, groups = [], matches = [], view = 'all', groupId = null, members = [];
  let mode = 'faces', selected = new Set(), selecting = false, busy = false, timer = null;
  let pollCount = 0, generation = 0, later = new Set(), mergeTarget = null, oldFocus = null;
  const nameDrafts = new Map();
  const openNow = () => dialog.classList.contains('on');
  const groupNow = () => groups.find(g => g.id === groupId);
  async function get(path) {
    const response = await fetch(`/api/people/${path}`);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || t('People could not be loaded.'));
    return data;
  }
  function fail(exception) { error.textContent = exception.message || String(exception); error.hidden = false; }
  function clearError() { error.hidden = true; error.textContent = ''; }
  function syncStatus() {
    const running = status.running, enabled = status.enabled, caps = status.capabilities || {};
    const summary = [tn('{count} face', '{count} faces', status.faces || 0),
      tn('{count} group', '{count} groups', status.groups || 0)];
    if (!enabled) summary.push(t('Paused'));
    if (status.skipped) summary.push(tn('{count} unavailable photo skipped', '{count} unavailable photos skipped', status.skipped));
    const text = status.lastError ? status.lastError : running
      ? (status.phase === 'preparing' ? t('Preparing face models · {percent}%', {percent: formatNumber(Math.round(100 * (status.downloaded || 0) / (caps.downloadBytes || 1)))})
        : t('Finding faces · {completed} of {total} photos', {completed: formatNumber(status.completed || 0), total: formatNumber(status.total || 0)}))
      : status.faces ? summary.join(' · ') : enabled ? t('Ready to find people') : t('Face matching is off');
    find('.people-status-text').textContent = text;
    find('.people-dot').classList.toggle('active', !!running);
    const progress = find('progress'); progress.hidden = !running;
    progress.value = status.phase === 'preparing' ? (status.downloaded || 0) / (caps.downloadBytes || 1) : (status.completed || 0) / (status.total || 1);
    find('.people-scan').hidden = !status.faces && !enabled;
    find('.people-scan').disabled = !!running;
    find('.people-pause').hidden = !enabled;
    find('.people-undo').hidden = !status.canUndo;
    find('.people-delete').hidden = !status.faces;
    document.getElementById('peopleCount').textContent = status.named ? formatNumber(status.named) : '';
    const toggle = document.getElementById('peopleToggle');
    toggle.classList.toggle('on', !!enabled); toggle.setAttribute('aria-pressed', String(!!enabled));
    toggle.querySelector('.switch-label').textContent = enabled ? t('On') : t('Off');
    document.getElementById('peopleSettingsStatus').textContent = status.faces || running ? text
      : t('Optional · compact model · 10.1 MB download. Photos stay on this device.');
    find('.people-nav').hidden = !groups.length;
    for (const tab of dialog.querySelectorAll('[data-people-view]')) {
      tab.setAttribute('aria-current', String(tab.dataset.peopleView === view && !groupId));
      if (tab.dataset.peopleView === 'unnamed') tab.textContent = groups.length ? t('Unnamed ({count})', {count: formatNumber(groups.filter(g => !g.hidden && !g.name).length)}) : t('Unnamed');
      if (tab.dataset.peopleView === 'review') tab.textContent = matches.length ? t('Review matches ({count})', {count: formatNumber(matches.length)}) : t('Review matches');
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (!status.running) return;
    /* Back off rather than stop. The cap used to end polling after an hour and
     * only an explicit enable, scan, merge or rename reset it, so a long first
     * scan froze the progress line and never showed the names it had found,
     * and reopening the panel could not recover it. */
    pollCount += 1;
    const delay = pollCount > 1800 ? 30000 : 2000;
    timer = setTimeout(async () => {
      try {
        const before = status.faces;
        status = await get('status'); syncStatus();
        if (openNow() && (!status.running || (!groupId && before !== status.faces))) await refresh();
        if (!status.running) await updateLabels();
        schedule();
      } catch (e) { if (openNow()) fail(e); }
    }, delay);
  }
  async function updateLabels() { onLabels(await get('labels')); }
  async function refresh() {
    const token = ++generation;
    const [s, gallery, review] = await Promise.all([get('status'), get('groups'), get('suggestions')]);
    if (token !== generation) return;
    status = s; groups = gallery.groups; matches = review.matches;
    if (groupId && !groupNow()) { groupId = null; selected.clear(); }
    if (groupId) {
      const data = await get(`members?group=${encodeURIComponent(groupId)}`);
      if (token !== generation) return;
      members = data.faces;
      selected = new Set([...selected].filter(id => members.some(f => f.id === id)));
    }
    syncStatus(); render(); schedule();
  }
  async function act(action, data = {}) {
    if (busy) return false;
    busy = true; clearError(); dialog.setAttribute('aria-busy', 'true');
    try {
      const response = await api('/api/people', { action, ...data });
      if (response.error) throw new Error(response.error);
      if (action === 'rename') nameDrafts.delete(data.group);
      if (action === 'clear') nameDrafts.clear();
      status = response; pollCount = 0;
      await refresh(); await updateLabels();
      return true;
    } catch (e) { fail(e); return false; }
    finally { busy = false; dialog.setAttribute('aria-busy', 'false'); }
  }
  async function openGroup(id) {
    groupId = id; selecting = false; selected.clear(); mergeTarget = null; mode = 'faces';
    clearError();
    try { await refresh(); content.scrollTop = 0; find('.people-back')?.focus(); } catch (e) { fail(e); }
  }
  function empty(title, detail) {
    const section = el('div', null, 'people-empty');
    section.append(el('h2', title), el('p', detail)); content.append(section);
    return section;
  }
  function render() {
    const editingName = find('.person-name-form input');
    const restoreNameFocus = editingName && document.activeElement === editingName;
    const caret = restoreNameFocus ? [editingName.selectionStart, editingName.selectionEnd] : null;
    content.replaceChildren();
    if (!groups.length) {
      const box = empty(status.running ? t('Finding familiar faces…') : status.scanComplete ? t('No faces found yet') : t('Find people in your photos'),
        status.running ? t('Groups appear here as photos are scanned. You can keep editing while this runs.')
          : t('Similar faces are grouped together. Add names, review possible matches, and correct a group whenever you need to.'));
      const illustration = el('div', null, 'people-placeholder'); illustration.setAttribute('aria-hidden', 'true');
      illustration.innerHTML = '<svg viewBox="0 0 200 90"><circle cx="45" cy="33" r="16"/><path d="M16 77c0-29 58-29 58 0"/><circle cx="100" cy="25" r="20"/><path d="M64 79c0-38 72-38 72 0"/><circle cx="155" cy="33" r="16"/><path d="M126 77c0-29 58-29 58 0"/></svg>';
      box.prepend(illustration);
      if (!status.running) {
        box.append(btn(status.capabilities?.installed ? t('Enable face matching') : t('Download models & enable'), () => act('enable'), 'accent-btn'));
        box.append(el('small', t('Compact model · 10.1 MB download · on-device processing')));
      }
      return;
    }
    if (groupId) {
      renderGroup();
      if (restoreNameFocus) {
        const input = find('.person-name-form input'); input.focus(); input.setSelectionRange(...caret);
      }
      return;
    }
    if (view === 'review') return renderReview();
    const filtered = visiblePeople(groups, view, find('.people-search').value);
    const heading = el('div', null, 'people-section-heading');
    heading.append(el('h2', view === 'hidden' ? t('Hidden people') : view === 'unnamed' ? t('Add a name') : t('Your people')),
      el('p', view === 'hidden' ? t('Hidden groups stay out of people search. You can bring them back.')
        : view === 'unnamed' ? t('Open a group to name it or combine it with someone you know.') : t('Choose a face to see their photos.')));
    content.append(heading);
    if (!filtered.length) return empty(t('No people here'), find('.people-search').value ? t('Try another name.') : t('Other groups are in Everyone.'));
    const grid = el('div', null, 'people-grid');
    for (const group of filtered) {
      const item = btn(null, () => openGroup(group.id), 'person-tile');
      item.setAttribute('aria-label', t('{name}, {photos}', {name: personName(group), photos: photoCount(group)}));
      item.append(faceImage(group.cover), el('strong', personName(group)), el('span', photoCount(group)));
      grid.append(item);
    }
    content.append(grid);
  }
  function renderGroup() {
    const group = groupNow();
    const back = btn(t('← People'), () => { groupId = null; selected.clear(); syncStatus(); render(); }, 'quiet people-back');
    content.append(back);
    const header = el('div', null, 'person-header');
    header.append(faceImage(group.cover));
    const identity = el('div', null, 'person-identity');
    const form = el('form', null, 'person-name-form');
    const name = el('input'); name.type = 'text'; name.value = nameDrafts.get(group.id) ?? group.name; name.maxLength = 100;
    name.oninput = () => nameDrafts.set(group.id, name.value);
    name.placeholder = t('Add a name'); name.setAttribute('aria-label', t('Person name')); name.autocomplete = 'off';
    const save = btn(t('Save name'), () => {}); save.type = 'submit';
    form.append(name, save);
    form.onsubmit = e => { e.preventDefault(); void act('rename', { group: group.id, name: name.value }); };
    identity.append(form, el('p', [photoCount(group), tn('{count} face', '{count} faces', group.faceCount), group.hidden ? t('Hidden') : ''].filter(Boolean).join(' · ')));
    header.append(identity); content.append(header);
    const toolbar = el('div', null, 'person-toolbar');
    const modes = el('div', null, 'people-tabs');
    for (const [key, title] of [['faces', t('Faces')], ['photos', t('Photos')]]) {
      const button = btn(title, () => { mode = key; selected.clear(); selecting = false; render(); });
      button.setAttribute('aria-current', String(mode === key)); modes.append(button);
    }
    toolbar.append(modes,
      btn(selecting ? t('Cancel selection') : t('Select faces'), () => { mode = 'faces'; selecting = !selecting; selected.clear(); render(); }),
      btn(t('Merge with…'), () => { mergeTarget = ''; render(); }),
      btn(group.hidden ? t('Show person') : t('Hide person'), () => act('hide', { group: group.id, hidden: !group.hidden })));
    content.append(toolbar);
    if (mergeTarget !== null) renderMerge(group);
    if (selecting) {
      const bar = el('div', null, 'people-selection');
      bar.append(el('strong', tn('{count} selected', '{count} selected', selected.size)));
      const split = btn(t('Move to a separate person'), async () => {
        if (await act('split', { group: group.id, faces: [...selected] })) {
          selected.clear(); selecting = false; render();
        }
      }); split.disabled = !selected.size || selected.size === members.length;
      const cover = btn(t('Use as cover'), () => act('cover', { group: group.id, face: [...selected][0] }));
      cover.disabled = selected.size !== 1;
      bar.append(split, cover); content.append(bar);
    }
    const grid = el('div', null, `person-faces ${mode === 'photos' ? 'person-photos' : ''}`);
    const shown = mode === 'photos' ? [...new Map(members.map(f => [f.photo, f])).values()] : members;
    for (const face of shown) {
      const tile = btn(null, () => {
        if (selecting) { selected.has(face.id) ? selected.delete(face.id) : selected.add(face.id); render(); }
        else if (mode === 'faces') { selected = new Set([face.id]); selecting = true; render(); }
        else void openPhoto(face.photo);
      }, `person-face ${selected.has(face.id) ? 'selected' : ''}`);
      tile.setAttribute('aria-label', mode === 'photos' && !selecting ? t('Open photo {name}', {name: face.photo}) : t('Select face in {name}', {name: face.photo}));
      if (selecting) tile.setAttribute('aria-pressed', String(selected.has(face.id)));
      let image;
      if (mode === 'photos') {
        image = el('img'); image.src = `/api/thumb?name=${encodeURIComponent(face.photo)}`; image.alt = ''; image.loading = 'lazy';
      } else image = faceImage(face.id);
      tile.append(image, el('span', face.photo.split('/').at(-1).replace(/^\d+:/, ''), 'person-photo-name'));
      if (selecting) tile.append(el('span', selected.has(face.id) ? '✓' : '', 'people-check'));
      grid.append(tile);
    }
    content.append(grid);
  }
  function renderMerge(group) {
    const panel = el('section', null, 'people-merge');
    panel.append(el('h3', t('Combine with another group')));
    const select = el('select'); select.setAttribute('aria-label', t('Person to merge with'));
    const placeholder = el('option', t('Choose a person')); placeholder.value = ''; select.append(placeholder);
    for (const g of groups.filter(g => g.id !== group.id)) {
      const option = el('option', t('{name} · {photos}', {name: personName(g), photos: photoCount(g)})); option.value = g.id; select.append(option);
    }
    select.value = mergeTarget; select.onchange = () => { mergeTarget = select.value; render(); };
    panel.append(select);
    const target = groups.find(g => g.id === mergeTarget);
    if (target) {
      const preview = el('div', null, 'people-merge-preview');
      preview.append(faceImage(group.cover), el('span', '+'), faceImage(target.cover)); panel.append(preview);
      panel.append(el('p', group.name ? t('Keep the name “{name}”.', {name: group.name}) : target.name ? t('Use the name “{name}”.', {name: target.name}) : t('You can name the combined group next.')));
    }
    const confirm = btn(t('Combine groups'), async () => {
      if (await act('merge', { group: group.id, other: mergeTarget })) { mergeTarget = null; render(); }
    }, 'accent-btn'); confirm.disabled = !target;
    panel.append(confirm, btn(t('Cancel'), () => { mergeTarget = null; render(); })); content.append(panel);
  }
  function renderReview() {
    const pair = matches.find(p => !later.has(`${p.a}/${p.b}`));
    if (!pair) return empty(t('All caught up'), later.size ? t('Skipped matches will be here when you reopen People.') : t('New possible matches appear here after scanning more photos.'));
    const a = groups.find(g => g.id === pair.a), b = groups.find(g => g.id === pair.b);
    if (!a || !b) return;
    const section = el('section', null, 'people-review');
    section.append(el('p', t('POSSIBLE MATCH'), 'people-eyebrow'), el('h2', t('Are these the same person?')),
      el('p', t('Compare the faces before combining their photos.'), 'people-review-intro'));
    const comparison = el('div', null, 'people-comparison');
    for (const group of [a, b]) {
      const side = el('div', null, 'people-comparison-side');
      const cover = btn(null, () => openGroup(group.id), 'people-review-cover'); cover.append(faceImage(group.cover));
      side.append(cover, el('h3', personName(group)), el('p', photoCount(group)));
      const examples = el('div', null, 'people-examples'); side.append(examples);
      get(`members?group=${encodeURIComponent(group.id)}`).then(data => {
        if (!examples.isConnected) return;
        for (const face of data.faces.filter(f => f.id !== group.cover).slice(0, 4)) examples.append(faceImage(face.id));
      }).catch(fail);
      comparison.append(side);
    }
    section.append(comparison);
    const keep = a.name || !b.name ? a : b, other = keep === a ? b : a;
    if (a.name && b.name && a.name !== b.name) section.append(el('p', t('Combining keeps the name “{name}”. You can change it afterward.', {name: keep.name})));
    const actions = el('div', null, 'people-review-actions');
    actions.append(btn(t('Different people'), () => act('reject', { group: a.id, other: b.id })),
      btn(t('Same person'), () => act('merge', { group: keep.id, other: other.id }), 'accent-btn'),
      btn(t('Skip for now'), () => { later.add(`${pair.a}/${pair.b}`); render(); }, 'quiet'));
    section.append(actions, el('small', t('Your corrections are remembered. You can undo the last change.')));
    content.append(section);
  }
  async function openPhoto(name) {
    try { await onPhoto(name); close(); } catch (e) { fail(e); }
  }
  function deletePrompt() {
    content.replaceChildren();
    const section = empty(t('Delete face data?'), t('This removes face groups, names, corrections, and face thumbnails from this catalog. Original photos and the photo contents index are kept. Downloaded models remain available.'));
    section.append(btn(t('Delete face data'), async () => { groupId = null; await act('clear'); }, 'destructive'), btn(t('Cancel'), render));
  }
  async function open() {
    oldFocus = document.activeElement;
    const settings = document.getElementById('settingsDialog');
    settings?.classList.remove('on'); settings?.setAttribute('aria-hidden', 'true');
    dialog.classList.add('on'); dialog.setAttribute('aria-hidden', 'false');
    later.clear(); clearError(); find('.people-close').focus();
    try { await refresh(); } catch (e) { fail(e); }
  }
  function close() {
    dialog.classList.remove('on'); dialog.setAttribute('aria-hidden', 'true');
    generation += 1; if (!status.running) clearTimeout(timer);
    (oldFocus?.getClientRects().length ? oldFocus : document.getElementById('peopleOpen'))?.focus();
  }
  document.getElementById('peopleOpen').onclick = open;
  document.getElementById('peopleSettingsOpen').onclick = open;
  document.getElementById('peopleToggle').onclick = async () => {
    await open(); await act(status.enabled ? 'disable' : 'enable');
  };
  find('.people-close').onclick = close;
  find('.people-pause').onclick = () => act('disable');
  find('.people-scan').onclick = () => act('scan');
  find('.people-undo').onclick = () => act('undo');
  find('.people-delete').onclick = deletePrompt;
  find('.people-search').oninput = () => { groupId = null; view = view === 'review' ? 'all' : view; syncStatus(); render(); };
  for (const tab of dialog.querySelectorAll('[data-people-view]')) tab.onclick = () => {
    view = tab.dataset.peopleView; groupId = null; clearError(); syncStatus(); render(); content.scrollTop = 0;
  };
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    // Native edit shortcuts also see menuTextEditing=true while this workspace
    // is open. Photo ratings/navigation must never fire beneath a face review.
    event.stopPropagation();
  });
  get('status').then(s => { status = s; syncStatus(); schedule(); }).catch(() => {});
  window.addEventListener('pagehide', () => clearTimeout(timer));
  return { open, close };
}
