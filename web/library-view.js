// SPDX-License-Identifier: GPL-3.0-only
/* A server-ordered view of the catalog, paged on demand and bounded in memory.
 *
 * The grid used to page the whole catalog into the browser and filter, sort
 * and collapse it in JavaScript. Here the server owns the order: the view
 * knows how many photos match and holds only the pages the grid, filmstrip or
 * navigation have asked for, dropping the least recently used ones once the
 * cache is full. `list` is a sparse array of the view's length whose loaded
 * slots hold the host's image objects, so consumers that only need loaded
 * rows can keep using ordinary array methods that skip holes.
 *
 * Spec changes are debounced and generation-stamped: a response for a spec
 * the user has already typed past is dropped, and the previous list stays on
 * screen until the new one arrives so a filter change never blocks input. */

export const VIEW_PAGE_SIZE = 200;
export const VIEW_PAGE_LIMIT = 40;
export const NAMES_PAGE_LIMIT = 100000;

export function specKey(spec) {
  return JSON.stringify(spec ?? null);
}

export function createLibraryView({
  query, materialize = (item) => item, onChange = () => {}, onError = () => {},
  pageSize = VIEW_PAGE_SIZE, pageLimit = VIEW_PAGE_LIMIT, debounce = 120,
  timers = globalThis,
} = {}) {
  let spec = null, key = '', total = 0, list = [], ready = false, pending = false;
  let generation = 0, revision = 0, timer = null, touch = 0;
  const pages = new Map();      // page index -> { touched }
  const inFlight = new Map();   // page index -> Promise
  const positions = new Map();  // image name -> index in the view
  const hot = new Map();        // renderer channel -> pages it last asked for
  let resolvers = [];

  const pageOf = (index) => Math.floor(index / pageSize);
  const hotPages = () => new Set([...hot.values()].flatMap((set) => [...set]));

  function settle(result) {
    const waiting = resolvers;
    resolvers = [];
    for (const resolve of waiting) resolve(result);
  }

  function install(offset, items) {
    const images = [];
    for (let i = 0; i < items.length; i++) {
      const index = offset + i;
      if (index >= total) break;
      const image = materialize(items[i]);
      const previous = list[index];
      if (previous && previous !== image) positions.delete(previous.name);
      list[index] = image;
      positions.set(image.name, index);
      images.push(image);
    }
    return images;
  }

  function evict(keepName) {
    const evicted = [];
    if (pages.size <= pageLimit) return evicted;
    const keepPage = keepName != null && positions.has(keepName) ? pageOf(positions.get(keepName)) : -1;
    const protectedPages = hotPages();
    const candidates = [...pages.entries()]
      .filter(([page]) => !protectedPages.has(page) && page !== keepPage)
      .sort((a, b) => a[1].touched - b[1].touched);
    while (pages.size > pageLimit && candidates.length) {
      const [page] = candidates.shift();
      pages.delete(page);
      const start = page * pageSize, end = Math.min(total, start + pageSize);
      for (let index = start; index < end; index++) {
        const image = list[index];
        if (!image) continue;
        positions.delete(image.name);
        delete list[index];
        evicted.push(image);
      }
    }
    if (evicted.length) revision++;
    return evicted;
  }

  function fetchPage(page, extra = {}) {
    if (pages.has(page) && !extra.locate) return Promise.resolve(null);
    if (inFlight.has(page) && !extra.locate) return inFlight.get(page);
    const gen = generation, requested = spec;
    const offset = page * pageSize;
    const promise = Promise.resolve(query({ ...requested, offset, limit: pageSize, ...extra }))
      .then((response) => {
        if (gen !== generation) return null;
        if (!response || response.error || !Array.isArray(response.items)) {
          throw new Error(response?.error || 'Could not load photos');
        }
        const count = Number.isFinite(+response.total) ? +response.total : total;
        if (ready && count !== total) {
          // The ordering moved under us (a mark changed membership between
          // requests). Positions are stale; start the view over.
          refresh();
          return null;
        }
        if (!ready) {
          total = count;
          list = new Array(total);
          positions.clear();
          pages.clear();
          ready = true;
        }
        pages.set(page, { touched: ++touch });
        install(offset, response.items);
        revision++;
        onChange({ page, located: response.located ?? null });
        return response;
      })
      .finally(() => { if (inFlight.get(page) === promise) inFlight.delete(page); });
    inFlight.set(page, promise);
    return promise;
  }

  /* Run the current spec from scratch: page 0 plus, when asked, the page that
   * holds `locate`, and the pages the renderers last asked for so a refresh
   * after a mark does not blank the rows already on screen. Resolves with the
   * total and the located index, or null when a newer request superseded it. */
  function run({ locate = null } = {}) {
    const gen = ++generation;
    const wanted = [...hotPages()];
    // The old list stays on screen until page 0 answers; `ready` is off so
    // no renderer can fetch old-list pages with the new spec meanwhile.
    pending = true;
    ready = false;
    pages.clear();
    inFlight.clear();
    return fetchPage(0, locate ? { locate } : {}).then(async (response) => {
      if (gen !== generation || !response) return null;
      const located = Number.isInteger(response.located) ? response.located : null;
      const extra = new Set(wanted.filter((page) => page * pageSize < total));
      if (located != null) extra.add(pageOf(located));
      extra.delete(0);
      await Promise.all([...extra].slice(0, 8).map((page) => fetchPage(page)));
      if (gen !== generation) return null;
      pending = false;
      const result = { total, located };
      settle(result);
      return result;
    }).catch((error) => {
      if (gen !== generation) return null;
      pending = false;
      onError(error);
      settle(null);
      return null;
    });
  }

  function refresh(options = {}) {
    if (spec === null) return Promise.resolve(null);
    timers.clearTimeout(timer);
    timer = null;
    return run(options);
  }

  function setSpec(next, { immediate = false, locate = null } = {}) {
    const nextKey = specKey(next);
    if (nextKey === key && ready && !pending) {
      return Promise.resolve({ total, located: positions.get(locate) ?? null });
    }
    spec = next;
    key = nextKey;
    timers.clearTimeout(timer);
    timer = null;
    generation++;
    pending = true;
    ready = false;
    settle(null);
    if (immediate || !debounce) return run({ locate });
    return new Promise((resolve) => {
      resolvers.push(resolve);
      timer = timers.setTimeout(() => { timer = null; run({ locate }); }, debounce);
    });
  }

  /* Load the pages covering [start, end). Each renderer names its channel so
   * the grid's and the filmstrip's windows are both protected from eviction. */
  function ensureRange(start, end, channel = 'grid') {
    if (!ready || !total) return Promise.resolve([]);
    const first = pageOf(Math.max(0, start)), last = pageOf(Math.min(total, Math.max(start + 1, end)) - 1);
    const wanted = new Set();
    hot.set(channel, wanted);
    const loads = [];
    for (let page = first; page <= last; page++) {
      wanted.add(page);
      const entry = pages.get(page);
      if (entry) entry.touched = ++touch;
      else loads.push(fetchPage(page).catch((error) => { onError(error); return null; }));
    }
    return Promise.all(loads);
  }

  function ensureIndex(index) {
    if (!ready || index < 0 || index >= total) return Promise.resolve(undefined);
    if (list[index]) return Promise.resolve(list[index]);
    return fetchPage(pageOf(index)).then(() => list[index]).catch((error) => { onError(error); return undefined; });
  }

  /* Every name in the view, in order, without loading rows: for Select All
   * and whole-view actions. Bounded by the server's names page. */
  async function allNames(extra = {}) {
    const names = [];
    const gen = generation;
    for (let offset = 0; ; offset += NAMES_PAGE_LIMIT) {
      const page = await query({ ...spec, ...extra, namesOnly: true, offset, limit: NAMES_PAGE_LIMIT });
      if (gen !== generation) return null;
      if (!page || page.error || !Array.isArray(page.names)) throw new Error(page?.error || 'Could not list photos');
      names.push(...page.names);
      if (!page.names.length || names.length >= +page.total) break;
    }
    return names;
  }

  /* Where one photo sits in the current view, or null. */
  async function locate(name) {
    if (!spec) return null;
    const gen = generation;
    const page = await query({ ...spec, offset: 0, limit: 1, idsOnly: true, locate: name });
    if (gen !== generation || !page || page.error) return null;
    return Number.isInteger(page.located) ? page.located : null;
  }

  function invalidate() {
    generation++;
    timers.clearTimeout(timer);
    timer = null;
    pages.clear();
    inFlight.clear();
    positions.clear();
    hot.clear();
    list = [];
    total = 0;
    ready = false;
    pending = false;
    revision++;
    settle(null);
  }

  return {
    get list() { return list; },
    get total() { return total; },
    get ready() { return ready; },
    get pending() { return pending; },
    get spec() { return spec; },
    get key() { return key; },
    get revision() { return revision; },
    get generation() { return generation; },
    get loadedCount() { return positions.size; },
    get pageCount() { return pages.size; },
    isLoaded(index) { return !!list[index]; },
    indexOf(name) { return positions.has(name) ? positions.get(name) : -1; },
    setSpec, refresh, ensureRange, ensureIndex, evict, allNames, locate, invalidate,
  };
}
