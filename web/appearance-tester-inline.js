// Preserve the existing tester in desktop shells without managed utility windows.
import {isAppearanceColor, normalizeAppearance} from './appearance-tester.js';
const STORAGE_KEY = 'lighttable.appearance-test.v1';

export function installAppearanceTester() {
  if (document.getElementById('appearanceTestDialog')) return;
  const root = document.documentElement;
  let settings = normalizeAppearance(null);
  try { settings = normalizeAppearance(JSON.parse(localStorage.getItem(STORAGE_KEY))); } catch (_) {}
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/web/appearance-tester.css';
  document.head.append(stylesheet);

  // Debug-only English labels; this panel is not part of the translated product UI.
  const backdrop = document.createElement('div');
  backdrop.id = 'appearanceTestDialog';
  backdrop.className = 'modal-backdrop appearance-test-backdrop';
  backdrop.setAttribute('aria-hidden', 'true');
  backdrop.setAttribute('data-no-i18n', '');
  backdrop.innerHTML = `<div class="modal appearance-test" role="dialog" aria-modal="true" aria-labelledby="appearanceTestTitle">
    <strong id="appearanceTestTitle">Appearance tester</strong>
    <label for="appearanceTestColor">Highlight color</label>
    <div class="appearance-test-color">
      <input id="appearanceTestColor" type="color" aria-label="Highlight color">
      <input id="appearanceTestHex" type="text" aria-label="Hex color" maxlength="7" spellcheck="false" autocomplete="off" pattern="#[0-9a-fA-F]{6}">
    </div>
    <label class="check-row"><input id="appearanceTestBorders" type="checkbox" role="switch">Show active button borders</label>
    <p>Changes apply immediately. With borders off, active buttons use text and icon color only.</p>
    <div class="modal-actions"><button id="appearanceTestReset">Reset</button><button id="appearanceTestClose">Done</button></div>
  </div>`;
  document.body.append(backdrop);
  const picker = backdrop.querySelector('#appearanceTestColor');
  const hex = backdrop.querySelector('#appearanceTestHex');
  const borders = backdrop.querySelector('#appearanceTestBorders');
  const apply = () => {
    root.toggleAttribute('data-appearance-color', Boolean(settings.color));
    if (settings.color) {
      root.style.setProperty('--appearance-accent', settings.color);
      const rgb = [1, 3, 5].map(start => parseInt(settings.color.slice(start, start + 2), 16));
      root.style.setProperty('--appearance-soft', `rgba(${rgb.join(',')},0.16)`);
    } else {
      root.style.removeProperty('--appearance-accent');
      root.style.removeProperty('--appearance-soft');
    }
    root.toggleAttribute('data-appearance-no-borders', !settings.borders);
  };
  const sync = () => {
    picker.value = settings.color;
    hex.value = picker.value;
    hex.removeAttribute('aria-invalid');
    borders.checked = settings.borders;
  };
  const save = () => {
    apply();
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (_) {}
  };
  const close = () => {
    backdrop.classList.remove('on');
    backdrop.setAttribute('aria-hidden', 'true');
  };
  picker.addEventListener('input', () => {
    settings.color = picker.value;
    hex.value = picker.value;
    hex.removeAttribute('aria-invalid');
    save();
  });
  hex.addEventListener('input', () => {
    const value = hex.value.trim();
    const valid = isAppearanceColor(value);
    hex.setAttribute('aria-invalid', String(!valid));
    if (!valid) return;
    settings.color = value.toLowerCase();
    picker.value = settings.color;
    save();
  });
  hex.addEventListener('blur', sync);
  borders.addEventListener('change', () => { settings.borders = borders.checked; save(); });
  backdrop.querySelector('#appearanceTestReset').addEventListener('click', () => {
    settings = normalizeAppearance(null);
    apply(); sync();
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
  });
  backdrop.querySelector('#appearanceTestClose').addEventListener('click', close);
  backdrop.addEventListener('click', event => { if (event.target === backdrop) close(); });
  document.addEventListener('keydown', event => {
    const shortcut = (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'd';
    const isOpen = backdrop.classList.contains('on');
    if (!shortcut && !(isOpen && event.key === 'Escape')) return;
    if (!isOpen && document.querySelector('.modal-backdrop.on')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.repeat) return;
    if (isOpen) close();
    else {
      sync();
      backdrop.setAttribute('aria-hidden', 'false');
      backdrop.classList.add('on');
    }
  }, true);
  apply(); sync();
}
