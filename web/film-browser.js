// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';

const i18nHTML = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll("\"", "&quot;").replaceAll("'", "&#39;");

// Menu values identify a rendering variant; saved stock IDs always identify the
// original emulsion, so paper, grain and development metadata remain shared.
export function filmChoiceValue(params = {}) {
  return params.film_tuning && params.film_tuning !== 'original'
    ? `${params.stock}::${params.film_tuning}::${params.film_tuning_version || '1'}`
    : params.stock;
}

export function normalizeFilmTuning(params = {}, profiles = []) {
  const film = profiles.find((profile) => profile.id === params.stock);
  const version = String(params.film_tuning_version || '1');
  const tuning = film?.tunings?.find((item) =>
    item.id === params.film_tuning && String(item.version) === version);
  return { ...params, film_tuning: tuning?.id || 'original',
    film_tuning_version: tuning ? version : '1' };
}

export function filmSelectionForChoice(choice, profiles = []) {
  const [stock, film_tuning = 'original', film_tuning_version = '1'] = String(choice || '').split('::');
  return normalizeFilmTuning({ stock, film_tuning, film_tuning_version }, profiles);
}

export function mergeFilmTuning(base, overlay = {}, profiles = []) {
  const merged = { ...base, ...overlay };
  // Applying a stock from an older preset must not inherit a tuned variant
  // from the destination photo, even when both name the same emulsion.
  if (Object.hasOwn(overlay, 'stock') && !Object.hasOwn(overlay, 'film_tuning')) {
    merged.film_tuning = 'original';
    merged.film_tuning_version = '1';
  } else if (Object.hasOwn(overlay, 'film_tuning') &&
      !Object.hasOwn(overlay, 'film_tuning_version')) {
    merged.film_tuning_version = '1';
  }
  return normalizeFilmTuning(merged, profiles);
}

export function filmStockGroups(profiles = []) {
  const films = profiles.filter((profile) => profile.stage === 'filming');
  const tuned = films.flatMap((profile) => (profile.tunings || [])
    .filter((tuning) => tuning.id === 'lighttable')
    .map((tuning) => ({
      id: filmChoiceValue({ stock: profile.id, film_tuning: tuning.id, film_tuning_version: tuning.version }),
      label: `${profile.name} · ${tr("LightTable tuned")}`,
      description: tuning.description || '', rustOnly: profile.rustOnly,
    })));
  const original = films.map((profile) => ({ id: profile.id,
    label: `${profile.name} · ${tr("Spektrafilm original")}`,
    rustOnly: profile.rustOnly,
  }));
  return [
    { label: tr("LightTable tuned"), options: tuned },
    { label: tr("Spektrafilm original"), options: original },
  ].filter((group) => group.options.length);
}

/* Film previews use the actual pipeline on a snapshot, never saved photo edits. */
export function filmParamsForStock(params, choice, profiles) {
  const next = { ...params, ...filmSelectionForChoice(choice, profiles) };
  const film = profiles.find((p) => p.id === next.stock);
  next.development_time = film?.defaultDevelopmentTime || 0;
  if (Number.isFinite(film?.defaultExposureEv)) next.exposure_ev = film.defaultExposureEv;
  if (next.workflow_mode === 'authentic' && !next.paper_locked && film?.targetPrint) next.paper = film.targetPrint;
  const papers = profiles.filter((p) => p.stage === 'printing' && (!film || p.channelModel === film.channelModel));
  if (!papers.some((p) => p.id === next.paper)) {
    next.paper = papers.find((p) => p.id === film?.targetPrint)?.id || papers[0]?.id || next.paper;
  }
  next.print_development_time = profiles.find((p) => p.id === next.paper)?.defaultDevelopmentTime || 0;
  return next;
}

export function createFilmBrowser({ context, apply }) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-backdrop';
  overlay.id = 'filmBrowserDialog';
  overlay.setAttribute('aria-hidden', 'true');
  overlay.innerHTML = `<section class="modal film-browser" role="dialog" aria-modal="true" aria-labelledby="filmBrowserTitle">
    <div class="film-browser-header"><div><strong id="filmBrowserTitle">${i18nHTML(tr("Preview film stocks"))}</strong><p id="filmBrowserPhoto"></p></div><button type="button" id="filmBrowserClose" aria-label="${i18nHTML(tr("Close film previews"))}">${i18nHTML(tr("Close"))}</button></div>
    <label class="film-browser-search">${i18nHTML(tr("Find a stock"))}<input id="filmBrowserSearch" type="search" placeholder="${i18nHTML(tr("Search film stocks"))}" autocomplete="off"></label>
    <p class="hint">${i18nHTML(tr("Previews use this photo’s edits. Choose a stock to apply it."))}</p>
    <p id="filmBrowserEmpty" class="hint" role="status" hidden>${i18nHTML(tr("No matching stocks"))}</p>
    <div class="film-preview-grid" id="filmPreviewGrid"></div>
  </section>`;
  document.body.append(overlay);
  const el = (id) => overlay.querySelector(`#${id}`);
  const grid = el('filmPreviewGrid');
  let snapshot, generation = 0, controller, observer, returnFocus, searchTimer;
  const urls = new Set();
  function cancel() {
    generation++;
    observer?.disconnect();
    controller?.abort();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
  }
  function close() {
    clearTimeout(searchTimer);
    cancel();
    overlay.classList.remove('on');
    overlay.setAttribute('aria-hidden', 'true');
    returnFocus?.focus();
  }
  function render(scrollToCurrent = false) {
    cancel();
    controller = new AbortController();
    const signal = controller.signal;
    const request = generation;
    const query = el('filmBrowserSearch').value.trim().toLocaleLowerCase();
    const stocks = snapshot.stocks.filter((p) => p.label.toLocaleLowerCase().includes(query));
    el('filmBrowserEmpty').hidden = stocks.length > 0;
    grid.replaceChildren();
    grid.scrollTop = 0;
    const cards = stocks.map((stock) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'film-preview-card';
      button.setAttribute('aria-label', tr("Apply {stockLabel}", {stockLabel: stock.label}));
      button.setAttribute('aria-pressed', String(stock.id === filmChoiceValue(snapshot.state.params) && snapshot.state.params.profile_enabled !== false));
      const image = document.createElement('img');
      image.alt = tr("{stockLabel} preview", {stockLabel: stock.label});
      image.hidden = true;
      const frame = document.createElement('div');
      frame.className = 'film-preview-image';
      frame.append(image);
      const label = document.createElement('strong'); label.textContent = stock.label;
      const status = document.createElement('span'); status.textContent = tr("Rendering preview…");
      button.append(frame, label, status);
      button.onclick = () => { close(); apply(stock.id, snapshot.name); };
      grid.append(button);
      return { stock, button, frame, image, status, near: false, started: false };
    });
    // Keep every stock reachable by scrolling or keyboard, but only render cards
    // near the viewport. Leaving the viewport removes unstarted work from the
    // queue; closing/searching cancels the request and discards stale results.
    let rendering = false;
    async function renderVisible() {
      if (rendering || request !== generation) return;
      rendering = true;
      let card;
      while (request === generation && (card = cards.find((item) => item.near && !item.started))) {
        card.started = true;
        const { stock, frame, image, status } = card;
        const width = Math.min(1600, Math.max(720, Math.ceil(
          Math.max(frame.clientWidth, frame.clientHeight) * Math.min(window.devicePixelRatio || 1, 2) * 1.2,
        )));
        try {
          const params = filmParamsForStock(snapshot.state.params, stock.id, snapshot.profiles);
          params.profile_enabled = true;
          const response = await fetch('/api/render/file', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
            body: JSON.stringify({ name: snapshot.name, state: { ...snapshot.state, params }, w: width, format: 'jpeg', engine: snapshot.engine, client: 'film-stock-browser' }),
          });
          if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) throw new Error(tr("Preview unavailable"));
          const blob = await response.blob();
          if (request !== generation) return;
          const url = URL.createObjectURL(blob); urls.add(url);
          image.src = url; image.hidden = false;
          status.textContent = stock.id === filmChoiceValue(snapshot.state.params) && snapshot.state.params.profile_enabled !== false ? tr("Current stock") : tr("Apply stock");
        } catch (error) {
          if (request !== generation || error.name === 'AbortError') return;
          status.textContent = tr("Preview unavailable · select to apply");
        }
      }
      rendering = false;
    }
    const byButton = new Map(cards.map((card) => [card.button, card]));
    observer = new IntersectionObserver((entries) => {
      if (request !== generation) return;
      for (const entry of entries) byButton.get(entry.target).near = entry.isIntersecting;
      renderVisible();
    }, { root: grid, rootMargin: '240px 0px' });
    if (scrollToCurrent) {
      const current = cards.find((card) => card.button.getAttribute('aria-pressed') === 'true');
      if (current) grid.scrollTop = current.button.offsetTop;
    }
    for (const { button } of cards) observer.observe(button);
  }
  el('filmBrowserClose').onclick = close;
  overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
  overlay.addEventListener('keydown', (event) => {
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const controls = [...overlay.querySelectorAll('button:not(:disabled), input')].filter((e) => e.offsetParent !== null);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  el('filmBrowserSearch').addEventListener('input', () => {
    cancel(); clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 180);
  });
  return { open() {
    snapshot = context();
    if (!snapshot?.name) return;
    returnFocus = document.activeElement;
    el('filmBrowserSearch').value = '';
    el('filmBrowserPhoto').textContent = snapshot.label;
    overlay.classList.add('on'); overlay.setAttribute('aria-hidden', 'false');
    el('filmBrowserSearch').focus(); render(true);
  }, close };
}
