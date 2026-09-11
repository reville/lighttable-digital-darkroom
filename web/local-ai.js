// SPDX-License-Identifier: GPL-3.0-only
import {tn as trn} from './i18n.js';

export function aiSearchTerms(metadata) {
  if (!metadata) return [];
  const terms = [metadata.caption, ...(metadata.tags || []), ...(metadata.ocr || [])];
  const faces = +(metadata.faceCount || 0);
  if (faces) terms.push('face', 'faces', 'person', 'people');
  if (faces > 1) terms.push('group', `${faces} faces`, `${faces} people`);
  return terms;
}

export function aiSkippedSummary(status) {
  if (!status.skipped) return '';
  const reasons = status.skippedReasons || {};
  const counts = [
    reasons.empty > 0 ? trn('{count} empty file', '{count} empty files', reasons.empty) : '',
    reasons['cloud-only'] > 0 ? trn('{count} file not downloaded', '{count} files not downloaded', reasons['cloud-only']) : '',
    reasons.unavailable > 0 ? trn('{count} unavailable file', '{count} unavailable files', reasons.unavailable) : '',
  ].filter(Boolean);
  return trn('{count} photo skipped{reasons}. Download or restore the originals, then rebuild the index.',
    '{count} photos skipped{reasons}. Download or restore the originals, then rebuild the index.', status.skipped,
    {reasons: counts.length ? ` (${counts.join(', ')})` : ''});
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
