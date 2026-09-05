/* One resilient Server-Sent Events connection for the LightTable window. */
export function connectServerEvents(client, handlers = {}) {
  if (typeof EventSource === 'undefined') return { close() {} };
  const source = new EventSource(`/api/events?client=${encodeURIComponent(client)}`);
  // The stream reconnects on its own; the window is told so it can check
  // whether the server is merely busy or has gone away.
  source.onerror = () => window.dispatchEvent(new CustomEvent(
    'lighttable-server-connection', { detail: { state: 'lost' } }));
  source.onopen = () => window.dispatchEvent(new CustomEvent(
    'lighttable-server-connection', { detail: { state: 'open' } }));
  const eventTypes = ['ready', 'state', 'library', 'job', 'ui.command',
    'ui.state', 'resync'];
  eventTypes.forEach((type) => {
    source.addEventListener(type, (event) => {
      let record = {};
      try { record = JSON.parse(event.data || '{}'); } catch { return; }
      if (handlers[type]) handlers[type](record);
    });
  });
  return source;
}
