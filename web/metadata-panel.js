/* IPTC, location, and the keyword tree.
 *
 * Exports used to carry no rights information at all, which ruled out client
 * delivery. These fields live in the catalog and are written into exported
 * files when the recipe asks for them; originals are never modified.
 *
 * Keywords gain a hierarchy here. The catalog interns `Parent > Child` paths
 * and filtering on a parent matches its children, so "Places" finds every
 * city under it without anyone tagging twice.
 */

const FIELDS = {
  iptcTitle: 'title',
  iptcCaption: 'caption',
  iptcHeadline: 'headline',
  iptcCreator: 'creator',
  iptcCopyright: 'copyright',
  iptcCredit: 'credit',
  iptcCity: 'city',
  iptcState: 'state',
  iptcCountry: 'country',
};

const GPS_FIELDS = { iptcLat: 'gps_lat', iptcLon: 'gps_lon' };

// Keyword names are typed by people or imported from sidecars written by
// other software, so they are text, never markup.
const escapeHTML = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export function createMetadataPanel(ctx) {
  const { el, post, get, toast } = ctx;
  let currentName = null;
  let saveTimer = null;
  let loading = false;
  let pendingSave = null;
  let saveChain = Promise.resolve();
  let refreshSequence = 0;
  let loadedName = null;

  function coordinates() {
    const latText = String(el('iptcLat')?.value ?? '').trim();
    const lonText = String(el('iptcLon')?.value ?? '').trim();
    if (!latText || !lonText) return null;
    const lat = Number(latText);
    const lon = Number(lonText);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)
        || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    return { lat, lon };
  }

  function canOpenMap() {
    return !loading && Boolean(currentName) && loadedName === currentName
      && Boolean(coordinates());
  }

  function syncMapAvailability() {
    const mapLink = el('iptcMapLink');
    if (mapLink) mapLink.disabled = !canOpenMap();
  }

  function setLoading(value) {
    loading = value;
    for (const id of [...Object.keys(FIELDS), ...Object.keys(GPS_FIELDS)]) {
      if (el(id)) el(id).disabled = value || !currentName;
    }
    syncMapAvailability();
  }

  function flushSave() {
    clearTimeout(saveTimer);
    const snapshot = pendingSave;
    pendingSave = null;
    if (snapshot) {
      // Preserve request order even when an earlier metadata write is slow.
      saveChain = saveChain.then(() => post('/api/metadata', snapshot))
        .then((result) => {
          if (result?.error) throw new Error(result.error);
        })
        .catch(() => toast('Could not save photo metadata'));
    }
    return saveChain;
  }

  function collect() {
    const fields = {};
    Object.entries(FIELDS).forEach(([id, key]) => {
      const input = el(id);
      if (input) fields[key] = input.value.trim() || null;
    });
    Object.entries(GPS_FIELDS).forEach(([id, key]) => {
      const input = el(id);
      if (!input) return;
      const value = input.value.trim();
      fields[key] = value === '' ? null : Number(value);
    });
    return fields;
  }

  function queueSave() {
    syncMapAvailability();
    if (loading || !currentName) return;
    // A later navigation must not change either the destination or the values.
    pendingSave = { name: currentName, fields: collect() };
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, 500);
  }

  async function refresh(name, force = false) {
    const saving = flushSave();
    const request = ++refreshSequence;
    const targetName = name || null;
    currentName = targetName;
    const isVisible = Boolean(el('infoPane')?.classList.contains('on'));
    if ((!isVisible || currentName === loadedName) && !force) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      await saving;
      if (request !== refreshSequence) return;
      if (!targetName) {
        loadedName = null;
        Object.keys(FIELDS).forEach((id) => { if (el(id)) el(id).value = ''; });
        Object.keys(GPS_FIELDS).forEach((id) => {
          if (el(id)) el(id).value = '';
        });
        return;
      }
      const response = await get(
        `/api/metadata?name=${encodeURIComponent(targetName)}`);
      if (request !== refreshSequence) return;
      loadedName = targetName;
      const iptc = (response && response.iptc) || {};
      Object.entries(FIELDS).forEach(([id, key]) => {
        if (el(id)) el(id).value = iptc[key] || '';
      });
      Object.entries(GPS_FIELDS).forEach(([id, key]) => {
        if (el(id)) el(id).value = iptc[key] == null ? '' : iptc[key];
      });
    } catch (error) {
      /* A photo outside the catalog simply has no metadata row yet. */
    } finally {
      if (request === refreshSequence) setLoading(false);
    }
  }

  async function refreshKeywordTree() {
    const container = el('keywordTree');
    if (!container) return;
    try {
      const response = await get('/api/catalog/keywords');
      const keywords = (response && response.keywords) || [];
      const suggestions = el('keywordSuggestions');
      if (suggestions) {
        suggestions.replaceChildren(...keywords.slice(0, 500)
          .map((keyword) => new Option(keyword.path, keyword.path)));
      }
      if (!keywords.length) {
        container.textContent = 'No keywords yet.';
        return;
      }
      container.innerHTML = keywords.map((keyword) => {
        const depth = (keyword.path.match(/ > /g) || []).length;
        return `<button class="keyword-node" type="button"
                  data-path="${encodeURIComponent(keyword.path)}"
                  data-id="${escapeHTML(keyword.id)}"
                  style="--depth:${depth}">
                  <span>${escapeHTML(keyword.name)}</span>
                  <span class="keyword-count">${Number(keyword.count) || 0}</span>
                </button>`;
      }).join('');
    } catch (error) {
      container.textContent = 'Keywords need the catalog.';
    }
  }

  function bind() {
    Object.keys(FIELDS).forEach((id) => {
      const input = el(id);
      if (input) input.addEventListener('input', queueSave);
    });
    Object.keys(GPS_FIELDS).forEach((id) => {
      const input = el(id);
      if (input) input.addEventListener('input', queueSave);
    });

    const mapLink = el('iptcMapLink');
    if (mapLink) {
      mapLink.addEventListener('click', () => {
        if (!canOpenMap()) {
          toast('Add a latitude and longitude first');
          return;
        }
        const { lat, lon } = coordinates();
        window.open(`https://maps.apple.com/?ll=${lat},${lon}&q=Photo`,
                    '_blank', 'noopener');
      });
      syncMapAvailability();
    }

    const applyAll = el('iptcApplyAll');
    if (applyAll) {
      applyAll.addEventListener('click', async () => {
        const names = ctx.selection();
        if (!names.length) { toast('Select photos first'); return; }
        const response = await post('/api/metadata/bulk',
                                    { names, fields: collect() });
        toast(`Metadata applied to ${response.count || 0} photos`);
      });
    }

    const tree = el('keywordTree');
    if (tree) {
      tree.addEventListener('click', (event) => {
        const node = event.target.closest('.keyword-node');
        if (!node) return;
        if (ctx.onKeywordFilter) {
          ctx.onKeywordFilter(decodeURIComponent(node.dataset.path));
        }
      });
      tree.addEventListener('dblclick', async (event) => {
        const node = event.target.closest('.keyword-node');
        if (!node) return;
        const name = await ctx.askName('Rename keyword',
                                       node.querySelector('span').textContent);
        if (!name) return;
        await post('/api/catalog/keywords',
                   { action: 'rename', id: Number(node.dataset.id), name });
        refreshKeywordTree();
      });
    }
  }

  bind();
  return { refresh, refreshKeywordTree };
}
