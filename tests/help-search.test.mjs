import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { HELP_SECTION_TOPICS, helpSearch, normalizeHelpText } from '../web/help-search.js';

const articles = [
  { id: 'export', title: 'Export photos', category: 'Export',
    summary: 'Save a finished file.', keywords: ['save', 'jpeg', 'tiff'],
    sections: [{ title: 'Prepare', paragraphs: ['Check the crop before saving.'],
      steps: ['Choose a destination folder.'], tips: ['Use a new filename.'] }] },
  { id: 'crop', title: 'Crop and straighten', category: 'Editing',
    summary: 'Change framing and aspect ratio.', keywords: ['rotate', 'horizon'],
    sections: [{ title: 'Frame', paragraphs: ['Drag the edges.'], steps: ['Choose Done.'] }] },
];

test('title matches rank above an incidental body mention', () => {
  assert.equal(helpSearch(articles, 'crop')[0].id, 'crop');
});
test('natural questions and partial tool names work', () => {
  assert.equal(helpSearch(articles, 'How do I crop?')[0].id, 'crop');
  assert.equal(helpSearch(articles, 'straigh')[0].id, 'crop');
});
test('all query terms must match, including steps and tips', () => {
  assert.deepEqual(helpSearch(articles, 'destination filename').map((a) => a.id), ['export']);
  assert.deepEqual(helpSearch(articles, 'crop spacecraft'), []);
});
test('categories apply to empty and nonempty searches', () => {
  assert.deepEqual(helpSearch(articles, '', 'Editing').map((a) => a.id), ['crop']);
  assert.deepEqual(helpSearch(articles, 'crop', 'Export').map((a) => a.id), ['export']);
});
test('punctuation, case, accents, and empty results are harmless', () => {
  assert.equal(normalizeHelpText('CAFÉ / RAW+JPEG'), 'cafe raw jpeg');
  assert.equal(helpSearch(articles, '?!').length, 2);
  assert.deepEqual(helpSearch(articles, '<script>'), []);
});
test('every shipped title finds its own article first', () => {
  const content = JSON.parse(readFileSync(new URL('../web/help-content.json', import.meta.url)));
  for (const article of content.articles) {
    assert.equal(helpSearch(content.articles, article.title)[0]?.id, article.id, article.title);
  }
});
test('shipped help answers common workflow searches', () => {
  const { articles: shipped } = JSON.parse(readFileSync(new URL('../web/help-content.json', import.meta.url)));
  for (const query of ['crop', 'mask', 'export', 'missing photos', 'grain', 'keyboard', 'white balance', 'presets']) {
    assert.ok(helpSearch(shipped, query).length > 0, query);
  }
});
test('every contextual help button has a bundled destination and a real section label', () => {
  const { articles: shipped } = JSON.parse(readFileSync(new URL('../web/help-content.json', import.meta.url)));
  const ids = new Set(shipped.map((article) => article.id));
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  for (const topics of Object.values(HELP_SECTION_TOPICS)) {
    for (const [label, id] of Object.entries(topics)) {
      assert.ok(ids.has(id), `Missing contextual help: ${id}`);
      assert.ok(new RegExp(`<span[^>]*>${label}</span>`).test(html), `Missing section: ${label}`);
    }
  }
});


test('search retains Chinese, Arabic and Indic writing systems', () => {
  for (const [title, query] of [['裁剪和拉直照片','裁剪'],['اقتصاص الصور','اقتصاص'],['फ़ोटो में बदलाव','फ़ोटो'],['ছবি সম্পাদনা','ছবি'],['ปรับแต่งภาพ','ภาพ']]) {
    const translated = [{...articles[0], title, summary:'', keywords:[], sections:[], categoryLabel:title}];
    assert.equal(helpSearch(translated, query)[0]?.id, 'export', query);
    assert.notEqual(normalizeHelpText(query), '');
  }
});

test('translated categories display and search while keeping canonical filters', () => {
  const translated = [{ ...articles[0], title: 'Enregistrer une photo',
    categoryLabel: 'Sortie', summary: '', keywords: [],
    sections: [{title: 'Préparer', steps: ['Choisir un dossier.']}] }];
  assert.equal(helpSearch(translated, 'sortie', 'Export')[0]?.id, 'export');
  assert.equal(helpSearch(translated, 'dossier', 'Export')[0]?.id, 'export');
  assert.deepEqual(helpSearch(translated, '', 'Sortie'), []);
});
