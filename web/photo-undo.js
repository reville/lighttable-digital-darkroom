// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';
/** Session undo stacks keyed by qualified catalog photo name.
 *
 * activate() returns stable arrays for the UI's undo/redo availability checks.
 * Use the methods below to mutate them, or call trim() after direct changes.
 * Snapshots are immutable JSON strings. The byte budget conservatively counts
 * two bytes per UTF-16 code unit; it bounds snapshot payloads, not JS overhead.
 */
export function createPhotoUndoHistory({
  maxPhotos = 80, maxSteps = 60, maxBytes = 24 * 1024 * 1024,
} = {}) {
  for (const [name, value, minimum] of [
    ['maxPhotos', maxPhotos, 1], ['maxSteps', maxSteps, 0], ['maxBytes', maxBytes, 0],
  ]) {
    if (!Number.isSafeInteger(value) || value < minimum) {
      throw new RangeError(tr("{name} must be an integer of at least {minimum}", {name: name, minimum: minimum}));
    }
  }

  const photos = new Map();
  let activeName = null;

  function checkSnapshot(state) {
    if (typeof state !== 'string') throw new TypeError(tr("Undo snapshots must be strings"));
    return state.length * 2;
  }

  function byteCount(entry) {
    return [...entry.undo, ...entry.redo].reduce((sum, state) => sum + checkSnapshot(state), 0);
  }

  function totalBytes() {
    let size = 0;
    for (const entry of photos.values()) size += byteCount(entry);
    return size;
  }

  function forget(name) {
    const entry = photos.get(name);
    if (!entry) return;
    entry.undo.length = 0;
    entry.redo.length = 0;
    photos.delete(name);
  }

  // Both stacks end at the nearest available state. Prune their far ends,
  // keeping the immediate redo path when undoing a photo with a large recipe.
  function dropOldest(entry) {
    const stack = entry.undo.length ? entry.undo : entry.redo;
    return checkSnapshot(stack.shift());
  }

  function trim() {
    for (const entry of photos.values()) {
      while (entry.undo.length + entry.redo.length > maxSteps) dropOldest(entry);
    }
    let bytes = totalBytes();
    for (const [name, entry] of photos) {
      if (photos.size <= maxPhotos && bytes <= maxBytes) break;
      if (name === activeName) continue;
      bytes -= byteCount(entry);
      forget(name);
    }
    const active = photos.get(activeName);
    while (active && bytes > maxBytes && (active.undo.length || active.redo.length)) {
      bytes -= dropOldest(active);
    }
  }

  return {
    activate(name) {
      if (typeof name !== 'string' || !name) throw new TypeError(tr("A photo name is required"));
      const entry = photos.get(name) || { undo: [], redo: [] };
      photos.delete(name);
      photos.set(name, entry);
      activeName = name;
      trim();
      return entry;
    },

    push(state) {
      checkSnapshot(state);
      const entry = photos.get(activeName);
      if (!entry) return false;
      const changed = entry.undo.at(-1) !== state || entry.redo.length > 0;
      if (entry.undo.at(-1) !== state) entry.undo.push(state);
      // Starting a new edit always abandons redo, even if its starting state
      // equals the most recent undo snapshot.
      entry.redo.length = 0;
      trim();
      return changed;
    },

    // Remove a snapshot that a cancelled gesture pushed, but only while it is
    // still the newest one and still matches. An edit that landed in between
    // keeps its step. Without this a cancelled drag left an Undo that did
    // nothing when pressed.
    dropLast(state) {
      const entry = photos.get(activeName);
      if (!entry?.undo.length || entry.undo.at(-1) !== state) return false;
      entry.undo.pop();
      trim();
      return true;
    },

    undo(current) {
      const entry = photos.get(activeName);
      if (!entry?.undo.length) return null;
      checkSnapshot(current);
      const previous = entry.undo.pop();
      entry.redo.push(current);
      trim();
      return previous;
    },

    redo(current) {
      const entry = photos.get(activeName);
      if (!entry?.redo.length) return null;
      checkSnapshot(current);
      const next = entry.redo.pop();
      entry.undo.push(current);
      trim();
      return next;
    },

    // Invalidate an externally changed photo without changing the selection.
    // Keep active arrays intact because app state holds references to them.
    clear(name = activeName) {
      const entry = photos.get(name);
      if (!entry) return;
      if (name === activeName) {
        entry.undo.length = 0;
        entry.redo.length = 0;
      } else {
        forget(name);
      }
    },

    trim,
    get size() { return photos.size; },
    get bytes() { return totalBytes(); },
    get activeName() { return activeName; },
  };
}
