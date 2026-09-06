/** RAW+JPEG pairs share a source and folder. Virtual copies remain independent. */
export function pairKey(image) {
  if (!image || image.virtual || (!image.raw && !/\.jpe?g$/i.test(image.sourceName || image.name || ''))) return null;
  const name = String(image.name || '');
  return name.replace(/\.[^./]+$/, '').toLowerCase();
}
export function indexPairs(images) {
  const groups = new Map(), byName = new Map();
  for (const image of images) {
    const key = pairKey(image);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(image);
  }
  for (const members of groups.values()) {
    if (members.length !== 2 || members.filter(image => image.raw).length !== 1) continue;
    for (const image of members) byName.set(image.name, members);
  }
  return byName;
}
export function pairViewPreference(prefs) {
  if (prefs.pairRawJPEG === false) return 'both';
  return ['raw', 'jpeg', 'both'].includes(prefs.pairView) ? prefs.pairView
    : prefs.hidePairedJPEG ? 'raw' : 'both';
}
export function collapsePairs(filtered, view, overrides = new Map()) {
  if (view === 'both') return filtered;
  const pairs = indexPairs(filtered), hidden = new Set();
  for (const members of new Set(pairs.values())) {
    const preferred = members.find(image => image.name === overrides.get(pairKey(image)))
      || members.find(image => !!image.raw === (view === 'raw')) || members[0];
    for (const image of members) if (image !== preferred) hidden.add(image.name);
  }
  return filtered.filter(image => !hidden.has(image.name));
}
export function pairedTargets(selected, pairs, linked) {
  const result = new Map();
  for (const image of selected) {
    for (const member of linked ? pairs.get(image.name) || [image] : [image]) result.set(member.name, member);
  }
  return [...result.values()];
}
