import { t as tr } from './i18n.js';
/** Describe delivered detail, rather than treating a 100% zoom as pixel proof. */
export function previewDetailLabel({state, refining=false, delivered=0, requested=0,
  source=0, actual=false, native=null} = {}) {
  if (state === 'empty' || state === 'error') return '';
  if (state === 'pending') return (actual ? tr("Loading 100% detail…") : tr("Updating preview…"));
  if (refining) return tr("Refining RAW detail…");
  // A native viewport is a full-resolution region, not a resized full frame.
  const region = native?.viewport;
  const sourcePixels = region?.width > 0 && region?.height > 0 &&
    region.fullWidth >= region.width && region.fullHeight >= region.height &&
    native.width === region.width && native.height === region.height;
  if (sourcePixels) return actual ? tr("100% detail ready") : '';
  if (delivered > 0 && requested > delivered + 1 && (!source || delivered < source - 1)) return tr("Updating preview detail…");
  if (!actual) return '';
  if (!source) return tr("Preview at 100% · source size unavailable");
  if (delivered >= source - 1) return tr("100% detail ready");
  return tr("Preview {value} px · source {value2} px", {value: Math.round(delivered), value2: Math.round(source)});
}

/** Keep the reported cause without guessing when a failure has no details. */
export function previewFailureMessage(summary, error) {
  const detail = typeof error === 'string' ? error : error?.message;
  const reason = typeof detail === 'string' ? detail.trim() : '';
  return reason ? `${summary}\n${reason}` : summary;
}
