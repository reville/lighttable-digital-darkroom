import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {t as tr} from '../web/i18n.js';

const source = readFileSync(new URL('../web/desktop-updates.js', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness({platform = 'linux', bridge = true, save = true, post, status} = {}) {
  const nodes = new Map(), calls = [], native = [], errors = [], timers = [];
  let blocked = false;
  const window = {__LIGHTTABLE_PLATFORM__: platform};
  const document = {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, disabled: false, textContent: '', handlers: {},
      addEventListener(name, fn) { this.handlers[name] = fn; }});
    return nodes.get(id);
  }};
  const context = vm.createContext({window, document, tr, Date,
    nativeBridge: () => bridge,
    sendNative: (name, body) => { native.push({name, body}); return bridge; },
    api: async (path, body) => { calls.push(path); return post ? post(path, body) : {ok: true, helper_directory: '/cache/updates/pending-test'}; },
    fetch: async () => ({ok: true, json: async () => status || {supported: true, state: 'idle'}}),
    setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; }, clearTimeout() {},
  });
  vm.runInContext(source.replace(/^import .*;$/gm, '').replace('export function', 'function'), context);
  const ui = context.installDesktopUpdates({
    prepare: async () => { blocked = true; if (!save) blocked = false; return save; },
    cancel: () => { blocked = false; }, onError: message => errors.push(message),
  });
  return {window, ui, calls, native, errors, timers, el: id => document.getElementById(id),
    blocked: () => blocked, click: id => document.getElementById(id).handlers.click()};
}

test('failed saves never prepare or launch an update', async () => {
  const h = harness({save: false, status: {supported: true, state: 'ready'}});
  await settle(); await h.click('checkForUpdates');
  assert.deepEqual(h.calls, []);
  assert.deepEqual(h.native, []);
  assert.equal(h.blocked(), false);
});

test('busy server cancels preparation and restores editing without an installer', async () => {
  const h = harness({status: {supported: true, state: 'ready'},
    post: path => path.endsWith('/prepare') ? {error: 'Export is running'} : {ok: true}});
  await settle(); await h.click('checkForUpdates');
  assert.deepEqual(h.calls, ['/api/updates/prepare', '/api/updates/cancel']);
  assert.deepEqual(h.native, []);
  assert.equal(h.blocked(), false);
  assert.deepEqual(h.errors, ['Export is running']);
});

test('ready Linux update waits for native helper validation before shutting down', async () => {
  const h = harness({status: {supported: true, state: 'ready'}});
  await settle(); await h.click('checkForUpdates');
  assert.deepEqual(h.calls, ['/api/updates/prepare', '/api/updates/apply']);
  assert.equal(h.native[0].name, 'finishUpdateShutdown');
  assert.equal(h.native[0].body.helper_directory, '/cache/updates/pending-test');
  assert.equal(h.blocked(), true);
  assert.equal(await h.window.lightTableShutdownForUpdate(), true);
  assert.equal(h.calls.at(-1), '/api/updates/shutdown');
});

test('failed apply cancels the detached worker before restoring editing', async () => {
  const h = harness({status: {supported: true, state: 'ready'},
    post: path => path.endsWith('/apply') ? {error: 'Could not start helper'} : {ok: true}});
  await settle(); await h.click('checkForUpdates');
  assert.deepEqual(h.calls, ['/api/updates/prepare', '/api/updates/apply', '/api/updates/cancel']);
  assert.deepEqual(h.native, []);
  assert.equal(h.blocked(), false);
  assert.deepEqual(h.errors, ['Could not start helper']);
});

test('managed installs and ordinary browsers cannot invoke a self update', async () => {
  for (const options of [
    {status: {supported: false, state: 'disabled', owner: 'arch'}},
    {bridge: false, status: {supported: true, state: 'ready'}},
  ]) {
    const h = harness(options);
    await h.window.lightTableRefreshUpdates();
    assert.equal(h.el('checkForUpdates').disabled, true);
    assert.equal(h.el('automaticUpdateChecks').disabled, true);
    await h.click('checkForUpdates');
    assert.deepEqual(h.calls, []);
    assert.deepEqual(h.native, []);
  }
});

test('Windows uses the native updater and honors its package ownership status', async () => {
  const h = harness({platform: 'windows'});
  assert.equal(h.native[0].name, 'requestUpdateStatus');
  h.ui.nativeEvent({type: 'updateStatus', supported: true});
  await h.click('checkForUpdates');
  assert.equal(h.native.at(-1).name, 'checkForUpdates');
  assert.deepEqual(h.calls, []);
  h.ui.nativeEvent({type: 'updateStatus', supported: false, managedBy: 'WinGet'});
  assert.match(h.el('desktopUpdateStatus').textContent, /WinGet/);
  assert.equal(h.el('checkForUpdates').disabled, true);
});

test('Later dismisses an available update for the rest of the session', async () => {
  const h = harness({status: {supported: true, state: 'available', available_version: '1.2.3'}});
  await settle(); await h.timers.find(timer => timer.delay === 20000).fn();
  assert.equal(h.el('desktopUpdateNotice').hidden, false);
  await h.click('desktopUpdateLater');
  assert.equal(h.el('desktopUpdateNotice').hidden, true);
  await h.click('checkForUpdates');
  assert.equal(h.el('desktopUpdateNotice').hidden, true);
});
