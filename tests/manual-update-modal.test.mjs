// SPDX-License-Identifier: GPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  getPlatformUpdateInstructions,
  showManualUpdateModal,
  closeManualUpdateModal
} from '../web/manual-update-modal.js';

test('getPlatformUpdateInstructions resolves macOS beta and Homebrew commands', () => {
  const homebrew = getPlatformUpdateInstructions({
    platform: 'macos',
    owner: 'homebrew',
    version: '0.6.0'
  });
  assert.match(homebrew.command, /brew upgrade --cask/);
  assert.equal(homebrew.websiteUrl, 'https://lighttable.app/');
  assert.match(homebrew.releaseNotesUrl, /releases\/tag\/v0\.6\.0/);

  const personal = getPlatformUpdateInstructions({
    platform: 'macos',
    personal: true,
    version: '0.6.0'
  });
  assert.match(personal.command, /scripts\/update-personal-app\.sh/);
});

test('getPlatformUpdateInstructions resolves Linux package manager commands', () => {
  const arch = getPlatformUpdateInstructions({
    platform: 'linux',
    owner: 'arch',
    version: '0.6.0'
  });
  assert.match(arch.command, /yay -Syu lighttable-bin/);
  assert.equal(arch.websiteUrl, 'https://lighttable.app/linux.html');

  const flatpak = getPlatformUpdateInstructions({
    platform: 'linux',
    owner: 'flatpak'
  });
  assert.match(flatpak.command, /flatpak update/);

  const snap = getPlatformUpdateInstructions({
    platform: 'linux',
    owner: 'snap'
  });
  assert.match(snap.command, /snap refresh/);
});

test('getPlatformUpdateInstructions resolves Windows package manager commands', () => {
  const scoop = getPlatformUpdateInstructions({
    platform: 'windows',
    owner: 'scoop',
    version: '0.6.0'
  });
  assert.match(scoop.command, /scoop update lighttable/);
  assert.equal(scoop.websiteUrl, 'https://lighttable.app/windows.html');

  const winget = getPlatformUpdateInstructions({
    platform: 'windows',
    owner: 'winget',
    version: '0.6.0'
  });
  assert.match(winget.command, /winget upgrade/);
});

test('getPlatformUpdateInstructions honors explicit command and url overrides', () => {
  const custom = getPlatformUpdateInstructions({
    command: 'my-custom-updater command',
    url: 'https://custom.example.com/download',
    version: '1.0.0'
  });
  assert.equal(custom.command, 'my-custom-updater command');
  assert.equal(custom.websiteUrl, 'https://custom.example.com/download');
});

test('showManualUpdateModal builds and manages modal in DOM', async () => {
  const elements = new Map();
  function makeElement(tag) {
    const el = {
      tagName: tag.toUpperCase(),
      id: '',
      className: '',
      classList: {
        _classes: new Set(),
        add(c) { this._classes.add(c); el.className = [...this._classes].join(' '); },
        remove(c) { this._classes.delete(c); el.className = [...this._classes].join(' '); },
        contains(c) { return this._classes.has(c); }
      },
      attributes: new Map(),
      setAttribute(k, v) { this.attributes.set(k, String(v)); },
      getAttribute(k) { return this.attributes.get(k); },
      children: [],
      appendChild(child) { this.children.push(child); return child; },
      handlers: {},
      addEventListener(evt, fn) {
        if (!this.handlers[evt]) this.handlers[evt] = [];
        this.handlers[evt].push(fn);
      },
      querySelector(sel) {
        const id = sel.startsWith('#') ? sel.slice(1) : null;
        if (id) {
          return elements.get(id) || null;
        }
        return null;
      },
      focus() { el.focused = true; },
      focused: false,
      isConnected: true,
      _innerHTML: '',
      set innerHTML(html) {
        this._innerHTML = html;
        const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);
        for (const id of ids) {
          const child = makeElement('div');
          child.id = id;
          elements.set(id, child);
        }
      },
      get innerHTML() { return this._innerHTML; }
    };
    return el;
  }

  const body = makeElement('body');
  globalThis.document = {
    activeElement: null,
    getElementById(id) { return elements.get(id) || null; },
    createElement(tag) {
      const el = makeElement(tag);
      return el;
    },
    body
  };

  let copiedText = '';
  Object.defineProperty(globalThis.navigator, 'clipboard', {
    value: {
      writeText: async (t) => { copiedText = t; }
    },
    configurable: true
  });

  const dialog = showManualUpdateModal({
    platform: 'macos',
    owner: 'homebrew',
    version: '0.6.0'
  });

  assert.ok(dialog);
  assert.equal(dialog.id, 'manualUpdateDialog');
  assert.equal(dialog.classList.contains('on'), true);
  assert.equal(dialog.getAttribute('aria-hidden'), 'false');
  assert.ok(dialog.innerHTML.includes('brew update &amp;&amp; brew upgrade'));
  assert.ok(dialog.innerHTML.includes('LightTable 0.6.0 Available'));

  // Test copy action
  const copyBtn = elements.get('manualUpdateCopyBtn');
  assert.ok(copyBtn);
  await copyBtn.handlers.click?.[0]();
  assert.equal(copiedText, 'brew update && brew upgrade --cask reville/lighttable/lighttable@beta');

  // Test close action
  const closeBtn = elements.get('manualUpdateCloseX');
  assert.ok(closeBtn);
  closeBtn.handlers.click?.[0]();
  assert.equal(dialog.classList.contains('on'), false);
  assert.equal(dialog.getAttribute('aria-hidden'), 'true');
});
