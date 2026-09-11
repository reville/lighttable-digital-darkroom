// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {filmChoiceValue, filmSelectionForChoice, filmParamsForStock,
  filmStockGroups, normalizeFilmTuning, mergeFilmTuning} from '../web/film-browser.js';
import {composePresetState} from '../web/presets.js';
import {transferPatch, transferChoices} from '../web/edit-transfer.js';

const profiles = [
  {id: 'portra160', name: 'Kodak Portra 160', stage: 'filming', channelModel: 'color', type: 'negative',
    targetPrint: 'endura', defaultDevelopmentTime: 3.25, tunings: [{id: 'lighttable', version: '1', description: 'Reviewed foliage rendering.'}]},
  {id: 'tri-x', name: 'Kodak Tri-X', stage: 'filming', channelModel: 'bw', type: 'negative', rustOnly: true, targetPrint: 'bw-paper'},
  {id: 'endura', name: 'Kodak Endura', stage: 'printing', channelModel: 'color', defaultDevelopmentTime: 2},
  {id: 'bw-paper', stage: 'printing', channelModel: 'bw', defaultDevelopmentTime: 4},
];
const tuned = {stock: 'portra160', film_tuning: 'lighttable', film_tuning_version: '1'};
const original = {stock: 'portra160', film_tuning: 'original', film_tuning_version: '1'};

test('stock groups are metadata-driven and put reviewed tunings before all original films', () => {
  const groups = filmStockGroups(profiles);
  assert.deepEqual(groups.map(group => group.label), ['LightTable tuned', 'Spektrafilm original']);
  assert.deepEqual(groups.map(group => group.options.map(option => option.id)),
    [['portra160::lighttable::1'], ['portra160', 'tri-x']]);
  assert.equal(groups[0].options[0].label, 'Kodak Portra 160 · LightTable tuned');
  assert.equal(groups[1].options[0].label, 'Kodak Portra 160 · Spektrafilm original');
  assert.equal(groups[1].options[1].rustOnly, true);
  assert.equal(groups[0].options[0].description, profiles[0].tunings[0].description);
  assert.deepEqual(filmStockGroups(profiles.map(({tunings, ...profile}) => profile)).map(group => group.label),
    ['Spektrafilm original'], 'an unavailable tuning never creates a duplicate option');
});

test('menu identities round-trip with a real stock ID and an explicitly versioned tuning', () => {
  assert.equal(filmChoiceValue(tuned), 'portra160::lighttable::1');
  assert.equal(filmChoiceValue({stock: 'portra160'}), 'portra160');
  assert.deepEqual(filmSelectionForChoice(filmChoiceValue(tuned), profiles), tuned);
  assert.deepEqual(filmSelectionForChoice('portra160', profiles), original);
  assert.deepEqual(filmSelectionForChoice('tri-x::lighttable::1', profiles),
    {stock: 'tri-x', film_tuning: 'original', film_tuning_version: '1'});
  assert.deepEqual(filmSelectionForChoice('portra160::lighttable::2', profiles), original,
    'unavailable revisions never silently select the current tuned revision');
});

test('switching variants retains profile metadata and switching unsupported stocks clears tuning', () => {
  const source = {...tuned, paper: 'endura', grain_amount: 1.4, workflow_mode: 'authentic', exposure_ev: 0.3};
  const next = filmParamsForStock(source, 'portra160::lighttable::1', profiles);
  assert.equal(next.stock, 'portra160');
  assert.equal(next.development_time, 3.25);
  assert.equal(next.print_development_time, 2);
  assert.equal(next.grain_amount, 1.4);
  assert.equal(next.exposure_ev, 0.3);
  const bw = filmParamsForStock(next, 'tri-x', profiles);
  assert.equal(bw.film_tuning, 'original');
  assert.equal(bw.film_tuning_version, '1');
  assert.equal(bw.paper, 'bw-paper');
  assert.equal(bw.print_development_time, 4);
  assert.equal(source.film_tuning, 'lighttable');
});

// Exercise app normalization rather than a parallel reimplementation: even
// hypothetical tuned defaults may not migrate a legacy photo or stock preset.
const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const runtime = vm.createContext({normalizeFilmTuning, mergeFilmTuning,
  S: {profiles, filmDefaults: {...tuned, grain_amount: 1}}, grainBaseline: () => 0.2});
for (const name of ['normalizeFilmParams', 'mergeFilmParams']) {
  vm.runInContext(app.match(new RegExp(`^function ${name}\\([^]*?^}`, 'm'))[0], runtime);
}
const normal = params => JSON.parse(JSON.stringify(runtime.normalizeFilmParams(params)));
const merge = (base, overlay) => JSON.parse(JSON.stringify(runtime.mergeFilmParams(base, overlay)));

test('loading old edits keeps the original rendering, while grain migration still works', () => {
  assert.deepEqual(normal({stock: 'portra160', grain_um2: 0.4}), {...original, grain_amount: 2});
  assert.equal(normal(tuned).film_tuning, 'lighttable');
  assert.equal(normal({}).film_tuning, 'lighttable');
  assert.equal(normal().film_tuning, 'lighttable');
  assert.equal(normal({profile_enabled: false}).film_tuning, 'lighttable');
  assert.equal(merge(tuned, {exposure_ev: 0.5}).film_tuning, 'lighttable');
  assert.equal(merge(tuned, {stock: 'portra160'}).film_tuning, 'original');
});

test('legacy presets select original in merge and replace modes; versioned looks preserve tuning', () => {
  const state = {params: tuned, grade: {}, masks: [], heals: [], optics: {}};
  for (const replace of [false, true]) {
    const next = composePresetState(state, {includeFilm: true, params: {stock: 'portra160'}},
      {replace, normalizeFilmParams: normal, mergeFilmParams: merge});
    assert.equal(next.params.film_tuning, 'original');
  }
  const legacyLook = composePresetState(state,
    {scope: 'look', includedFilm: ['stock'], params: {stock: 'portra160'}}, {mergeFilmParams: merge});
  assert.equal(legacyLook.params.film_tuning, 'original');
  const tunedLook = composePresetState({...state, params: original},
    {scope: 'look', includedFilm: Object.keys(tuned), params: tuned}, {mergeFilmParams: merge});
  assert.equal(tunedLook.params.film_tuning, 'lighttable');
  assert.equal(tunedLook.params.film_tuning_version, '1');
  assert.deepEqual(state.params, tuned);
});

test('film-only edit transfer carries variant and removes a destination variant for legacy sources', () => {
  const choices = Object.fromEntries(Object.keys(transferChoices()).map(key => [key, key === 'film']));
  const destination = {params: {...tuned, wb_temperature: 5000}};
  const legacy = transferPatch({params: {stock: 'portra160'}}, destination, choices);
  assert.equal(normal(legacy.params).film_tuning, 'original');
  assert.equal(legacy.params.wb_temperature, 5000);
  const copied = transferPatch({params: tuned}, {params: original}, choices);
  assert.equal(copied.params.film_tuning, 'lighttable');
  assert.equal(copied.params.film_tuning_version, '1');
});
