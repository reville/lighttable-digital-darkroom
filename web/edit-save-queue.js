// A photo's identity and complete JSON edit state travel together. Never read
// mutable editor state from a timer or after a request has started.
function copyPayload(payload) {
  return typeof structuredClone === 'function'
    ? structuredClone(payload)
    : JSON.parse(JSON.stringify(payload));
}

function freezePayload(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    Object.values(value).forEach(freezePayload);
  }
  return value;
}

/**
 * Debounce independently per photo and serialize requests for each photo.
 * send(name, payload) must reject on an unsuccessful write. enqueue() never
 * returns a rejecting promise; background failures are retained in getStatus()
 * and onStatus(). A failed photo pauses until retry(), retaining its newest edit.
 * Await flush() before operations that require durable edits; it rejects on a
 * failure and waits only through the revisions present when it was called.
 */
export function createEditSaveQueue({
  send,
  delay = 400,
  onStatus = () => {},
  journal = null,
  setTimeout: schedule = globalThis.setTimeout.bind(globalThis),
  clearTimeout: unschedule = globalThis.clearTimeout.bind(globalThis),
}) {
  if (typeof send !== 'function') throw new TypeError('An edit save function is required');
  const entries = new Map();
  let tokenCounter = 0;
  const tokenPrefix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  let journalError = null;
  const cleanupPending = new Map();

  function getStatus() {
    const active = [...entries.values()];
    const errors = active.filter(entry => entry.error)
      .map(entry => ({name: entry.name, error: entry.error}));
    return {
      state: errors.length ? 'error' : active.length ? 'saving' : 'saved',
      pendingNames: active.map(entry => entry.name),
      savingNames: active.filter(entry => entry.running).map(entry => entry.name),
      errors,
      recoveryError: journalError,
    };
  }

  function notify() {
    // A broken UI observer must not turn a successful write into a failed one
    // or break the request chain. Still surface the observer's error.
    try { onStatus(getStatus()); }
    catch (error) { console.error('Edit save status callback failed', error); }
  }

  function clearTimer(entry) {
    if (entry.timer !== null) unschedule(entry.timer);
    entry.timer = null;
  }

  function start(entry) {
    if (entry.error || entry.running || !entry.queued) return;
    clearTimer(entry);
    const job = entry.queued;
    entry.queued = null;
    entry.running = job;
    notify();
    // Own both outcomes here so debounce/immediate saves cannot leave an
    // unhandled rejection when their caller has no reason to await a write.
    Promise.resolve(job.persisted).catch(error => { journalError = error; notify(); })
      .then(() => send(entry.name, job.payload)).then(async () => {
      if (journal) {
        try {
          await journal.remove(entry.name, job.token);
          if (cleanupPending.get(entry.name) === job.token) cleanupPending.delete(entry.name);
          if (!cleanupPending.size) journalError = null;
        }
        catch (error) { cleanupPending.set(entry.name, job.token); journalError = error; }
      }
      entry.running = null;
      const waiting = [];
      for (const waiter of entry.waiters) {
        if (waiter.revision <= job.revision) waiter.resolve();
        else waiting.push(waiter);
      }
      entry.waiters = waiting;
      if (!entry.queued) entries.delete(entry.name);
      notify();
      // Flush barriers bypass a later edit's debounce, but ordinary subsequent
      // input retains its own debounce window.
      if (entry.queued && (entry.timer === null || waiting.length)) start(entry);
    }, reason => {
      entry.running = null;
      entry.error = reason instanceof Error ? reason : new Error(String(reason));
      if (!entry.queued) entry.queued = job;
      clearTimer(entry);
      const waiting = entry.waiters;
      entry.waiters = [];
      waiting.forEach(waiter => waiter.reject(entry.error));
      notify();
    });
  }

  function enqueue(name, payload, {immediate = false} = {}) {
    if (typeof name !== 'string' || !name) throw new TypeError('A photo name is required');
    let entry = entries.get(name);
    const previous = entry && (entry.queued || entry.running)?.payload;
    // A rejected recovery must not turn into an unchecked write when the user
    // makes another adjustment before resolving its source-identity conflict.
    const guarded = previous?.expectedRecoverySourceKey && !payload.expectedRecoverySourceKey
      ? {...payload, expectedRecoverySourceKey: previous.expectedRecoverySourceKey,
          sourceKey: previous.sourceKey} : payload;
    const snapshot = freezePayload(copyPayload(guarded));
    if (!entry) {
      entry = {name, revision: 0, queued: null, running: null, timer: null, error: null, waiters: []};
      entries.set(name, entry);
    }
    const token = `${tokenPrefix}:${++tokenCounter}`;
    const persisted = journal ? Promise.resolve().then(() => journal.put(name, token, snapshot)) : null;
    // Attach a rejection handler now; debounce can outlive a failed disk write.
    persisted?.catch(error => { journalError = error; notify(); });
    entry.queued = {revision: ++entry.revision, payload: snapshot, token, persisted};
    clearTimer(entry);
    if (!entry.error && !immediate) {
      entry.timer = schedule(() => {
        entry.timer = null;
        start(entry);
      }, delay);
    }
    notify();
    if (immediate) start(entry);
  }

  function selectedEntries(name) {
    return name === undefined ? [...entries.values()] : [entries.get(name)].filter(Boolean);
  }

  async function flush(name) {
    const selected = selectedEntries(name);
    const barriers = selected.map(entry => new Promise((resolve, reject) => {
      if (entry.error) { reject(entry.error); return; }
      entry.waiters.push({revision: entry.revision, resolve, reject});
      clearTimer(entry);
      start(entry);
    }));
    const results = await Promise.allSettled(barriers);
    const failed = results.flatMap((result, index) => result.status === 'rejected'
      ? [{name: selected[index].name, error: result.reason}] : []);
    if (failed.length) {
      const error = new AggregateError(failed.map(item => item.error),
        `Could not save edits for ${failed.map(item => item.name).join(', ')}`);
      error.failedNames = failed.map(item => item.name);
      throw error;
    }
    return getStatus();
  }

  async function retry(name) {
    if (journal) {
      for (const [photo, token] of cleanupPending) {
        if (name !== undefined && name !== photo) continue;
        await journal.remove(photo, token); cleanupPending.delete(photo);
      }
      if (!cleanupPending.size) journalError = null;
    }
    selectedEntries(name).forEach(entry => { entry.error = null; });
    if (journal) selectedEntries(name).forEach(entry => {
      if (entry.queued) {
        const job = entry.queued;
        job.persisted = Promise.resolve().then(() => journal.put(entry.name, job.token, job.payload));
        job.persisted.catch(error => { journalError = error; notify(); });
      }
    });
    return flush(name);
  }

  function getPending(name) {
    const entry = entries.get(name);
    const job = entry && (entry.queued || entry.running);
    return job ? copyPayload(job.payload) : null;
  }

  return {enqueue, flush, retry, getStatus, getPending};
}
