// SPDX-License-Identifier: GPL-3.0-only
/* Linux desktop colors belong to the chrome, never to rendered photographs.
 * The corresponding CSS deliberately leaves the workspace and color scopes
 * neutral. No theme value is interpreted as CSS or executable content. */
const HEX = /^#[\da-f]{6}$/i;
const color = (value, fallback) => typeof value === 'string' && HEX.test(value) ? value.toLowerCase() : fallback;
const rgb = (hex) => [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16));
const mix = (a, b, weight) => '#' + rgb(a).map((v, i) => Math.round(v * (1 - weight) + rgb(b)[i] * weight).toString(16).padStart(2, '0')).join('');
const luminance = (hex) => rgb(hex).map((v) => {
  v /= 255;
  return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
}).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
const readable = (candidate, background, minimum = 4.5) => {
  if (contrast(candidate, background) >= minimum) return candidate;
  const target = contrast('#ffffff', background) > contrast('#000000', background) ? '#ffffff' : '#000000';
  for (let step = 1; step <= 20; step++) {
    const adjusted = mix(candidate, target, step / 20);
    if (contrast(adjusted, background) >= minimum) return adjusted;
  }
  return target;
};

export function desktopThemeTokens(payload, systemDark = true) {
  const palette = payload?.source === 'omarchy' ? payload.colors || {} : {};
  const background = color(palette.background, systemDark ? '#222222' : '#f2f2f2');
  const mode = ['dark', 'light'].includes(payload?.mode) ? payload.mode
    : color(palette.background) ? (luminance(background) > .4 ? 'light' : 'dark')
      : systemDark ? 'dark' : 'light';
  const foreground = readable(color(palette.foreground, mode === 'dark' ? '#f0f0f0' : '#202020'), background);
  const surface = (key, weight) => {
    const proposed = color(palette[key], mix(background, foreground, weight));
    return contrast(foreground, proposed) >= 4.5 ? proposed : background;
  };
  const raised = surface('lighter_background', .05);
  const inset = surface('dark_background', .025);
  const field = surface(null, .045);
  const hover = surface(null, .09);
  const active = surface(null, .14);
  const muted = [background, raised, inset, field, hover, active].reduce(
    (candidate, surface) => readable(candidate, surface), mix(background, foreground, .66));
  const accent = readable(color(palette.accent, '#4b9cf5'), background);
  const accentStrong = readable(color(palette.accent, '#2680eb'), background, 3);
  const onAccent = readable('#ffffff', accentStrong);
  const soft = `rgba(${rgb(accent).join(',')},0.16)`;
  const line = mix(background, foreground, .21);
  return {
    source: payload?.source === 'omarchy' && color(palette.background) && color(palette.foreground) ? 'omarchy' : 'system',
    mode,
    tokens: {
      chrome: background, panel: background, 'panel-raised': raised, inset, field,
      'field-hover': hover, hover, active, menu: background,
      ink: foreground, 'ink-2': foreground, muted, 'muted-2': muted,
      line, 'line-soft': mix(background, foreground, .12), 'line-strong': mix(background, foreground, .3),
      accent, 'accent-strong': accentStrong, 'accent-soft': soft, 'on-accent': onAccent,
      'edit-panel': background, 'edit-line': line, 'edit-line-hi': 'transparent',
      'slider-accent': accentStrong, groove: mix(background, foreground, .18),
      'groove-hi': 'transparent', 'groove-lo': 'transparent', 'knob-color': muted,
      'row-label-ink': foreground, 'row-value-ink': foreground, 'row-value-ink-idle': muted,
      'field-inset': inset, 'field-line': line, 'field-line-hover': accent,
      'btn-face': field, 'btn-face-hover': hover, 'btn-face-active': active, 'btn-line': line,
      'edge-hi': 'transparent', 'on-soft': soft, 'on-ink': accent,
    },
  };
}

export function installDesktopTheme({ platform, win = window, doc = document,
  fetchTheme = (options) => win.fetch('/api/desktop-theme', options), refreshMs = 5000 } = {}) {
  if (platform !== 'linux') return null;
  const media = win.matchMedia('(prefers-color-scheme: dark)');
  const root = doc.documentElement;
  let lastTheme = null, timer = null, request = null, stopped = false;
  const apply = () => {
    const theme = desktopThemeTokens(lastTheme, media.matches);
    root.dataset.desktopTheme = theme.source;
    root.dataset.desktopThemeMode = theme.mode;
    for (const [name, value] of Object.entries(theme.tokens)) root.style.setProperty(`--desktop-${name}`, value);
  };
  const schedule = () => {
    win.clearTimeout(timer);
    if (!stopped && !doc.hidden) timer = win.setTimeout(refresh, refreshMs);
  };
  async function refresh() {
    if (stopped || doc.hidden || request) return;
    win.clearTimeout(timer);
    request = new AbortController();
    const timeout = win.setTimeout(() => request?.abort(), 3000);
    try {
      const response = await fetchTheme({ cache: 'no-store', signal: request.signal });
      if (response.ok) {
        const payload = await response.json();
        if (!stopped) { lastTheme = payload; apply(); }
      }
    } catch (_) { /* Keep the current colors if the server is restarting. */ }
    finally { win.clearTimeout(timeout); request = null; schedule(); }
  }
  const visibility = () => {
    win.clearTimeout(timer);
    if (doc.hidden) request?.abort();
    else void refresh();
  };
  const focus = () => { void refresh(); };
  const stop = () => {
    stopped = true;
    win.clearTimeout(timer);
    request?.abort();
    media.removeEventListener('change', apply);
    doc.removeEventListener('visibilitychange', visibility);
    win.removeEventListener('focus', focus);
    win.removeEventListener('pagehide', stop);
  };
  media.addEventListener('change', apply);
  doc.addEventListener('visibilitychange', visibility);
  win.addEventListener('focus', focus);
  win.addEventListener('pagehide', stop);
  apply();
  void refresh();
  return { refresh, stop };
}
