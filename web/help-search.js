// Search is shared by the help UI and its behavioral tests. No server or index
// service is needed; all searchable text comes from the bundled articles.
const STOP_WORDS = new Set(['a', 'an', 'and', 'are', 'can', 'do', 'does', 'how',
  'i', 'in', 'is', 'it', 'my', 'of', 'on', 'the', 'to', 'with']);
export const HELP_CATEGORIES = ['Getting started', 'Library', 'Editing', 'Film',
  'Export', 'Settings', 'Troubleshooting'];
// Direct article IDs keep contextual help stable when search ranking changes.
// Tests validate every destination against the shipped content.
export const HELP_SECTION_TOPICS = {
  Editing: {
    Light: 'editing-tone', Color: 'editing-white-balance', Curve: 'editing-curves',
    'Color Grading': 'editing-color-grading', Effects: 'editing-effects',
    Detail: 'editing-detail-effects-enhance', 'Lens corrections': 'editing-lens-corrections',
    Profile: 'settings-and-defaults',
  },
  Film: {
    'Film profile': 'film-getting-started', 'Physical film stages': 'film-halation-diffusion-scan',
    'Reference Match': 'film-reference-match',
  },
};

export function normalizeHelpText(value) {
  return String(value).normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

export function helpSearch(articles, query = '', category = '') {
  const phrase = normalizeHelpText(query);
  const terms = [...new Set(phrase.split(' ').filter((term) => term && !STOP_WORDS.has(term)))];
  return articles.flatMap((article, index) => {
    if (category && article.category !== category) return [];
    const title = normalizeHelpText(article.title);
    const keywords = normalizeHelpText(article.keywords.join(' '));
    const summary = normalizeHelpText(article.summary);
    const body = normalizeHelpText(article.sections.map((section) =>
      [section.title, ...section.paragraphs, ...(section.steps || []), ...(section.tips || [])].join(' ')).join(' '));
    const fields = [title, keywords, summary, body, normalizeHelpText(article.category)];
    if (!terms.every((term) => fields.some((field) => field.includes(term)))) return [];
    const score = terms.reduce((sum, term) => sum + (title.includes(term) ? 12 : 0)
      + (keywords.includes(term) ? 7 : 0) + (summary.includes(term) ? 4 : 0)
      + (body.includes(term) ? 1 : 0), 0) + (phrase && title.includes(phrase) ? 25 : 0);
    return [{ article, score, index }];
  }).sort((a, b) => b.score - a.score || a.index - b.index).map(({ article }) => article);
}
