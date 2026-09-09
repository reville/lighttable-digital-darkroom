/** Conservative local suggestions. Groups never change flags or catalog stacks. */
const SELECT = ['subjectSharpness', 'eyeSharpness', 'eyesOpen'];
const REJECT = ['exposure', 'misfire', 'document'];
const MAX_GAP_MS = 30_000;
const MAX_NEIGHBORS = 64;
const MAX_GROUP_SIZE = 16; // Every member must fit in Survey for review.
const POPCOUNT = Array.from({length: 256}, (_, value) => {
  let count = 0;
  for (; value; value &= value - 1) count++;
  return count;
});

function signature(image) {
  const value = image.ai?.cull?.similarity;
  if (value?.version !== 1 || !/^[0-9a-f]{16}$/i.test(value.hash) ||
      !/^[0-9a-f]{96}$/i.test(value.layout) ||
      !Number.isFinite(value.contrast) || value.contrast < 0.035 ||
      !Number.isFinite(value.aspect) || value.aspect <= 0) return null;
  const hash = value.hash.match(/../g).map(byte => parseInt(byte, 16));
  const bits = hash.reduce((sum, byte) => sum + POPCOUNT[byte], 0);
  if (bits < 4 || bits > 60) return null;
  return {...value, hash, layout: value.layout.match(/../g).map(byte => parseInt(byte, 16))};
}

function similar(a, b) {
  if (Math.abs(Math.log(a.aspect / b.aspect)) > 0.04) return false;
  let distance = 0;
  for (let i = 0; i < a.hash.length; i++) distance += POPCOUNT[a.hash[i] ^ b.hash[i]];
  if (distance > 8) return false;
  let total = 0;
  for (let i = 0; i < a.layout.length; i++) {
    const delta = Math.abs(a.layout[i] - b.layout[i]);
    if (delta > 45) return false;
    total += delta;
  }
  return total / a.layout.length <= 18;
}

function quality(image) {
  const record = image.ai.cull, criteria = record.criteria;
  const count = (keys, verdict) => keys.filter(key => criteria[key]?.verdict === verdict).length;
  const focus = record.metrics?.focus?.frame;
  return [count(REJECT, 'yes'), -count(SELECT, 'yes'), -count(REJECT, 'no'),
    -(Number.isFinite(focus) && focus > 0 ? focus : 0)];
}

/** Return arrays of original photos, strongest first; omit singletons.
 * captureValue is the app's capture-time parser returning milliseconds. A
 * catalog captureTimeKnown marker is required because date/captureTime can
 * otherwise be a filesystem-time fallback. Equal ranks retain input order.
 * Every member must match every earlier member, so A~B~C cannot chain A!~C.
 * Only 64 temporal neighbors are scanned per seed, keeping large imports bounded.
 * Groups fit Survey's 16-photo limit; overflow remains available to later groups.
 */
export function groupSimilarPhotos(images, captureValue) {
  if (typeof captureValue !== 'function') return [];
  const candidates = [];
  for (const [index, image] of images.entries()) {
    if (!image || image.kind === 'video' || image.virtual || image.captureTimeKnown !== true ||
        !image.ai?.cull?.criteria) continue;
    const rawTime = image.date || image.captureTime;
    if (typeof rawTime !== 'string' ||
        !/^\d{4}[-:]\d{2}[-:]\d{2}[ T]\d{2}:\d{2}:\d{2}/.test(rawTime)) continue;
    const time = captureValue({...image, date: rawTime, captureTime: rawTime, mtime: undefined});
    if (!Number.isFinite(time) || time <= 0) continue;
    const value = signature(image);
    if (value) candidates.push({image, index, time, signature: value, quality: quality(image)});
  }
  candidates.sort((a, b) => a.time - b.time || a.index - b.index);
  const used = new Set(), groups = [];
  for (let i = 0; i < candidates.length; i++) {
    if (used.has(i)) continue;
    const first = candidates[i], group = [first], indices = [i];
    for (let j = i + 1; j < candidates.length && j <= i + MAX_NEIGHBORS &&
        group.length < MAX_GROUP_SIZE; j++) {
      const candidate = candidates[j];
      if (candidate.time - first.time > MAX_GAP_MS) break;
      if (!used.has(j) && group.every(member => similar(member.signature, candidate.signature))) {
        group.push(candidate); indices.push(j);
      }
    }
    if (group.length < 2) continue;
    indices.forEach(index => used.add(index));
    group.sort((a, b) => {
      for (let k = 0; k < a.quality.length; k++) {
        if (a.quality[k] !== b.quality[k]) return a.quality[k] - b.quality[k];
      }
      return a.index - b.index;
    });
    groups.push(group.map(item => item.image));
  }
  return groups;
}

/** A recommendation requires a real select pass, no known reject issue, and
 * an untied assessment. Merely being first in a stable group is not evidence. */
export function cullSuggestion(group) {
  if (!Array.isArray(group) || group.length < 2 || !group[0]?.ai?.cull?.criteria) return null;
  const best = quality(group[0]);
  if (best[0] !== 0 || best[1] === 0) return null;
  for (const image of group.slice(1)) {
    if (!image?.ai?.cull?.criteria) return null;
    const other = quality(image);
    let comparison = 0;
    for (let i = 0; i < best.length; i++) {
      if (best[i] !== other[i]) { comparison = best[i] - other[i]; break; }
    }
    if (comparison >= 0) return null;
  }
  return group[0];
}
