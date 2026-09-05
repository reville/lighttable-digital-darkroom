import { connectServerEvents } from '/web/events.js';

export function installUIBridge({ client, post, report, execute, handlers }) {
  let timer = null;
  const schedule = (delay = 100) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const state = report();
      post('/api/ui/state', { ...state, client }).catch(() => {});
    }, delay);
  };
  const source = connectServerEvents(client, {
    ...handlers,
    ready: () => schedule(0),
    'ui.command': async (event) => {
      if (event.targetClient && event.targetClient !== client) return;
      try {
        const result = await execute(event.command, event.args || {}, event);
        await post('/api/ui/result', { id: event.commandId, ok: true,
          result: result ?? null });
      } catch (error) {
        await post('/api/ui/result', { id: event.commandId, ok: false,
          error: error?.message || String(error) }).catch(() => {});
      } finally {
        schedule(0);
      }
    },
  });
  const heartbeat = setInterval(() => schedule(0), 5000);
  return { schedule, close: () => {
    clearInterval(heartbeat);
    clearTimeout(timer);
    source.close();
  } };
}
