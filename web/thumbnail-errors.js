// SPDX-License-Identifier: GPL-3.0-only
import { t } from './i18n.js';

// Only failed <img> requests need a JSON read. Share that read between the
// grid and filmstrip, and bound it so a folder of bad originals stays usable.
const pending = new Map();
const queue = [];
let active = 0;

function pump() {
  while (active < 2 && queue.length) {
    const { source, resolve } = queue.shift();
    active++;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    fetch(source, { cache: 'no-store', signal: controller.signal })
      .then(async response => {
        if (response.ok) return { recovered: true };
        const body = await response.json().catch(() => ({}));
        return {
          message: typeof body.error === 'string' ? body.error :
            t('Thumbnail request failed (HTTP {status}). Try again.', { status: response.status }),
          diagnostic: typeof body.details?.diagnostic === 'string' ? body.details.diagnostic : '',
        };
      })
      .catch(() => ({ message: t('Could not reach LightTable. Check that the app is running, then retry.') }))
      .then(resolve)
      .finally(() => {
        clearTimeout(timer);
        pending.delete(source);
        active--;
        pump();
      });
  }
}

function diagnose(source) {
  if (!pending.has(source)) {
    pending.set(source, new Promise(resolve => queue.push({ source, resolve })));
    pump();
  }
  return pending.get(source);
}

export function bindThumbnailErrors(element, image, onState = () => {}) {
  let source = '', generation = 0, checked = false, panel;
  function clear() {
    generation++;
    image.style.visibility = '';
    delete image.dataset.thumbnailError;
    panel?.remove();
    panel = null;
  }
  function show(message, diagnostic = '') {
    image.style.visibility = 'hidden'; // keep thumbnail geometry and observation
    image.dataset.thumbnailError = '1';
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'thumbnail-error';
      panel.setAttribute('role', 'group');
      const title = document.createElement('strong');
      title.textContent = t('Thumbnail unavailable');
      const reason = document.createElement('div');
      reason.className = 'thumbnail-error-reason';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = t('Retry');
      retry.onclick = event => {
        event.stopPropagation();
        checked = false;
        clear();
        image.dataset.thumbnailKind = 'source';
        image.src = source + (source.includes('?') ? '&' : '?') + 'retry=' + Date.now();
      };
      // Reading the error must not open the photo or run photo shortcuts.
      for (const event of ['dblclick', 'keydown']) {
        panel.addEventListener(event, e => e.stopPropagation());
      }
      panel.append(title, reason, retry);
      element.append(panel);
    }
    const text = [message, diagnostic].filter(Boolean).join('\n\n');
    panel.querySelector('.thumbnail-error-reason').textContent = text;
    panel.title = text;
    panel.setAttribute('aria-label', `${t('Thumbnail unavailable')}: ${text}`);
    onState(true);
  }
  image.addEventListener('load', () => {
    checked = false; clear();
    if (image.dataset.thumbnailKind === 'source') onState(false);
  });
  image.addEventListener('error', async () => {
    // A failed edit-aware rendition should return to the usable source first.
    if (image.dataset.thumbnailKind === 'edited') {
      image.dataset.thumbnailKind = 'source';
      image.src = source;
      return;
    }
    if (checked && panel) return;
    const request = ++generation;
    show(t('The thumbnail could not be displayed. Try again.'));
    if (checked || !source.startsWith('/api/thumb?')) return;
    checked = true;
    const result = await diagnose(source);
    if (request !== generation) return;
    if (result.recovered) {
      image.src = source + '&retry=' + Date.now();
    } else {
      show(result.message, result.diagnostic);
    }
  });
  return nextSource => {
    if (source === nextSource) return;
    source = nextSource;
    checked = false;
    clear();
  };
}
