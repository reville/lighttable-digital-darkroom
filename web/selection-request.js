/* Resolve the full view before publishing a selection; never publish a subset. */
export function createSelectionRequest({load, scope, visible, selection, changed, onError}) {
  let generation = 0;
  let pending = false;
  return {
    get pending() { return pending; },
    cancel() { generation++; pending = false; changed(); },
    async selectAll() {
      const ticket = ++generation;
      const intendedScope = scope();
      selection().clear();
      pending = true;
      changed();
      try {
        await load();
        if (ticket !== generation || intendedScope !== scope()) return false;
        for (const image of visible()) selection().add(image.name);
        return true;
      } catch (error) {
        if (ticket === generation) onError(error);
        return false;
      } finally {
        if (ticket === generation) { pending = false; changed(); }
      }
    },
  };
}
