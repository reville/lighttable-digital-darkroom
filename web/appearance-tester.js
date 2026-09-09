// Hidden, local UI experiment. Deliberately absent from menus and public Help.
const STORAGE_KEY = 'lighttable.appearance-test.v1';
const CHANNEL_NAME = 'lighttable-appearance-test';
const HEX = /^#[0-9a-f]{6}$/i;
export const DEFAULT_APPEARANCE = Object.freeze({color: '#f9c184', borders: false});

export function isAppearanceColor(value) {
  return typeof value === 'string' && HEX.test(value);
}

export function normalizeAppearance(value) {
  return {
    color: isAppearanceColor(value?.color) ? value.color.toLowerCase() : DEFAULT_APPEARANCE.color,
    borders: typeof value?.borders === 'boolean' ? value.borders : DEFAULT_APPEARANCE.borders,
  };
}

// Both windows share the same origin and, on macOS, the same WebKit data store.
// BroadcastChannel also keeps live updates working if storage is unavailable.
export function createAppearanceSettings({win = window, onChange = () => {}} = {}) {
  const read = () => {
    try { return normalizeAppearance(JSON.parse(win.localStorage.getItem(STORAGE_KEY))); }
    catch (_) { return normalizeAppearance(null); }
  };
  let settings = read(), channel = null, receivedUpdate = false;
  const receive = value => {
    settings = normalizeAppearance(value);
    onChange({...settings});
  };
  try { channel = new win.BroadcastChannel(CHANNEL_NAME); } catch (_) {}
  if (channel) channel.onmessage = ({data}) => {
    if (data?.type === 'request') channel.postMessage({type: 'snapshot', settings});
    else if (data?.type === 'settings' || (data?.type === 'snapshot' && !receivedUpdate)) {
      if (data.type === 'settings') receivedUpdate = true;
      receive(data.settings);
    }
  };
  const storage = event => {
    if (event.key === STORAGE_KEY || event.key === null) receive(read());
  };
  win.addEventListener('storage', storage);
  const set = value => {
    receivedUpdate = true;
    receive(value);
    try {
      if (settings.color === DEFAULT_APPEARANCE.color && settings.borders === DEFAULT_APPEARANCE.borders) win.localStorage.removeItem(STORAGE_KEY);
      else win.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch (_) {}
    channel?.postMessage({type: 'settings', settings});
  };
  receive(settings);
  channel?.postMessage({type: 'request'});
  return {
    get: () => ({...settings}), set,
    reset: () => set(null),
    close: () => { channel?.close(); win.removeEventListener('storage', storage); },
  };
}

export function applyAppearance(root, settings) {
  root.toggleAttribute('data-appearance-color', Boolean(settings.color));
  if (settings.color) {
    root.style.setProperty('--appearance-accent', settings.color);
    const rgb = [1, 3, 5].map(start => parseInt(settings.color.slice(start, start + 2), 16));
    root.style.setProperty('--appearance-soft', `rgba(${rgb.join(',')},0.16)`);
  } else {
    root.style.removeProperty('--appearance-accent');
    root.style.removeProperty('--appearance-soft');
  }
  root.toggleAttribute('data-appearance-no-borders', !settings.borders);
}

export function installAppearanceTester() {
  if (['windows', 'linux'].includes(window.__LIGHTTABLE_PLATFORM__)) {
    return import('./appearance-tester-inline.js').then(module => module.installAppearanceTester());
  }
  if (document.getElementById('appearanceTestStyles')) return;
  const stylesheet = document.createElement('link');
  stylesheet.id = 'appearanceTestStyles';
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/web/appearance-tester.css';
  document.head.append(stylesheet);
  const settings = createAppearanceSettings({onChange: value => applyAppearance(document.documentElement, value)});
  let popup = null;
  const open = event => {
    if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== 'd') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.repeat) return;
    const native = window.webkit?.messageHandlers?.lightTable;
    if (native) { native.postMessage({action: 'openAppearanceTester'}); return; }
    if (popup && !popup.closed) { popup.focus(); return; }
    popup = window.open('/web/appearance-tester.html', 'LightTableAppearanceTester',
      'popup,width=340,height=300,resizable=yes');
    if (popup) popup.focus();
    else window.alert('Allow pop-up windows to open the appearance tester.');
  };
  document.addEventListener('keydown', open, true);
  window.addEventListener('pagehide', () => {
    document.removeEventListener('keydown', open, true);
    settings.close();
    popup?.close();
  }, {once: true});
}
