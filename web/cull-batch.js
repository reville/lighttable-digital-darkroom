// SPDX-License-Identifier: GPL-3.0-only
/** Flag changes are separate from analysis and retain a session undo stack. */
export function cullFlagTargets(images, status, replace = false) {
  return [...new Map(images.map(image => [image.name, image])).values()]
    .filter(image => image.status !== status &&
      (replace || !image.status || image.status === 'pending'));
}

export function createCullBatch({imageFor, enqueue, flush, changed}) {
  const batches = [], versions = new Map();
  let busy = false;
  const version = name => versions.get(name) || 0;
  function noteFlagChange(name, patch) {
    if (Object.hasOwn(patch, 'status')) versions.set(name, version(name) + 1);
  }
  async function apply(images, status, replace = false) {
    if (busy) return null;
    const targets = cullFlagTargets(images, status, replace);
    if (!targets.length) return null;
    busy = true;
    const batch = [];
    try {
      for (const image of targets) {
        const before = image.status || 'pending', beforeVersion = version(image.name);
        enqueue(image, {status});
        batch.push({name: image.name, before, beforeVersion, after: status, version: version(image.name)});
      }
      batches.push(batch);
      if (batches.length > 20) batches.shift();
      changed();
      return {count: batch.length, saved: await flush()};
    } finally { busy = false; changed(); }
  }
  async function undo() {
    if (busy || !batches.length) return null;
    busy = true;
    const batch = batches.pop();
    let count = 0;
    try {
      for (const entry of batch) {
        const image = imageFor(entry.name);
        // A later manual or external flag decision always wins over batch undo.
        if (!image || image.status !== entry.after || version(entry.name) !== entry.version) continue;
        enqueue(image, {status: entry.before});
        count++;
        // Restore the preceding batch's undo eligibility when unwinding our own work.
        for (const previous of batches) {
          const prior = previous.find(item => item.name === entry.name);
          if (prior?.version === entry.beforeVersion && prior.after === entry.before) prior.version = version(entry.name);
        }
      }
      changed();
      return {count, saved: await flush()};
    } finally { busy = false; changed(); }
  }
  return {apply, undo, noteFlagChange, get busy() { return busy; }, get canUndo() { return !!batches.length; }};
}
