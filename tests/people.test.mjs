import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const text = (await readFile(new URL('../web/people.js', import.meta.url), 'utf8'))
  .replaceAll("'/web/", `'${new URL('../web/', import.meta.url).href}`);
const { visiblePeople, personName, pendingMatches } = await import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const groups = [{id:'a', name:'Alice', hidden:0}, {id:'b', name:'', hidden:0}, {id:'c', name:'Bob', hidden:1}];
test('People views keep hidden and unnamed groups distinct', () => {
  assert.deepEqual(visiblePeople(groups, 'all').map(g => g.id), ['a', 'b']);
  assert.deepEqual(visiblePeople(groups, 'unnamed').map(g => g.id), ['b']);
  assert.deepEqual(visiblePeople(groups, 'hidden').map(g => g.id), ['c']);
  assert.deepEqual(visiblePeople(groups, 'all', ' ALI ').map(g => g.id), ['a']);
  assert.equal(personName(groups[1]), 'Unnamed person');
});

const proposed = [{a:'a', b:'b'}, {a:'b', b:'c'}, {a:'a', b:'c'}];
const pairKeys = pairs => pairs.map(p => `${p.a}/${p.b}`);
test('the review count counts down as matches are set aside', () => {
  assert.deepEqual(pairKeys(pendingMatches(proposed, new Set())), ['a/b', 'b/c', 'a/c']);
  assert.deepEqual(pairKeys(pendingMatches(proposed, new Set(['b/c']))), ['a/b', 'a/c']);
  assert.deepEqual(pendingMatches(proposed, new Set(['a/b', 'b/c', 'a/c'])), []);
});

const libraryText = (await readFile(new URL('../web/library.js', import.meta.url), 'utf8'))
  .replaceAll("'/web/", `'${new URL('../web/', import.meta.url).href}`);
const { photoMatchesQuery, photoMatchesRules } = await import(`data:text/javascript;base64,${Buffer.from(libraryText).toString('base64')}`);
test('person names work with library search and saved smart-collection rules', () => {
  const photo = {name:'1:IMG_0001.jpg', people:['Alice Smith'], rating:4};
  assert.equal(photoMatchesQuery(photo, 'alice'), true);
  assert.equal(photoMatchesQuery(photo, 'BOB'), false);
  assert.equal(photoMatchesRules(photo, {query:'Alice Smith', ratingMin:4}), true);
  assert.equal(photoMatchesRules(photo, {query:'Alice Smith', ratingMin:5}), false);
  assert.equal(photoMatchesQuery({...photo, people:[]}, 'alice'), false);
});
