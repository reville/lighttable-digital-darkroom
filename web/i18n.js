// SPDX-License-Identifier: GPL-3.0-only
// Explicit gettext templates keep translations separate from file names, edit
// values, identifiers and user content. This module has no import-time effects.
let locale = 'en';
let catalog = { messages: {}, plurals: {} };
let manifest = { locales: [{ code: 'en', name: 'English', nativeName: 'English', dir: 'ltr' }] };
const cache = new Map();

export function currentLocale() { return locale; }
export function availableLocales() { return manifest.locales; }
export function interpolate(template, values = {}) {
  return String(template).replace(/\{(\w+)\}/g, (token, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : token);
}
export function t(source, values = {}) {
  return interpolate(catalog.messages?.[source] ?? source, values);
}
export function tn(one, other, count, values = {}) {
  const category = new Intl.PluralRules(locale).select(Number(count));
  const source = Number(count) === 1 ? one : other;
  const template = catalog.plurals?.[one]?.[category] ?? catalog.messages?.[source] ?? source;
  return interpolate(template, { ...values, count: formatNumber(count) });
}
export function formatNumber(value, options = {}) {
  return new Intl.NumberFormat(locale, options).format(Number(value));
}
export function formatDate(value, options = {}) {
  return new Intl.DateTimeFormat(locale, options).format(new Date(value));
}
export function resolveLocale(language, supported = manifest.locales.map(item => item.code)) {
  const tag = String(language || '').replaceAll('_', '-').toLowerCase();
  if (!tag) return 'en';
  const exact = supported.find(code => code.toLowerCase() === tag);
  if (exact) return exact;
  if (tag === 'zh' || tag.startsWith('zh-')) {
    const chinese = /(?:^|-)hans(?:-|$)/.test(tag) ? 'zh-Hans'
      : /(?:^|-)(hant|tw|hk|mo)(?:-|$)/.test(tag) ? 'zh-Hant' : 'zh-Hans';
    return supported.includes(chinese) ? chinese : 'en';
  }
  return supported.find(code => code.toLowerCase() === tag.split('-')[0]) || 'en';
}
export function launchLanguage(prefs, systemLanguages, supported = manifest.locales.map(item => item.code)) {
  if (prefs?.localeChosen === true && supported.includes(prefs.locale)) {
    return { locale: prefs.locale, ask: false, save: false };
  }
  const primary = systemLanguages?.[0] || 'en';
  return { locale: resolveLocale(primary, supported), ask: !/^en(?:[-_]|$)/i.test(primary), save: true };
}
export function useCatalog(code, next, nextManifest = manifest) {
  locale = code; catalog = next; manifest = nextManifest;
}
export async function loadLocale(code, fetcher = globalThis.fetch) {
  if (cache.has(code)) return cache.get(code);
  const response = await fetcher(`/web/locales/${encodeURIComponent(code)}.json`);
  if (!response.ok) throw new Error(t('Could not load this language. Try again.'));
  const next = await response.json();
  if (next.version !== 1 || next.locale !== code || !next.messages || typeof next.messages !== 'object') {
    throw new Error(t('Could not load this language. Try again.'));
  }
  cache.set(code, next);
  return next;
}
export async function readLanguagePreferences(fetcher = globalThis.fetch) {
  const response = await fetcher('/api/prefs');
  if (!response.ok) throw new Error(t('Could not read language preferences. Try again.'));
  const prefs = await response.json();
  if (!prefs || typeof prefs !== 'object' || Array.isArray(prefs) || prefs.error) {
    throw new Error(t('Could not read language preferences. Try again.'));
  }
  return prefs;
}
export async function saveLanguage(code, fetcher = globalThis.fetch) {
  // Load before writing: a missing bundle must never become a saved choice.
  const next = await loadLocale(code, fetcher);
  const response = await fetcher('/api/prefs', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ locale: code, localeChosen: true }) });
  const result = await response.json();
  if (!response.ok || result?.ok !== true || result.error) {
    throw new Error(t('Could not save the language. Try again.'));
  }
  return next;
}
export function translateStatic(root = document) {
  for (const node of root.querySelectorAll('[data-i18n-text]')) {
    const messages = JSON.parse(node.dataset.i18nText);
    for (const [index, source] of Object.entries(messages)) {
      const child = node.childNodes[Number(index)];
      if (child?.nodeType === 3) {
        const before = child.textContent.match(/^\s*/)?.[0] || '';
        const after = child.textContent.match(/\s*$/)?.[0] || '';
        child.textContent = before + t(source) + after;
      }
    }
  }
  for (const node of root.querySelectorAll('[data-i18n-attrs]')) {
    for (const [name, source] of Object.entries(JSON.parse(node.dataset.i18nAttrs))) node.setAttribute(name, t(source));
  }
}
export async function initializeLanguage({ fetcher = globalThis.fetch, languages, choose } = {}) {
  const response = await fetcher('/web/locales/manifest.json');
  if (!response.ok) throw new Error(t('Could not load available languages. Try again.'));
  manifest = await response.json();
  if (manifest.version !== 1 || !Array.isArray(manifest.locales)) throw new Error(t('Could not load available languages. Try again.'));
  const prefs = await readLanguagePreferences(fetcher);
  const decision = launchLanguage(prefs, languages);
  let code = decision.locale;
  let next;
  if (decision.ask) {
    code = await choose(code, manifest.locales);
    next = await loadLocale(code, fetcher); // chooser persisted only on successful Continue
  } else if (decision.save) next = await saveLanguage(code, fetcher);
  else next = await loadLocale(code, fetcher);
  useCatalog(code, next);
  return code;
}
