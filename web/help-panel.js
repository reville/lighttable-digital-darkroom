import { HELP_CATEGORIES, helpSearch } from '/web/help-search.js';

const byId = (id) => document.getElementById(id);
const dialog = byId('helpDialog');
const search = byId('helpSearch');
const results = byId('helpResults');
const reader = byId('helpArticle');
const category = byId('helpCategory');
let articles = null;
let pendingLoad = null;
let selectedId = null;
let returnFocus = null;
let inertElements = [];
let openGeneration = 0;

// Article text is never interpreted as HTML. Source evidence stays in docs/;
// the shipped bundle contains only the reader-facing fields.
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, action, className = 'quiet') {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function showArticle(article, focus = false) {
  selectedId = article.id;
  reader.replaceChildren(element('p', article.category, 'help-eyebrow'));
  const heading = element('h2', article.title);
  heading.tabIndex = -1;
  reader.append(heading, element('p', article.summary, 'help-summary'));
  for (const section of article.sections) {
    const block = element('section');
    if (section.title) block.append(element('h3', section.title));
    for (const paragraph of section.paragraphs) block.append(element('p', paragraph));
    if (section.steps?.length) {
      const list = element('ol');
      section.steps.forEach((step) => list.append(element('li', step)));
      block.append(list);
    }
    for (const tip of section.tips || []) block.append(element('p', tip, 'help-tip'));
    reader.append(block);
  }
  const related = (article.related || []).map((id) => articles.find((item) => item.id === id)).filter(Boolean);
  if (related.length) {
    const links = element('section', null, 'help-related');
    links.append(element('h3', 'Related help'));
    related.forEach((item) => links.append(button(item.title, () => showArticle(item, true))));
    reader.append(links);
  }
  for (const item of results.querySelectorAll('[data-help-article]')) {
    item.setAttribute('aria-current', String(item.dataset.helpArticle === selectedId));
  }
  reader.scrollTop = 0;
  dialog.classList.add('help-reading');
  if (focus) heading.focus({ preventScroll: true });
}

function renderResults() {
  if (!articles) return;
  const matches = helpSearch(articles, search.value, category.value);
  results.replaceChildren();
  byId('helpResultCount').textContent = `${matches.length} ${matches.length === 1 ? 'article' : 'articles'}`;
  byId('helpClear').hidden = !search.value;
  for (const article of matches) {
    const item = button(null, () => showArticle(article, true), 'help-result');
    item.dataset.helpArticle = article.id;
    item.setAttribute('aria-current', String(article.id === selectedId));
    item.append(element('span', article.category, 'help-result-category'),
      element('span', article.title, 'help-result-title'), element('span', article.summary, 'help-result-summary'));
    results.append(item);
  }
  if (!matches.length) {
    results.append(element('p', 'No matching articles.', 'help-empty-title'),
      element('p', 'Try a tool name or a shorter phrase, such as “crop”, “missing photos”, or “export”.', 'help-empty'));
    results.append(button('Browse all help', () => {
      search.value = ''; category.value = ''; renderResults(); search.focus();
    }));
    reader.replaceChildren(element('h2', 'Let’s find the right help'),
      element('p', 'Search covers article titles, tool names, and the full instructions. Choose All topics to search across the app.'));
    selectedId = null;
    return;
  }
  showArticle(matches.find((article) => article.id === selectedId) || matches[0]);
}

async function loadArticles() {
  if (articles) return articles;
  if (!pendingLoad) pendingLoad = fetch('/web/help-content.json').then(async (response) => {
    if (!response.ok) throw new Error('Help content is unavailable.');
    const content = await response.json();
    if (content.version !== 1 || !Array.isArray(content.articles) || !content.articles.length) {
      throw new Error('Help content could not be read.');
    }
    articles = content.articles;
    return articles;
  }).finally(() => { pendingLoad = null; });
  return pendingLoad;
}

async function open(options = {}) {
  const generation = ++openGeneration;
  if (!dialog.classList.contains('on')) {
    returnFocus = document.activeElement;
    inertElements = [...document.body.children].filter((node) => node !== dialog &&
      !['SCRIPT', 'STYLE', 'LINK'].includes(node.tagName) && !node.inert);
    inertElements.forEach((node) => { node.inert = true; });
  }
  dialog.classList.add('on');
  dialog.setAttribute('aria-hidden', 'false');
  byId('helpBtn').setAttribute('aria-expanded', 'true');
  if (options.query != null) { search.value = options.query; selectedId = null; }
  if (options.category != null) category.value = options.category;
  if (options.article) selectedId = options.article;
  search.focus(); search.select();
  if (!articles) {
    byId('helpResultCount').textContent = 'Loading help…';
    reader.replaceChildren(element('p', 'Loading help…'));
  }
  try {
    await loadArticles();
    if (generation !== openGeneration) return;
    renderResults();
    if (options.article) {
      const article = articles.find((item) => item.id === options.article);
      if (article) showArticle(article);
    }
    dialog.classList.toggle('help-reading', Boolean(options.article));
  } catch {
    if (generation !== openGeneration) return;
    byId('helpResultCount').textContent = 'Help unavailable';
    reader.replaceChildren(element('h2', 'Help couldn’t be loaded'),
      element('p', 'Try again. If this continues, reopen LightTable to reload its bundled help.'),
      button('Try again', () => open(options)));
  }
}

function close() {
  if (!dialog.classList.contains('on')) return;
  ++openGeneration;
  dialog.classList.remove('on');
  dialog.setAttribute('aria-hidden', 'true');
  byId('helpBtn').setAttribute('aria-expanded', 'false');
  inertElements.forEach((node) => { node.inert = false; });
  inertElements = [];
  const target = returnFocus?.isConnected && !returnFocus.closest('[hidden]') ? returnFocus : byId('helpBtn');
  target?.focus({ preventScroll: true });
  returnFocus = null;
}

HELP_CATEGORIES.forEach((name) => category.append(new Option(name, name)));
byId('helpBtn').addEventListener('click', () => open());
byId('helpClose').onclick = close;
byId('helpClear').onclick = () => { search.value = ''; renderResults(); search.focus(); };
byId('helpBack').onclick = () => { dialog.classList.remove('help-reading'); results.querySelector('button')?.focus(); };
byId('helpShortcuts').onclick = () => { close(); window.LightTableSettings?.open('shortcuts'); };
search.addEventListener('input', () => { selectedId = null; renderResults(); dialog.classList.remove('help-reading'); });
category.addEventListener('change', () => { selectedId = null; renderResults(); dialog.classList.remove('help-reading'); });
search.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('button')?.focus(); }
  if (event.key === 'Enter') { event.preventDefault(); results.querySelector('[data-help-article]')?.click(); }
});
results.addEventListener('keydown', (event) => {
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
  const items = [...results.querySelectorAll('button')];
  const index = items.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
});
dialog.addEventListener('pointerdown', (event) => { if (event.target === dialog) close(); });

// Capture before photo/speed-key handlers. Keep text selection/copy and native
// input behavior working, but never let reading help rate or alter a photo.
document.addEventListener('keydown', (event) => {
  if (!dialog.classList.contains('on')) {
    const typing = event.target.closest?.('input, textarea, select, [contenteditable="true"]');
    if (event.key === 'F1' || (event.key === '?' && !typing && !event.metaKey && !event.ctrlKey && !event.altKey)) {
      if (document.querySelector('.modal-backdrop.on')) return;
      event.preventDefault(); event.stopImmediatePropagation(); open();
    }
    return;
  }
  if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); return; }
  if (event.key === 'F1') {
    event.preventDefault(); event.stopImmediatePropagation();
    dialog.classList.remove('help-reading'); search.focus(); search.select(); return;
  }
  if (event.key === 'Tab') {
    const focusables = [...dialog.querySelectorAll('button, input, select, a[href], [tabindex="0"]')]
      .filter((node) => !node.disabled && node.getClientRects().length);
    const first = focusables[0], last = focusables.at(-1);
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !focusables.includes(active))) {
      event.preventDefault(); last?.focus();
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault(); first?.focus();
    }
  }
  // Let target handlers (search/list navigation) run, then stop bubbling below.
}, true);
dialog.addEventListener('keydown', (event) => event.stopPropagation());
dialog.addEventListener('keyup', (event) => event.stopPropagation());
document.addEventListener('focusin', (event) => {
  if (dialog.classList.contains('on') && !dialog.contains(event.target)) search.focus();
});
window.LightTableHelp = { open, close, isOpen: () => dialog.classList.contains('on') };
