// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';

/** Organization is a preference; it never changes or duplicates a recipe. */
export function normalizePresetPacks(value = {}) {
  const seen = new Set();
  const packs = (Array.isArray(value?.packs) ? value.packs : []).filter(pack => {
    if (!pack || typeof pack.id !== 'string' || !pack.id.startsWith('pack-') ||
        seen.has(pack.id) || typeof pack.name !== 'string' || !pack.name.trim() ||
        !['builtin', 'yours', 'community'].includes(pack.collection)) return false;
    seen.add(pack.id); return true;
  }).map(pack => ({ id: pack.id, name: pack.name.trim().slice(0, 80), collection: pack.collection }));
  const assignments = Object.fromEntries(Object.entries(value?.assignments || {})
    .filter(([, id]) => seen.has(id)));
  const collapsed = [...new Set((Array.isArray(value?.collapsed) ? value.collapsed : [])
    .filter(id => typeof id === 'string'))];
  return { packs, assignments, collapsed };
}

export function defaultPresetPack(preset) {
  const collection = preset.collection || 'yours';
  if (collection === 'builtin') {
    if (preset.tags?.includes('Film Simulation Drama')) return { id: 'builtin-film-drama', name: 'Film Simulation Drama' };
    if (preset.tags?.some(tag => ['B&W', 'Black & White'].includes(tag))) return { id: 'builtin-bw', name: tr('Black & White') };
    if (preset.tags?.some(tag => ['Film', 'Vintage'].includes(tag))) return { id: 'builtin-film', name: tr('Film') };
    return { id: 'builtin-color', name: tr('Color') };
  }
  if (collection === 'applied') return { id: 'applied', name: tr('Saved with this photo.') };
  if (collection === 'community') {
    const author = preset.author?.name || 'LightTable';
    return { id: `community-${author}`, name: author };
  }
  return { id: 'yours', name: tr('Yours') };
}

export function groupPresetPacks(presets, organization, collection, { includeEmpty = true } = {}) {
  const state = normalizePresetPacks(organization);
  const groups = new Map();
  const custom = new Map(state.packs.filter(pack => pack.collection === collection).map(pack => [pack.id, pack]));
  for (const pack of custom.values()) groups.set(pack.id, { ...pack, custom: true, presets: [] });
  for (const preset of presets) {
    const assigned = state.assignments[preset.id || preset.name];
    const pack = custom.get(assigned) || defaultPresetPack(preset);
    if (!groups.has(pack.id)) groups.set(pack.id, { ...pack, presets: [] });
    groups.get(pack.id).presets.push(preset);
  }
  const builtInOrder = ['builtin-color', 'builtin-film', 'builtin-film-drama', 'builtin-bw'];
  return [...groups.values()].filter(pack => includeEmpty || pack.presets.length)
    .sort((a, b) => {
      if (a.custom || b.custom || collection !== 'builtin') return Number(!!b.custom) - Number(!!a.custom);
      return builtInOrder.indexOf(a.id) - builtInOrder.indexOf(b.id);
    });
}

export function movePresetToPack(organization, preset, packId) {
  const state = normalizePresetPacks(organization);
  const key = preset.id || preset.name;
  if (!packId) delete state.assignments[key];
  else if (state.packs.some(pack => pack.id === packId && pack.collection === (preset.collection || 'yours'))) {
    state.assignments = { ...state.assignments, [key]: packId };
  }
  return state;
}
