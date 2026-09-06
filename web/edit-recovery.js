// Pending edits are kept outside the render server, scoped to one catalog.
// Native hosts persist files; a normal browser uses its own origin storage.
const clone = value => JSON.parse(JSON.stringify(value));
export async function recoveryKey(value) {
  const bytes = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, '0')).join('');
}

export function createEditRecovery({scope, nativeRequest, storage, hash = recoveryKey}) {
  const scopeKey = hash(scope);
  const prefix = 'lighttable-edit-recovery-v1:';
  let chain = Promise.resolve();
  function serial(run) {
    const next = chain.then(run);
    chain = next.catch(() => {});
    return next;
  }
  async function request(operation, name, value, token) {
    const scopeId = await scopeKey;
    const key = name ? await hash(name) : null;
    if (nativeRequest) return nativeRequest({operation, scope: scopeId, key, value, token});
    if (!storage) throw new Error('Local edit recovery storage is unavailable');
    const base = `${prefix}${scopeId}:`;
    if (operation === 'list') {
      const records = [];
      for (let i = 0; i < storage.length; i++) {
        const item = storage.key(i);
        if (item?.startsWith(base)) records.push(JSON.parse(storage.getItem(item)));
      }
      return records;
    }
    const path = base + key;
    if (operation === 'put') {
      const previous = storage.getItem(path);
      if (previous) JSON.parse(previous);
      storage.setItem(path, JSON.stringify(value));
    }
    if (operation === 'remove') {
      const previous = storage.getItem(path);
      if (previous && JSON.parse(previous).token === token) storage.removeItem(path);
    }
    return true;
  }
  return {
    async list() {
      const records = await request('list');
      if (!Array.isArray(records)) throw new Error('Invalid edit recovery journal');
      return records.filter(record => record?.scope === scope && typeof record.name === 'string'
        && typeof record.token === 'string' && record.payload?.state?.name === record.name);
    },
    put(name, token, payload) {
      const value = {version: 1, scope, name, token,
        updatedAt: new Date().toISOString(), payload: clone(payload)};
      return serial(() => request('put', name, value));
    },
    remove(name, token) { return serial(() => request('remove', name, null, token)); },
  };
}

// Only a successful acknowledgement for this exact revision may remove it.
export function recoveryPayloadMatches(payload, saved) {
  function equal(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
    if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length
      && left.every((item, index) => equal(item, right[index]));
    return Object.keys(left).every(key => equal(left[key], right[key]));
  }
  return Object.entries(payload?.state || {}).every(([key, value]) =>
    key === 'name' || equal(value, saved?.[key]));
}
