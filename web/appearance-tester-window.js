// SPDX-License-Identifier: GPL-3.0-only
import {applyAppearance, createAppearanceSettings, isAppearanceColor} from './appearance-tester.js';

const picker = document.getElementById('appearanceTestColor');
const hex = document.getElementById('appearanceTestHex');
const borders = document.getElementById('appearanceTestBorders');
const sync = value => {
  applyAppearance(document.documentElement, value);
  picker.value = value.color;
  hex.value = picker.value;
  hex.removeAttribute('aria-invalid');
  borders.checked = value.borders;
};
const settings = createAppearanceSettings({onChange: sync});
picker.addEventListener('input', () => settings.set({...settings.get(), color: picker.value}));
hex.addEventListener('input', () => {
  const color = hex.value.trim();
  const valid = isAppearanceColor(color);
  hex.setAttribute('aria-invalid', String(!valid));
  if (valid) settings.set({...settings.get(), color});
});
hex.addEventListener('blur', () => sync(settings.get()));
borders.addEventListener('change', () => settings.set({...settings.get(), borders: borders.checked}));
document.getElementById('appearanceTestReset').addEventListener('click', () => settings.reset());
const close = () => {
  const native = window.webkit?.messageHandlers?.appearanceTester;
  if (native) native.postMessage('close');
  else window.close();
};
document.getElementById('appearanceTestClose').addEventListener('click', close);
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'd') event.preventDefault();
  if (event.key === 'Escape') { event.preventDefault(); close(); }
});
window.addEventListener('pagehide', () => settings.close(), {once: true});
