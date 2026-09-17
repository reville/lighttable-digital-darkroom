// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installCrashReportConsent, shouldAskCrashReports } from '../web/crash-report-consent.js';

const IDS = ['crashReportDialog', 'crashReportAllow', 'crashReportDecline',
  'crashReportPending', 'crashReportError', 'crashReportIntro'];
const wait = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));

test('asks only when this build can send reports and nothing was chosen yet', () => {
  assert.equal(shouldAskCrashReports(null), false);
  assert.equal(shouldAskCrashReports({ available: false, consent: null }), false);
  assert.equal(shouldAskCrashReports({ available: true, consent: null }), true);
  assert.equal(shouldAskCrashReports({ available: true }), true);
  assert.equal(shouldAskCrashReports({ available: true, consent: true }), false);
  assert.equal(shouldAskCrashReports({ available: true, consent: false }), false);
});

function fixture({ status, busy = () => false, postResult = { ok: true } } = {}) {
  class Element {
    hidden = false; disabled = false; textContent = '';
    constructor(id) {
      this.id = id;
      this.attributes = {};
      this.handlers = {};
      const classes = new Set();
      this.classList = {
        contains: (name) => classes.has(name),
        toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      };
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
    click() { for (const handler of this.handlers.click || []) handler({}); }
  }
  const elements = new Map(IDS.map((id) => [id, new Element(id)]));
  const listeners = [];
  globalThis.document = {
    addEventListener(type, handler, capture) {
      assert.equal(capture, true, 'dialog keys are captured before editor shortcuts');
      if (type === 'keydown') listeners.push(handler);
    },
  };
  const posts = [], choices = [];
  const consent = installCrashReportConsent({
    el: (id) => elements.get(id),
    getJSON: async (path) => { assert.equal(path, '/api/crash-reports'); return status; },
    post: async (path, body) => {
      posts.push([path, body]);
      if (postResult instanceof Error) throw postResult;
      return postResult;
    },
    isBusy: busy, onChoice: (value) => choices.push(value), delay: 0, retry: 5,
  });
  const key = (name) => {
    const event = { key: name, stopped: false, prevented: false,
      stopImmediatePropagation() { this.stopped = true; },
      preventDefault() { this.prevented = true; } };
    for (const handler of listeners) handler(event);
    return event;
  };
  const dialog = elements.get('crashReportDialog');
  return { consent, elements, posts, choices, key, open: () => dialog.classList.contains('on'), dialog };
}

test('opens once at startup and saves consent', async () => {
  const view = fixture({ status: { available: true, consent: null, pending: 1 } });
  await view.consent.start();
  await wait();
  assert.equal(view.open(), true);
  assert.equal(view.dialog.attributes['aria-hidden'], 'false');
  assert.equal(view.elements.get('crashReportPending').hidden, false);
  view.elements.get('crashReportAllow').click();
  await wait();
  assert.deepEqual(view.posts, [['/api/prefs', { crashReports: true }]]);
  assert.deepEqual(view.choices, [true]);
  assert.equal(view.open(), false);
  assert.equal(view.consent.status().consent, true);
  await view.consent.start();
  await wait();
  assert.equal(view.open(), false, 'a later library reload does not ask again');
});

test('declining is saved as a choice and the recent-crash note is hidden without one', async () => {
  const view = fixture({ status: { available: true, consent: null, pending: 0 } });
  await view.consent.start();
  await wait();
  assert.equal(view.elements.get('crashReportPending').hidden, true);
  view.elements.get('crashReportDecline').click();
  await wait();
  assert.deepEqual(view.posts, [['/api/prefs', { crashReports: false }]]);
  assert.deepEqual(view.choices, [false]);
});

test('waits behind first-run setup or another dialog', async () => {
  let busy = true;
  const view = fixture({ status: { available: true, consent: null }, busy: () => busy });
  await view.consent.start();
  await wait(30);
  assert.equal(view.open(), false);
  busy = false;
  await wait(30);
  assert.equal(view.open(), true);
});

test('never asks when the build cannot send or a choice exists', async () => {
  for (const status of [{ available: false, consent: null }, { available: true, consent: false }, null]) {
    const view = fixture({ status });
    await view.consent.start();
    await wait();
    assert.equal(view.open(), false);
  }
});

test('Escape postpones the question and editor shortcuts stay blocked while open', async () => {
  const view = fixture({ status: { available: true, consent: null } });
  assert.equal(view.key('l').stopped, false, 'closed dialog leaves shortcuts alone');
  await view.consent.start();
  await wait();
  assert.equal(view.key('l').stopped, true);
  assert.equal(view.key('Tab').stopped, false, 'the shared focus trap handles Tab');
  const escape = view.key('Escape');
  assert.equal(escape.prevented, true);
  assert.equal(view.open(), false);
  assert.deepEqual(view.posts, []);
});

test('a failed save keeps the question open with a message', async () => {
  const view = fixture({ status: { available: true, consent: null },
    postResult: { error: 'Preferences are read-only.' } });
  await view.consent.start();
  await wait();
  view.elements.get('crashReportAllow').click();
  await wait();
  assert.equal(view.open(), true);
  assert.equal(view.elements.get('crashReportError').textContent, 'Preferences are read-only.');
  assert.equal(view.elements.get('crashReportAllow').disabled, false);
  assert.deepEqual(view.choices, []);
});

test('the markup explains what a report contains and offers both choices', () => {
  const markup = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  for (const id of [...IDS, 'crashReportsSetting', 'crashReports']) {
    assert.match(markup, new RegExp(`id="${id}"`));
  }
  const dialog = markup.slice(markup.indexOf('id="crashReportDialog"'), markup.indexOf('id="importDialog"'));
  assert.match(dialog, /photo metadata/);
  assert.match(dialog, /other personal information/);
  assert.match(dialog, /Settings ▸ General/);
});
