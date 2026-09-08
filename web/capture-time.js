import { t as tr, tn as trn } from './i18n.js';
/** Catalog date sorting treats known offsets as instants; unzoned camera times
 * remain unzoned and sort on the same neutral clock basis as SQLite. */
export function captureSortValue(image) {
  const value = image.date || image.captureTime || image.mtime;
  if (typeof value === 'number') return value * 1000;
  let text = String(value || '').replace(/^(\d{4}):(\d{2}):(\d{2})/, '$1-$2-$3').replace(' ', 'T');
  if (/^\d{4}-\d{2}-\d{2}T/.test(text) && !/(?:Z|[+-]\d{2}:\d{2})$/.test(text)) text += 'Z';
  const time = Date.parse(text);
  return Number.isFinite(time) ? time : 0;
}

/** Review a capture-clock correction before applying a named, immutable batch. */
export function installCaptureTime(ctx) {
  const {el, post, toast} = ctx;
  let proposal = null, lastChanges = null, busy = false, sequence = 0;
  const status = text => { el('captureTimeStatus').textContent = text; };
  function invalidate() { proposal = null; sequence++; el('captureTimeApply').disabled = true; }
  for (const id of ['captureShiftHours','captureTimeZone','captureIncludePairs']) el(id).addEventListener('input', invalidate);
  const names = () => [...new Set(ctx.selection())];
  async function preview(reset=false) {
    if (busy) return;
    const selected = names();
    if (!selected.length) return toast(tr("Select photos first"));
    const request = ++sequence;
    busy = true; proposal = null; el('captureTimeApply').disabled = true;
    status(tr("Reading capture times…"));
    try {
      const result = await post('/api/metadata/capture-time', {action:'preview', names:selected,
        shiftSeconds:Number(el('captureShiftHours').value || 0) * 3600,
        timeZone:el('captureTimeZone').value.trim(), includePairs:el('captureIncludePairs').checked, reset});
      if (request !== sequence) return;
      if (result?.error || !result?.ok) throw Error(((result?.error || tr("Preview failed"))));
      proposal = result;
      const lines = [trn('{count} capture will change. Originals stay unchanged.',
        '{count} captures will change. Originals stay unchanged.', result.count)];
      lines.push(...result.changes.slice(0, 20).map(item => tr('{name}: {before} → {after}{warning}', {
        name: item.name.split('/').pop(), before: item.before || tr('Unknown'),
        after: item.after || item.original || tr('Original camera time'),
        warning: item.warning ? '\n' + item.warning : '',
      })));
      if (result.count > 20) lines.push(tr('… and {count} more.', {count: result.count - 20}));
      if (result.skipped.length) lines.push(tr('Skipped {count}: {details}', {count: result.skipped.length,
        details: result.skipped.slice(0, 5).map(item => `${item.name.split('/').pop()} — ${item.reason}`).join('; ')}));
      status(lines.join('\n'));
      el('captureTimeApply').disabled = !result.count;
    } catch(error) { status(((error.message || tr("Could not preview capture times")))); }
    finally { busy = false; }
  }
  async function apply(changes, undo=false) {
    if (busy || !changes?.length) return;
    busy=true; el('captureTimeApply').disabled=true; el('captureTimeUndo').disabled=true;
    status(tr("Saving corrected capture times…"));
    try {
      if (!await ctx.flush()) throw Error(tr("Save pending edits before correcting capture times"));
      const result=await post('/api/metadata/capture-time',{action:'apply',changes});
      if(result?.error || !result?.ok) throw Error(((result?.error || tr("Could not save capture times"))));
      lastChanges=undo ? null : changes.map(item=>({...item,beforeOverride:item.after,after:item.beforeOverride}));
      proposal=null;
      status((undo ? trn("Restored {count} capture. You can also restore capture-time steps in History.", "Restored {count} captures. You can also restore capture-time steps in History.", result.count, {resultCount: result.count}) : trn("Corrected {count} capture. You can also restore capture-time steps in History.", "Corrected {count} captures. You can also restore capture-time steps in History.", result.count, {resultCount: result.count})));
      await ctx.changed(result);
    } catch(error) { status(((error.message || tr("Could not save capture times")))); }
    finally {busy=false; el('captureTimeUndo').disabled=!lastChanges; el('captureTimeApply').disabled=!proposal?.count;}
  }
  el('captureTimePreview').onclick=()=>preview();
  el('captureTimeReset').onclick=()=>preview(true);
  el('captureTimeApply').onclick=()=>apply(proposal?.changes);
  el('captureTimeUndo').onclick=()=>apply(lastChanges,true);
  return {selectionChanged:invalidate};
}
