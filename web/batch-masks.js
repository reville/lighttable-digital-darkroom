/* Batch progress follows the immutable job ID. State changes arrive over SSE. */
export function mergeMaskDelta(masks, delta) {
  const removed = new Set(delta.removed || []);
  const result = (masks || []).filter(mask => !removed.has(mask.id));
  const present = new Set(result.map(mask => mask.id));
  for (const mask of delta.added || []) {
    if (!present.has(mask.id)) { result.push(structuredClone(mask)); present.add(mask.id); }
  }
  return result;
}

export function installMaskBatch({el, post, get, flush, targets, toast}) {
  let current = null;
  let request = false;
  function render() {
    el('batchMaskActivity').hidden = !current;
    if (!current) return;
    const running = Boolean(current.active);
    const processed = current.processed || 0;
    el('batchMaskSummary').textContent = running
      ? `${current.cancel_requested ? 'Stopping mask detection' : 'Generating masks'} · ${processed} of ${current.total} photos processed`
      : current.undone ? 'Generated masks undone · other edits retained'
      : `${current.cancel_requested ? 'Mask batch stopped' : 'Mask batch finished'} · ${current.masked || 0} photos masked · ${current.failed || 0} failed`;
    el('batchMaskCancel').hidden = !running;
    el('batchMaskCancel').disabled = request || current.cancel_requested;
    el('batchMaskUndo').hidden = running || !current.masked || current.undone;
    el('batchMaskUndo').disabled = request;
    el('batchMaskErrors').textContent = (current.omittedResults ? 'Showing recent results. Earlier failures remain included in the total.\n' : '') + (current.errors || []).join('\n');
    el('batchMaskDetails').hidden = !current.errors?.length;
    el('batchMaskDismiss').hidden = running;
    el('batchAiMaskBtn').disabled = running || request || !targets().length;
  }
  async function start() {
    if (request || current?.active) return;
    const names = targets().map(image => image.name);
    if (!names.length) return toast('Select photos for batch AI masking');
    request = true;
    render();
    try {
      if (!await flush()) throw new Error('Finish saving your edits before generating masks');
      const result = await post('/api/batch/semantic-masks', {names, categories: ['subject']});
      if (!result.ok) throw new Error(result.error || 'Could not start mask detection');
      const status = await get('/api/batch/semantic-masks/status');
      if (status.jobId === result.jobId) current = status;
    } catch (error) { toast(error.message); }
    finally { request = false; render(); }
  }
  el('batchAiMaskBtn').onclick = start;
  el('batchMaskCancel').onclick = async () => {
    if (!current?.active || request) return;
    request = true; render();
    try {
      const result = await post('/api/batch/semantic-masks/cancel', {jobId: current.jobId});
      if (!result.ok) throw new Error(result.error || 'Could not cancel');
      current = await get('/api/batch/semantic-masks/status');
    } catch (error) { toast(error.message); }
    finally { request = false; render(); }
  };
  el('batchMaskUndo').onclick = async () => {
    if (!current || request) return;
    request = true; render();
    try {
      if (!await flush()) throw new Error('Finish saving your edits before undoing the batch');
      const result = await post('/api/batch/semantic-masks/undo', {jobId: current.jobId});
      if (!result.ok) throw new Error(result.error || 'Could not undo this batch');
      current = {...current, masked: 0, undone: true};
      toast(`Removed generated masks from ${result.count} photos`);
    } catch (error) { toast(error.message); }
    finally { request = false; render(); }
  };
  el('batchMaskDismiss').onclick = () => { current = null; render(); };
  get('/api/batch/semantic-masks/status').then(status => {
    if (!current && status.total) { current = status; render(); }
  }).catch(() => {});
  return {
    get active() { return Boolean(current?.active || request); },
    update(record) {
      if (record.kind !== 'masks.semantic' || !record.result) return;
      if (current?.active && current.jobId !== record.id) return;
      current = {...record.result, jobId: record.id};
      render();
    },
  };
}
