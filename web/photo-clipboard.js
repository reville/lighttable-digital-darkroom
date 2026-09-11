// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';

export function installPhotoCopyContextMenu(preview, { available, openMenu }) {
  const canOpen = event => available() &&
    !event.target.closest('button, input, select, textarea, a, [contenteditable]');
  // Control-click is a secondary click on macOS, even if WebKit reports
  // button 0. Keep it out of the photo tool pointer handlers.
  preview.addEventListener('pointerdown', event => {
    if ((event.button === 2 || (event.button === 0 && event.ctrlKey)) && canOpen(event)) {
      event.stopImmediatePropagation();
    }
  }, true);
  preview.addEventListener('contextmenu', event => {
    if (!canOpen(event)) return;
    event.preventDefault();
    event.stopPropagation();
    // The menu acts on the photo even if a text field previously had focus.
    preview.ownerDocument.activeElement?.blur?.();
    openMenu({ x: event.clientX, y: event.clientY });
  });
}

// Start Clipboard.write during the key/menu gesture; WebKit requires user
// activation even when the PNG itself will only be ready asynchronously.
export function createPhotoClipboard({ getPayload, nativeBridge, notify }) {
  let pending = null;
  let sequence = 0;

  async function render(payload) {
    const response = await fetch('/api/render/file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, w: 2048, format: 'png', client: 'photo-clipboard' }),
      signal: AbortSignal.timeout(120000),
    });
    if (!response.ok || !response.headers.get('content-type')?.startsWith('image/png')) {
      throw new Error('Photo render failed');
    }
    return response.blob();
  }

  function complete(event) {
    if (!pending || pending.id !== event.id) return;
    event.ok ? pending.resolve() : pending.reject(new Error('Native copy failed'));
  }

  async function copy() {
    if (pending) return;
    const payload = getPayload();
    if (!payload) return;
    const id = `photo-copy-${++sequence}`;
    pending = { id };
    let timer;
    try {
      const bridge = nativeBridge();
      if (bridge) {
        const blob = await render(payload);
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 8192) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
        }
        await new Promise((resolve, reject) => {
          pending = { id, resolve, reject };
          timer = setTimeout(() => reject(new Error('Native copy timed out')), 10000);
          bridge.postMessage({ action: 'copyPhotoImage', id, png: btoa(binary) });
        });
      } else {
        if (!globalThis.ClipboardItem || !navigator.clipboard?.write) {
          throw new Error('Image clipboard unavailable');
        }
        const png = render(payload);
        // A permission rejection can precede a render failure.
        void png.catch(() => {});
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
      }
      notify(tr('Photo copied to clipboard'));
    } catch {
      notify(tr('Could not copy photo to clipboard'));
    } finally {
      clearTimeout(timer);
      pending = null;
    }
  }

  return { copy, complete };
}
