// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';
/* Colour labels and the keyboard schemes that drive culling.
 *
 * Labels are the third axis of triage alongside stars and pick/reject: a
 * photographer marks "send to client", "needs retouch", "already delivered"
 * without spending a star rating on it. They are stored on the image state and
 * filtered in SQL, so the grid never has to hold the whole library to answer.
 */

export const LABELS = ['none', 'red', 'yellow', 'green', 'blue', 'purple'];

export const LABEL_COLORS = {
  none: 'transparent',
  red: '#d9534f',
  yellow: '#d8b019',
  green: '#4f9d5d',
  blue: '#3f7fd0',
  purple: '#8a63c8',
};

export const LABEL_TITLES = {
  none: tr("No label"),
  red: tr("Red"),
  yellow: tr("Yellow"),
  green: tr("Green"),
  blue: tr("Blue"),
  purple: tr("Purple"),
};

export const cleanLabel = (value) => (
  LABELS.includes(String(value || 'none')) ? String(value || 'none') : 'none');

/* Two schemes. The default is this app's own; "classic" matches the muscle
 * memory of the editor most people are arriving from, which is worth more
 * during a cull than internal consistency. */
export const KEY_SCHEMES = {
  lighttable: {
    name: 'LightTable',
    grid: 'g', squareGrid: 'G', detail: 'd', survey: 'n', compare: '\\',
    crop: 'c', mask: 'm', heal: 'q', before: 'b', fit: 'f', search: '/',
    pick: ['p', 'a'], reject: ['x'], unflag: ['u'],
    speed: { e: 'exposure', j: 'contrast', h: 'highlights', s: 'shadows',
      w: 'whites', k: 'blacks', t: 'temp', i: 'tint', v: 'vibrance',
      l: 'saturation', y: 'clarity', z: 'dehaze' },
  },
  classic: {
    name: tr('Classic'),
    grid: 'g', squareGrid: 'G', detail: 'e', survey: 'n', compare: 'c',
    crop: 'r', mask: 'm', heal: 'q', before: 'b', fit: 'f', search: '/',
    pick: ['p'], reject: ['x'], unflag: ['u'],
    speed: { e: 'exposure', j: 'contrast', h: 'highlights', s: 'shadows',
      w: 'whites', k: 'blacks', t: 'temp', i: 'tint', v: 'vibrance',
      l: 'saturation', y: 'clarity', z: 'dehaze' },
  },
};

export const LABEL_KEYS = { 6: 'red', 7: 'yellow', 8: 'green', 9: 'blue' };

export function labelSwatch(label) {
  const clean = cleanLabel(label);
  if (clean === 'none') return '';
  return `<span class="label-dot" data-label="${clean}"`
    + ` style="background:${LABEL_COLORS[clean]}"`
    + ` title="${String(LABEL_TITLES[clean]).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}"></span>`;
}

/* Build the label control shown in the Info pane. */
export function renderLabelRow(container, current, onPick) {
  if (!container) return;
  container.innerHTML = '';
  LABELS.forEach((label) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'label-btn' + (cleanLabel(current) === label ? ' on' : '');
    button.dataset.label = label;
    button.title = LABEL_TITLES[label];
    button.setAttribute('aria-label', LABEL_TITLES[label]);
    if (label === 'none') {
      button.textContent = '×';
    } else {
      button.style.background = LABEL_COLORS[label];
    }
    button.addEventListener('click', () => onPick(label));
    container.appendChild(button);
  });
}
