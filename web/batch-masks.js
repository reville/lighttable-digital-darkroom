// SPDX-License-Identifier: GPL-3.0-only
import {t as tr, tn as trn} from './i18n.js';
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
      ? tr('{status} · {processed} of {total} photos processed', {status: current.cancel_requested ? tr('Stopping mask detection') : tr('Generating masks'), processed, total: current.total})
      : current.undone ? tr('Generated masks undone · other edits retained')
      : tr('{status} · {masked} photos masked · {failed} failed', {status: current.cancel_requested ? tr('Mask batch stopped') : tr('Mask batch finished'), masked: current.masked || 0, failed: current.failed || 0});
    el('batchMaskCancel').hidden = !running;
    el('batchMaskCancel').disabled = request || current.cancel_requested;
    el('batchMaskUndo').hidden = running || !current.masked || current.undone;
    el('batchMaskUndo').disabled = request;
    el('batchMaskErrors').textContent = (current.omittedResults ? tr('Showing recent results. Earlier failures remain included in the total.\n') : '') + (current.errors || []).join('\n');
    el('batchMaskDetails').hidden = !current.errors?.length;
    el('batchMaskDismiss').hidden = running;
    el('batchAiMaskBtn').disabled = running || request || !targets().length;
  }
  async function start() {
    if (request || current?.active) return;
    const names = targets().map(image => image.name);
    if (!names.length) return toast(tr('Select photos for batch AI masking'));
    request = true;
    render();
    try {
      if (!await flush()) throw new Error(tr('Finish saving your edits before generating masks'));
      const result = await post('/api/batch/semantic-masks', {names, categories: ['subject']});
      if (!result.ok) throw new Error(result.error || tr('Could not start mask detection'));
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
      if (!result.ok) throw new Error(result.error || tr('Could not cancel'));
      current = await get('/api/batch/semantic-masks/status');
    } catch (error) { toast(error.message); }
    finally { request = false; render(); }
  };
  el('batchMaskUndo').onclick = async () => {
    if (!current || request) return;
    request = true; render();
    try {
      if (!await flush()) throw new Error(tr('Finish saving your edits before undoing the batch'));
      const result = await post('/api/batch/semantic-masks/undo', {jobId: current.jobId});
      if (!result.ok) throw new Error(result.error || tr('Could not undo this batch'));
      current = {...current, masked: 0, undone: true};
      toast(trn('Removed generated masks from {count} photo', 'Removed generated masks from {count} photos', result.count));
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
