/* Auto-advance during a cull. Marking a photo can remove it from the visible
 * list, and the old code then advanced from index -1 and landed on the first
 * photo in the library, so the next keystroke marked a photo nobody had judged. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function markHarness() {
  const context = vm.createContext({SURVEY: null});
  vm.runInContext(
    'let _stripKey = "", _gridKey = "";\n'
    + app.slice(app.indexOf('function markResumeIndex('),
                app.indexOf('function setViewMode(')),
    context);
  return context;
}

const photos = names => names.map(name => ({name}));

test('a marked photo that stays in view advances to its neighbour', () => {
  const {photoAfterMark} = markHarness();
  const list = photos(['a', 'b', 'c', 'd']);
  assert.equal(photoAfterMark(list, list[1], 1).name, 'c');
});

test('a mark that filters the photo out resumes at the following photo', () => {
  const {photoAfterMark} = markHarness();
  const before = photos(['a', 'b', 'c', 'd']);
  // 'c' was picked, so a "Unflagged" filter no longer admits it.
  const after = before.filter(image => image.name !== 'c');
  const next = photoAfterMark(after, before[2], 2);
  assert.equal(next.name, 'd', 'should continue the cull, not restart it');
  assert.notEqual(next.name, 'a', 'must not jump to the first photo');
});

test('marking the last visible photo stays at the end of the list', () => {
  const {photoAfterMark} = markHarness();
  const before = photos(['a', 'b', 'c']);
  const after = before.filter(image => image.name !== 'c');
  assert.equal(photoAfterMark(after, before[2], 2).name, 'b');
});

test('marking the only visible photo leaves nowhere to advance', () => {
  const {photoAfterMark} = markHarness();
  assert.equal(photoAfterMark([], photos(['a'])[0], 0), undefined);
});

test('an absent resume index does not resume', () => {
  const {photoAfterMark} = markHarness();
  const before = photos(['a', 'b', 'c']);
  const after = before.filter(image => image.name !== 'b');
  assert.equal(photoAfterMark(after, before[1], -1), undefined);
});

test('a DOM event in place of an index is ignored rather than mis-indexed', () => {
  const {photoAfterMark} = markHarness();
  // refreshFilteredView is bound directly to select onchange handlers, so the
  // first argument can be an Event. It must not be read as a list position.
  const before = photos(['a', 'b', 'c']);
  const after = before.filter(image => image.name !== 'b');
  assert.equal(photoAfterMark(after, before[1], {type: 'change'}), undefined);
});
