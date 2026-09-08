import { t as tr } from './i18n.js';
/** Describe delivered detail, rather than treating a 100% zoom as pixel proof. */
export function previewDetailLabel({state, refining=false, delivered=0, renderedWidth=delivered, requested=0,
  source=0, actual=false} = {}) {
  if (state === 'empty' || state === 'error') return '';
  if (state === 'pending') return (actual ? tr("Loading 100% detail…") : tr("Updating preview…"));
  if (refining) return tr("Refining RAW detail…");
  // RAW active pixels can be smaller than the camera's advertised dimensions.
  // A completed full-size request must not report an update that will never run.
  if (delivered > 0 && requested > renderedWidth + 1 && (!source || delivered < source - 1)) return tr("Updating preview detail…");
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
