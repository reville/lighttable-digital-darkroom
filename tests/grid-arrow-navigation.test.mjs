/* Up and down in a grid. The keys used to fall through to the browser, which
 * scrolled the library and left the selection on whatever photo was already
 * active — the wrong answer while culling, where every press should judge a
 * different photo. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { createGridLayout, gridRowNeighbour } from '../web/view-performance.js';

const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const surveySource = readFileSync(new URL('../web/survey.js', import.meta.url), 'utf8');

const photos = (count) => Array.from({ length: count },
  (_, index) => ({ name: `photo-${index}.jpg`, width: 3000, height: 2000 }));

function surveyRowNeighbour(...args) {
  const context = vm.createContext({});
  vm.runInContext(surveySource.replace(/^import .*;$/gm, '')
    .replace(/export function/g, 'function'), context);
  return context.surveyRowNeighbour(...args);
}

/* The library grid: square rows are uniform, the photo grid packs lanes. */
function gridHarness(list, options) {
  const layout = createGridLayout(list, options);
  const scrolled = [];
  const context = vm.createContext({
    gridRowNeighbour,
    _gridLayout: layout,
    _gridList: list,
    _gridIndexByName: new Map(list.map((image, index) => [image.name, index])),
    S: { images: list },
    current: list[0],
    cur() { return context.current; },
    visible() { return list; },
    renderGrid() {},
    gridViewportTop() { return 0; },
    go(index) { context.current = list[index]; },
    $: () => ({
      classList: { contains: () => true },
      querySelector: () => ({ offsetHeight: 0 }),
      clientHeight: 10_000,
      getBoundingClientRect: () => ({ top: 0 }),
      get scrollTop() { return 0; },
      set scrollTop(value) { scrolled.push(value); },
    }),
  });
  vm.runInContext(app.slice(app.indexOf('function scrollGridToImage('),
    app.indexOf('const _rawDefaultCache = new Map();')), context);
  return {
    context,
    press(direction) { return context.goGridRow(direction); },
    select(name) { context.current = list.find((image) => image.name === name); },
    active() { return context.current?.name; },
  };
}

test('down and up move a whole row in the square culling grid', () => {
  // 900px of grid at the 180px cell size is four columns.
  const list = photos(12);
  const grid = gridHarness(list, { width: 900, cell: 180, photo: false });
  assert.equal(grid.press(1), true);
  assert.equal(grid.active(), 'photo-4.jpg', 'one row down, not one photo');
  assert.equal(grid.press(1), true);
  assert.equal(grid.active(), 'photo-8.jpg');
  assert.equal(grid.press(-1), true);
  assert.equal(grid.active(), 'photo-4.jpg');
});

test('the key is left alone at the top and bottom of a column', () => {
  const list = photos(12);
  const grid = gridHarness(list, { width: 900, cell: 180, photo: false });
  assert.equal(grid.press(-1), false, 'nothing above the first row');
  assert.equal(grid.active(), 'photo-0.jpg');
  grid.select('photo-11.jpg');
  assert.equal(grid.press(1), false, 'nothing below the last row');
  assert.equal(grid.active(), 'photo-11.jpg');
});

test('a short final row keeps the photos above it reachable', () => {
  // Ten photos across four columns leave the last row two cells wide.
  const list = photos(10);
  const grid = gridHarness(list, { width: 900, cell: 180, photo: false });
  grid.select('photo-6.jpg');
  assert.equal(grid.press(1), false, 'that column ends at the middle row');
  assert.equal(grid.active(), 'photo-6.jpg');
  grid.select('photo-5.jpg');
  assert.equal(grid.press(1), true);
  assert.equal(grid.active(), 'photo-9.jpg');
});

test('the photo grid moves within a packed column, not by a fixed stride', () => {
  // Mixed aspect ratios make the lanes fill to different depths, so the photo
  // below is whatever landed next in the same lane.
  const list = photos(9).map((image, index) => index % 3 === 0
    ? { ...image, width: 2000, height: 3000 } : image);
  const layout = createGridLayout(list, { width: 900, cell: 180, photo: true });
  const grid = gridHarness(list, { width: 900, cell: 180, photo: true });
  assert.equal(grid.press(1), true);
  const expected = layout.lanes[layout.positions[0].column][1];
  assert.equal(grid.active(), list[expected.index].name);
  assert.equal(grid.press(-1), true);
  assert.equal(grid.active(), 'photo-0.jpg');
});

test('an empty grid and an unselected grid never throw', () => {
  assert.equal(gridHarness([], { width: 900, cell: 180 }).press(1), false);
  const grid = gridHarness(photos(6), { width: 900, cell: 180 });
  grid.context.current = { name: 'not-in-this-view.jpg' };
  assert.equal(grid.press(1), true);
  assert.equal(grid.active(), 'photo-0.jpg', 'down starts at the first photo');
});

test('grid row neighbours are the cells directly above and below', () => {
  const layout = createGridLayout(photos(12), { width: 900, cell: 180, photo: false });
  assert.equal(gridRowNeighbour(layout, 0, 1), 4);
  assert.equal(gridRowNeighbour(layout, 4, -1), 0);
  assert.equal(gridRowNeighbour(layout, 0, -1), -1);
  assert.equal(gridRowNeighbour(layout, 11, 1), -1);
  assert.equal(gridRowNeighbour(layout, 99, 1), -1);
});

test('survey rows move a row and wrap the way its left and right keys do', () => {
  // Five cells across three columns: rows [0,1,2] and [3,4].
  assert.equal(surveyRowNeighbour(1, 5, 3, 1), 4);
  assert.equal(surveyRowNeighbour(4, 5, 3, -1), 1);
  assert.equal(surveyRowNeighbour(2, 5, 3, 1), 2, 'that column has one row');
  assert.equal(surveyRowNeighbour(0, 5, 3, -1), 3, 'wraps to the last full cell');
  assert.equal(surveyRowNeighbour(3, 5, 3, 1), 0);
  assert.equal(surveyRowNeighbour(0, 1, 1, 1), 0);
  assert.equal(surveyRowNeighbour(0, 0, 1, 1), -1);
});
