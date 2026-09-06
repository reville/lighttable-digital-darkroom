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
    if (!selected.length) return toast('Select photos first');
    const request = ++sequence;
    busy = true; proposal = null; el('captureTimeApply').disabled = true;
    status('Reading capture times…');
    try {
      const result = await post('/api/metadata/capture-time', {action:'preview', names:selected,
        shiftSeconds:Number(el('captureShiftHours').value || 0) * 3600,
        timeZone:el('captureTimeZone').value.trim(), includePairs:el('captureIncludePairs').checked, reset});
      if (request !== sequence) return;
      if (result?.error || !result?.ok) throw Error(result?.error || 'Preview failed');
      proposal = result;
      status(`${result.count} capture${result.count===1?'':'s'} will change. Originals stay unchanged.\n`
        + result.changes.slice(0,20).map(item=>`${item.name.split('/').pop()}: ${item.before || 'Unknown'} → ${item.after || item.original || 'Original camera time'}${item.warning ? '\n' + item.warning : ''}`).join('\n')
        + (result.count>20 ? `\n… and ${result.count-20} more.` : '')
        + (result.skipped.length ? `\nSkipped ${result.skipped.length}: ${result.skipped.slice(0,5).map(item=>`${item.name.split('/').pop()} — ${item.reason}`).join('; ')}` : ''));
      el('captureTimeApply').disabled = !result.count;
    } catch(error) { status(error.message || 'Could not preview capture times'); }
    finally { busy = false; }
  }
  async function apply(changes, undo=false) {
    if (busy || !changes?.length) return;
    busy=true; el('captureTimeApply').disabled=true; el('captureTimeUndo').disabled=true;
    status('Saving corrected capture times…');
    try {
      if (!await ctx.flush()) throw Error('Save pending edits before correcting capture times');
      const result=await post('/api/metadata/capture-time',{action:'apply',changes});
      if(result?.error || !result?.ok) throw Error(result?.error || 'Could not save capture times');
      lastChanges=undo ? null : changes.map(item=>({...item,beforeOverride:item.after,after:item.beforeOverride}));
      proposal=null;
      status(`${undo?'Restored':'Corrected'} ${result.count} capture${result.count===1?'':'s'}. You can also restore capture-time steps in History.`);
      await ctx.changed(result);
    } catch(error) { status(error.message || 'Could not save capture times'); }
    finally {busy=false; el('captureTimeUndo').disabled=!lastChanges; el('captureTimeApply').disabled=!proposal?.count;}
  }
  el('captureTimePreview').onclick=()=>preview();
  el('captureTimeReset').onclick=()=>preview(true);
  el('captureTimeApply').onclick=()=>apply(proposal?.changes);
  el('captureTimeUndo').onclick=()=>apply(lastChanges,true);
  return {selectionChanged:invalidate};
}
