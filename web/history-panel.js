/* Persistent editing history.
 *
 * Each committed interaction is stored in the catalog, compressed, so edits
 * remain available after the session's undo stacks are gone. The pane lists
 * steps newest first; restoring one creates another undoable edit.
 */

const STEP_LIMIT = 200;
const COALESCE_MS = 2000;

// Step labels and origins arrive from the CLI and other clients as free text.
const escapeHTML = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export function createHistoryPanel(ctx) {
  const { el, post, get, toast } = ctx;
  const list = el('historyList');
  const clearButton = el('historyClear');
  let currentName = null;
  let steps = [];
  const channels = new Map();
  let refreshSequence = 0;
  let selectionSequence = 0;
  const isAvailable = () => !ctx.enabled || ctx.enabled();

  function timeText(seconds) {
    const date = new Date((seconds || 0) * 1000);
    if (Number.isNaN(date.getTime())) return '';
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    return sameDay
      ? date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : date.toLocaleDateString([], { month: 'short', day: 'numeric' })
        + ' ' + date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function render() {
    if (!list) return;
    if (!isAvailable()) {
      list.textContent = 'Persistent history is available in catalog libraries. Undo and redo still work in this session.';
      if (clearButton) clearButton.disabled = true;
      return;
    }
    if (clearButton) clearButton.disabled = !currentName || !steps.length;
    if (!currentName) { list.textContent = 'Select a photo.'; return; }
    if (!steps.length) {
      list.textContent = 'No steps recorded yet.';
      return;
    }
    list.innerHTML = steps.map((step) => `
      <button class="history-step" data-id="${escapeHTML(step.id)}" type="button">
        <span class="history-label">${escapeHTML(step.label || 'Edit')}</span>
        <span class="history-meta">${timeText(step.created)}${
          step.origin && step.origin !== 'edit'
            ? ` · ${escapeHTML(step.origin)}` : ''}</span>
      </button>`).join('');
  }

  let loadedName = null;

  async function refresh(name, force = false) {
    const requestedName = name || null;
    if (currentName !== requestedName) {
      refreshSequence++;
      selectionSequence++;
      steps = [];
      loadedName = null;
    }
    currentName = requestedName;
    if (!isAvailable()) {
      refreshSequence++;
      loadedName = null;
      steps = [];
      render();
      return;
    }
    const isVisible = Boolean(el('historyPane')?.classList.contains('on'));
    if (!isVisible && !force) {
      return;
    }
    if (currentName === loadedName && !force) return;
    steps = [];
    if (!currentName) { loadedName = null; render(); return; }
    const sequence = ++refreshSequence;
    try {
      const response = await get(
        `/api/history?name=${encodeURIComponent(requestedName)}`);
      if (sequence !== refreshSequence || currentName !== requestedName) return;
      loadedName = requestedName;
      steps = (response && response.steps) || [];
    } catch (error) {
      if (sequence !== refreshSequence || currentName !== requestedName) return;
      steps = [];
    }
    render();
  }

  function channelFor(name) {
    if (!channels.has(name)) channels.set(name, {
      name, label: '', stateJSON: null, deadline: 0, pending: null,
      timer: null, tail: Promise.resolve(true), queue: [], failed: false, error: null,
    });
    return channels.get(name);
  }

  function notifyStatus() {
    const active = [...channels.values()];
    ctx.onStatus?.({
      pendingNames: active.filter(channel => channel.pending || channel.queue.length)
        .map(channel => channel.name),
      failedNames: active.filter(channel => channel.failed).map(channel => channel.name),
      error: active.find(channel => channel.failed)?.error || null,
    });
  }

  function retire(channel) {
    // Retain a recipe only for its active coalescing window or outstanding
    // writes, rather than keeping every visited photo's large mask snapshot.
    if (!channel.timer && !channel.pending && !channel.queue.length &&
        channels.get(channel.name) === channel) {
      channels.delete(channel.name);
    }
  }

  function sendQueued(channel, retry = false) {
    channel.tail = channel.tail.then(async () => {
      if (retry) {
        channel.failed = false;
        channel.error = null;
        notifyStatus();
      }
      if (channel.failed) return false;
      while (channel.queue.length) {
        const { path, body } = channel.queue[0];
        try {
          const response = await post(path, body);
          if (response?.error || response?.ok === false) {
            throw new Error(response.error || 'History request failed');
          }
        } catch (error) {
          // Keep the failed operation at the head. In particular, neither a
          // later edit nor Clear may overtake a write awaiting Retry save.
          channel.failed = true;
          channel.error = path === '/api/history/clear'
            ? 'Could not clear photo history' : 'Could not save photo history';
          toast(channel.error);
          notifyStatus();
          return false;
        }
        channel.queue.shift();
        notifyStatus();
        if (channel.name === currentName) {
          loadedName = null;
          void refresh(channel.name);
        }
      }
      return true;
    }).finally(() => {
      retire(channel);
      notifyStatus();
    });
    return channel.tail;
  }

  function enqueue(channel, path, body) {
    channel.queue.push({ path, body });
    notifyStatus();
    return sendQueued(channel);
  }

  function flushPending(channel) {
    if (!channel.pending) return;
    const packet = channel.pending;
    channel.pending = null;
    enqueue(channel, '/api/history', packet);
  }

  function finishWindow(channel) {
    if (channel.timer !== null) clearTimeout(channel.timer);
    channel.timer = null;
    channel.deadline = 0;
    channel.stateJSON = null;
    flushPending(channel);
    retire(channel);
  }

  /* Send the first edit immediately and the last edit within two seconds.
   * Each photo owns its window and send chain: navigating or changing tools
   * cannot replace another photo's pending step or reorder its snapshots. */
  function record(name, label, state) {
    if (!isAvailable() || !name || !state) return;
    const stateJSON = JSON.stringify(state);
    const packet = { name, label, state: JSON.parse(stateJSON) };
    const channel = channelFor(name);
    const now = Date.now();
    if (label === channel.label && now < channel.deadline) {
      if (stateJSON === channel.stateJSON) return;
      channel.stateJSON = stateJSON;
      channel.pending = packet;
      notifyStatus();
      return;
    }
    flushPending(channel);
    channel.label = label;
    channel.stateJSON = stateJSON;
    channel.deadline = now + COALESCE_MS;
    if (channel.timer !== null) clearTimeout(channel.timer);
    channel.timer = setTimeout(() => finishWindow(channel), COALESCE_MS);
    enqueue(channel, '/api/history', packet);
  }

  function flush(name = null) {
    const selected = [...channels.values()].filter(channel => !name || channel.name === name);
    const results = selected.map(channel => {
      // Retry failures already observed by this call. A write that first fails
      // during this flush remains pending and makes this attempt return false.
      const retry = channel.failed;
      finishWindow(channel);
      return sendQueued(channel, retry);
    });
    return Promise.all(results)
      .then(results => results.every(Boolean));
  }

  if (list) {
    list.addEventListener('click', async (event) => {
      const button = event.target.closest('.history-step');
      if (!button) return;
      const name = currentName;
      const selection = selectionSequence;
      try {
        const state = await get(`/api/history/state?id=${button.dataset.id}`);
        if (name !== currentName || selection !== selectionSequence) return;
        if (state?.error) throw new Error(state.error);
        if (state?.captureTimeOnly) {
          if (!ctx.onRestoreCaptureTime) throw new Error('Capture-time restore is unavailable');
          await ctx.onRestoreCaptureTime(name, Number(button.dataset.id));
        } else if (state && ctx.onRestore) await ctx.onRestore(state);
      } catch (error) {
        toast('Could not restore that step');
      }
    });
  }

  if (clearButton) {
    clearButton.addEventListener('click', async () => {
      if (!isAvailable() || !currentName) return;
      const name = currentName;
      const channel = channelFor(name);
      // Queue clear after accepted edits; subsequent edits queue after clear.
      // Keep the channel registered until its clear operation is enqueued.
      flushPending(channel);
      const cleared = enqueue(channel, '/api/history/clear', { name });
      finishWindow(channel);
      await cleared;
    });
  }

  render();
  notifyStatus();
  return {
    refresh, record, flush, retry: flush,
    get hasPending() {
      return [...channels.values()].some(channel => channel.pending || channel.queue.length > 0);
    },
    get limit() { return STEP_LIMIT; },
  };
}
