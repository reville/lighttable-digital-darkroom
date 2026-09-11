// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { desktopThemeTokens, installDesktopTheme } from '../web/desktop-theme.js';

const dark = { source: 'omarchy', mode: 'dark', colors: { background: '#1a1b26', foreground: '#a9b1d6', accent: '#7aa2f7' } };
const light = { source: 'omarchy', mode: 'light', colors: { background: '#faf4ed', foreground: '#575279', accent: '#b4637a' } };
const settle = async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); };

test('follows Omarchy palette without changing canvas, image or semantic tokens', () => {
  for (const payload of [dark, light]) {
    const theme = desktopThemeTokens(payload);
    assert.equal(theme.source, 'omarchy');
    assert.equal(theme.tokens.chrome, payload.colors.background);
    assert.equal(theme.tokens.ink, payload.colors.foreground);
    assert.equal(theme.mode, payload.mode);
    for (const key of ['canvas', 'bg', 'ok', 'skip', 'warn', 'filter', 'opacity']) assert.equal(theme.tokens[key], undefined);
  }
});

test('infers legacy theme mode and tracks the system when Omarchy is absent', () => {
  assert.equal(desktopThemeTokens({ ...light, mode: null }).mode, 'light');
  assert.equal(desktopThemeTokens({ ...dark, mode: null }).mode, 'dark');
  assert.equal(desktopThemeTokens(null, false).tokens.chrome, '#f2f2f2');
  assert.equal(desktopThemeTokens(null, true).tokens.chrome, '#222222');
});

test('invalid color input cannot become CSS and unreadable palettes get legible text', () => {
  const result = desktopThemeTokens({ source: 'omarchy', colors: {
    background: 'url(https://example.invalid)', foreground: '#222222', accent: 'red; --bg: red',
  } });
  assert.equal(result.source, 'system');
  assert.notEqual(result.tokens.ink, '#222222');
  for (const value of Object.values(result.tokens)) assert.match(value, /^(#[\da-f]{6}|rgba\([\d,\.]+\)|transparent)$/);
});

function fixture(platform = 'linux') {
  const properties = new Map(), timers = new Map(), windowEvents = new Map(), documentEvents = new Map(), mediaEvents = new Map();
  let nextTimer = 0, payload = dark, fail = false, fetches = 0;
  const media = { matches: true,
    addEventListener: (name, fn) => mediaEvents.set(name, fn), removeEventListener: (name) => mediaEvents.delete(name) };
  const doc = { hidden: false, documentElement: { dataset: {}, style: { setProperty: (key, value) => properties.set(key, value) } },
    addEventListener: (name, fn) => documentEvents.set(name, fn), removeEventListener: (name) => documentEvents.delete(name) };
  const win = { matchMedia: () => media,
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; }, clearTimeout: (id) => timers.delete(id),
    addEventListener: (name, fn) => windowEvents.set(name, fn), removeEventListener: (name) => windowEvents.delete(name) };
  const controller = installDesktopTheme({ platform, win, doc, fetchTheme: async (options) => {
    fetches++; assert.equal(options.cache, 'no-store');
    if (fail) throw new Error('Server restarting');
    return { ok: true, json: async () => payload };
  } });
  return { controller, properties, timers, windowEvents, documentEvents, mediaEvents, media, doc,
    set: (next) => { payload = next; }, fail: () => { fail = true; }, fetches: () => fetches };
}

test('refreshes on live theme switches, retains colors on network error, pauses while hidden and cleans up', async () => {
  const f = fixture(); await settle();
  assert.equal(f.properties.get('--desktop-chrome'), '#1a1b26');
  assert.equal(f.timers.size, 1);
  assert.equal([...f.timers.values()][0].delay, 5000);
  f.set(light); await f.controller.refresh();
  assert.equal(f.properties.get('--desktop-chrome'), '#faf4ed');
  assert.equal(f.doc.documentElement.dataset.desktopThemeMode, 'light');
  f.fail(); await f.controller.refresh();
  assert.equal(f.properties.get('--desktop-chrome'), '#faf4ed');
  f.doc.hidden = true; f.documentEvents.get('visibilitychange')();
  const count = f.fetches(); await f.controller.refresh();
  assert.equal(f.fetches(), count); assert.equal(f.timers.size, 0);
  f.doc.hidden = false; f.documentEvents.get('visibilitychange')(); await settle();
  assert.equal(f.fetches(), count + 1);
  f.controller.stop();
  assert.equal(f.timers.size, 0); assert.equal(f.windowEvents.size, 0); assert.equal(f.documentEvents.size, 0); assert.equal(f.mediaEvents.size, 0);
  assert.ok([...f.properties.keys()].every((key) => key.startsWith('--desktop-')));
});

test('live system appearance changes apply when no Omarchy palette is available', async () => {
  const f = fixture(); await settle();
  f.set({ source: 'system', mode: null, colors: {} }); await f.controller.refresh();
  f.media.matches = false; f.mediaEvents.get('change')();
  assert.equal(f.properties.get('--desktop-chrome'), '#f2f2f2');
  assert.equal(f.doc.documentElement.dataset.desktopTheme, 'system');
  f.controller.stop();
});

test('Mac and Windows never fetch or apply the Linux theme', async () => {
  for (const platform of ['darwin', 'win32', 'macos', 'windows', null]) {
    const f = fixture(platform); await settle();
    assert.equal(f.controller, null); assert.equal(f.fetches(), 0); assert.equal(f.properties.size, 0); assert.equal(f.timers.size, 0);
  }
});
