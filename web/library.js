import { aiSearchTerms } from '/web/local-ai.js';

export function photoMatchesQuery(image, query, exif = null) {
  const normalized = String(query || '').trim().toLocaleLowerCase();
  if (!normalized) return true;
  const terms = [image.name, image.displayName, ...(image.keywords || []),
    ...aiSearchTerms(image.ai)];
  if (exif) terms.push(...Object.values(exif));
  return terms.some((value) => String(value || '').toLocaleLowerCase().includes(normalized));
}

export function photoMatchesRules(image, rules = {}, exif = null) {
  if ((+image.rating || 0) < (+rules.ratingMin || 0)) return false;
  if (rules.flag && rules.flag !== 'all' && image.status !== rules.flag) return false;
  if (rules.kind === 'raw' && !image.raw) return false;
  if (rules.kind === 'processed' && (image.raw || image.virtual)) return false;
  if (rules.kind === 'virtual' && !image.virtual) return false;
  return photoMatchesQuery(image, rules.query, exif);
}
