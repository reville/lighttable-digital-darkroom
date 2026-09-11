// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const action = source.slice(source.indexOf('async function trashRejected()'),
  source.indexOf('/* ------------------------------------------------------- survey controls */'));

function setup({rows = [], saved = true, result = {paths: ['/original.jpg'], photoCount: 1}, error} = {}) {
  const calls = [], messages = [];
  const context = vm.createContext({
    visible: () => rows,
    saveState: async immediate => { calls.push(['save', immediate]); return saved; },
    window: {confirm: () => { calls.push(['confirm']); return true; }},
    tr: (text, values) => ({text, values}), trn: text => text,
    toast: message => messages.push(message),
    api: async (route, body) => {
      calls.push(['plan', route, JSON.parse(JSON.stringify(body))]);
      if (error) throw error;
      return result;
    },
    sendNative: (action, body) => { calls.push(['native', action, body]); return true; },
  });
  vm.runInContext(action, context);
  return {run: () => context.trashRejected(), calls, messages};
}

test('Trash Rejected saves edits first and excludes virtual copies and keepers', async () => {
  const env = setup({rows: [
    {name: 'keeper', status: 'approved'},
    {name: 'virtual', status: 'skipped', virtual: true},
    {name: 'original', status: 'skipped', virtual: false},
  ]});
  await env.run();
  assert.deepEqual(env.calls.map(call => call[0]), ['save', 'confirm', 'plan', 'native']);
  assert.deepEqual(env.calls[2][2], {names: ['original']});
  assert.equal(env.messages.at(-1).values.rejectedLength, 1);
});

test('a view containing only rejected virtual copies has no physical Trash action', async () => {
  const env = setup({rows: [{name: 'virtual', status: 'skipped', virtual: true}]});
  await env.run();
  assert.deepEqual(env.calls.map(call => call[0]), ['save']);
});

test('unsaved edits prevent confirmation and Trash planning', async () => {
  const env = setup({saved: false, rows: [{name: 'original', status: 'skipped'}]});
  await env.run();
  assert.deepEqual(env.calls.map(call => call[0]), ['save']);
});

for (const failure of [{result: {error: 'Folder unavailable'}}, {error: Error('Disconnected')},
  {result: {paths: [], photoCount: 0}}]) {
  test(`failed or empty Trash plan is not sent to the desktop: ${JSON.stringify(failure)}`, async () => {
    const env = setup({...failure, rows: [{name: 'original', status: 'skipped'}]});
    await env.run();
    assert(!env.calls.some(call => call[0] === 'native'));
    assert(!env.messages.some(message => message?.text?.startsWith('Moving')));
  });
}

test('trashed native event triggers library reload', () => {
  const start = source.indexOf('window.lightTableNativeEvent = (event) => {');
  const end = source.indexOf('\n};\n', start) + 3;
  const handlerSource = source.slice(start, end);
  let reloaded = false;
  const context = vm.createContext({
    window: {},
    reloadLibrary: () => { reloaded = true; },
  });
  vm.runInContext(handlerSource + '\nwindow.lightTableNativeEvent({type: "trashed", count: 2});', context);
  assert.equal(reloaded, true);
});

