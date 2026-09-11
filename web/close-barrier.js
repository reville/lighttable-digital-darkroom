// SPDX-License-Identifier: GPL-3.0-only
// Keep user input out of the interval between the final save and native quit.
// Cancelling a timed-out attempt invalidates its eventual asynchronous reply.
export function createCloseBarrier({capture, flush, setBlocked}) {
  let generation = 0;
  return {
    async prepare() {
      const current = ++generation;
      setBlocked(true);
      try {
        await capture();
        const saved = await flush();
        if (current !== generation) return false;
        if (!saved) setBlocked(false);
        return !!saved;
      } catch (_) {
        if (current === generation) setBlocked(false);
        return false;
      }
    },
    cancel() { generation++; setBlocked(false); },
  };
}
