// SPDX-License-Identifier: GPL-3.0-only
// Failures belong to a source revision, not permanently to a catalog name.
// Unknown photos remain eligible until the normal lazy display path tries them.
export function createPhotoDisplayStatus() {
  const results = new Map();
  let revision = 0;
  const identity = image => `${image.fileKey || image.mtime || ''}|${image.availability || 'local'}`;
  function entry(image) {
    const result = results.get(image.name);
    return result?.identity === identity(image) ? result : null;
  }
  return {
    get revision() { return revision; },
    record(image, failed, channel = 'thumbnail') {
      if (!image) return false;
      let result = entry(image);
      if (!result && !failed && !['cloud-only', 'unavailable', 'empty'].includes(image.availability)) return false;
      if (!result) {
        result = { identity: identity(image), states: {} };
        results.set(image.name, result);
      }
      if (result.states[channel] === failed) return false;
      result.states[channel] = failed;
      revision++;
      return true;
    },
    cannotDisplay(image) {
      const states = entry(image)?.states || {};
      if (Object.values(states).some(Boolean)) return true;
      // A successful source thumbnail after Retry supersedes stale cloud
      // availability until the next catalog scan refreshes the row.
      return states.thumbnail !== false && ['cloud-only', 'unavailable', 'empty'].includes(image.availability);
    },
    retain(images) {
      const names = new Set(images.map(image => image.name));
      for (const name of results.keys()) if (!names.has(name)) results.delete(name);
    },
  };
}
