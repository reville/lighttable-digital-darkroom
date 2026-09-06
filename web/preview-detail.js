/** Describe delivered detail, rather than treating a 100% zoom as pixel proof. */
export function previewDetailLabel({state, refining=false, delivered=0, requested=0,
  source=0, actual=false} = {}) {
  if (state === 'empty' || state === 'error') return '';
  if (state === 'pending') return actual ? 'Loading 100% detail…' : 'Updating preview…';
  if (refining) return 'Refining RAW detail…';
  if (delivered > 0 && requested > delivered + 1 && (!source || delivered < source - 1)) return 'Updating preview detail…';
  if (!actual) return '';
  if (!source) return 'Preview at 100% · source size unavailable';
  if (delivered >= source - 1) return '100% detail ready';
  return `Preview ${Math.round(delivered)} px · source ${Math.round(source)} px`;
}
