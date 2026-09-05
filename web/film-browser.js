/* Film previews use the actual pipeline on a snapshot, never saved photo edits. */
export function filmParamsForStock(params, stock, profiles) {
  const next = { ...params, stock };
  const film = profiles.find((p) => p.id === stock);
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
    <div class="film-browser-header"><div><strong id="filmBrowserTitle">Preview film stocks</strong><p id="filmBrowserPhoto"></p></div><button type="button" id="filmBrowserClose" aria-label="Close film previews">Close</button></div>
    <label class="film-browser-search">Find a stock<input id="filmBrowserSearch" type="search" placeholder="Search film stocks" autocomplete="off"></label>
    <p class="hint">Previews use this photo’s edits. Choose a stock to apply it.</p>
    <div class="film-preview-grid" id="filmPreviewGrid"></div>
    <div class="film-browser-footer"><button type="button" id="filmBrowserPrevious">Previous</button><span id="filmBrowserPage" role="status"></span><button type="button" id="filmBrowserNext">Next</button></div>
  </section>`;
  document.body.append(overlay);
  const el = (id) => overlay.querySelector(`#${id}`);
  let snapshot, page = 0, generation = 0, controller, returnFocus, searchTimer;
  const urls = new Set();
  function cancel() {
    generation++;
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
  async function render() {
    cancel();
    controller = new AbortController();
    const signal = controller.signal;
    const request = generation;
    const query = el('filmBrowserSearch').value.trim().toLocaleLowerCase();
    const stocks = snapshot.stocks.filter((p) => p.label.toLocaleLowerCase().includes(query));
    page = Math.min(page, Math.max(0, Math.ceil(stocks.length / 4) - 1));
    const shown = stocks.slice(page * 4, page * 4 + 4);
    el('filmBrowserPrevious').disabled = page === 0;
    el('filmBrowserNext').disabled = (page + 1) * 4 >= stocks.length;
    el('filmBrowserPage').textContent = stocks.length ? `${page * 4 + 1}–${page * 4 + shown.length} of ${stocks.length} stocks` : 'No matching stocks';
    const grid = el('filmPreviewGrid');
    grid.replaceChildren();
    const cards = shown.map((stock) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'film-preview-card';
      button.setAttribute('aria-label', `Apply ${stock.label}`);
      button.setAttribute('aria-pressed', String(stock.id === snapshot.state.params.stock && snapshot.state.params.profile_enabled !== false));
      const image = document.createElement('img');
      image.alt = `${stock.label} preview`;
      image.hidden = true;
      const label = document.createElement('strong'); label.textContent = stock.label;
      const status = document.createElement('span'); status.textContent = 'Rendering preview…';
      button.append(image, label, status);
      button.onclick = () => { close(); apply(stock.id, snapshot.name); };
      grid.append(button);
      return { stock, image, status };
    });
    // Bound work to four visible stocks, one render at a time. Closing/searching
    // cancels the browser request and prevents further queued renders.
    for (const { stock, image, status } of cards) {
      if (request !== generation) return;
      try {
        const params = filmParamsForStock(snapshot.state.params, stock.id, snapshot.profiles);
        params.profile_enabled = true;
        const response = await fetch('/api/render/file', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
          body: JSON.stringify({ name: snapshot.name, state: { ...snapshot.state, params }, w: 360, format: 'jpeg', engine: snapshot.engine, client: 'film-stock-browser' }),
        });
        if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) throw new Error('Preview unavailable');
        const blob = await response.blob();
        if (request !== generation) return;
        const url = URL.createObjectURL(blob); urls.add(url);
        image.src = url; image.hidden = false;
        status.textContent = stock.id === snapshot.state.params.stock && snapshot.state.params.profile_enabled !== false ? 'Current stock' : 'Apply stock';
      } catch (error) {
        if (request !== generation || error.name === 'AbortError') return;
        status.textContent = 'Preview unavailable · select to apply';
      }
    }
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
  el('filmBrowserPrevious').onclick = () => { page--; render(); };
  el('filmBrowserNext').onclick = () => { page++; render(); };
  el('filmBrowserSearch').addEventListener('input', () => {
    cancel(); clearTimeout(searchTimer); page = 0;
    searchTimer = setTimeout(render, 180);
  });
  return { open() {
    snapshot = context();
    if (!snapshot?.name) return;
    returnFocus = document.activeElement;
    page = Math.max(0, Math.floor(snapshot.stocks.findIndex((p) => p.id === snapshot.state.params.stock) / 4));
    el('filmBrowserSearch').value = '';
    el('filmBrowserPhoto').textContent = snapshot.label;
    overlay.classList.add('on'); overlay.setAttribute('aria-hidden', 'false');
    el('filmBrowserSearch').focus(); render();
  }, close };
}
