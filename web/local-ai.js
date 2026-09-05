export function aiSearchTerms(metadata) {
  if (!metadata) return [];
  const terms = [metadata.caption, ...(metadata.tags || []), ...(metadata.ocr || [])];
  const faces = +(metadata.faceCount || 0);
  if (faces) terms.push('face', 'faces', 'person', 'people');
  if (faces > 1) terms.push('group', `${faces} faces`, `${faces} people`);
  return terms;
}

/* Assisted culling ------------------------------------------------------- */

export const CULL_SELECT = ['subjectSharpness', 'eyeSharpness', 'eyesOpen'];
export const CULL_REJECT = ['exposure', 'misfire', 'document'];

/** The verdict for one criterion, or null when the photo was never scored. */
export function cullVerdict(metadata, criterion) {
  const entry = metadata?.cull?.criteria?.[criterion];
  return entry && typeof entry === 'object' ? entry : null;
}

/** True when any of the chosen criteria answered yes for this photo. */
export function cullMatches(metadata, criteria) {
  return (criteria || []).some(
    (name) => cullVerdict(metadata, name)?.verdict === 'yes');
}

/** How many photos each criterion answered yes for, and how many it judged. */
export function cullTally(images, criteria) {
  const counts = {};
  for (const name of criteria) counts[name] = { yes: 0, judged: 0 };
  for (const image of images) {
    for (const name of criteria) {
      const entry = cullVerdict(image.ai, name);
      if (!entry) continue;
      if (entry.verdict !== 'unknown') counts[name].judged += 1;
      if (entry.verdict === 'yes') counts[name].yes += 1;
    }
  }
  return counts;
}
