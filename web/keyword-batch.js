import {t as tr, tn as trn} from './i18n.js';
/* Selection-wide additive tagging; each photo keeps its independent keywords. */
export function installKeywordBatch(ctx) {
  const {el, post, toast} = ctx;
  let busy = false, undoId = '';
  function sync() {
    const count = ctx.names().length;
    for (const id of ['keywordBatchAdd', 'keywordBatchRemove']) el(id).disabled = busy || !count || !ctx.enabled();
    el('keywordBatchUndo').hidden = !undoId;
    el('keywordBatchUndo').disabled = busy;
    el('keywordBatchScope').textContent = count ? trn('{count} selected photo', '{count} selected photos', count) : tr('Select photos in a grid to tag them together.');
  }
  async function run(action) {
    if (busy) return;
    const names = [...ctx.names()];
    const values = ctx.values();
    if (action !== 'undo' && (!names.length || !values.length)) return toast(tr('Select photos and enter keywords first'));
    busy = true; sync();
    el('keywordInput').disabled = true; el('keywordAdd').disabled = true;
    try {
      if (!await ctx.flush()) throw new Error(tr('Save pending edits before changing keywords'));
      const result = await post('/api/catalog/keywords', {action, names, keywords: values, undoId});
      if (!result.ok || result.error) throw new Error(result.error || tr('Keywords could not be saved'));
      ctx.apply(result.changes || []);
      undoId = result.undoId || '';
      el('keywordInput').value = '';
      toast(action === 'undo' ? trn('{count} photo restored', '{count} photos restored', result.count) : trn('{count} photo updated', '{count} photos updated', result.count));
    } catch (error) { toast(error.message); }
    finally {
      busy = false; el('keywordInput').disabled = false; el('keywordAdd').disabled = false; sync();
    }
  }
  el('keywordBatchAdd').onclick = () => run('add');
  el('keywordBatchRemove').onclick = () => run('remove');
  el('keywordBatchUndo').onclick = () => run('undo');
  sync();
  return {sync};
}
