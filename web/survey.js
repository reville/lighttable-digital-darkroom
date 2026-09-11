// SPDX-License-Identifier: GPL-3.0-only

const i18nHTML = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll("\"", "&quot;").replaceAll("'", "&#39;");
import { t as tr } from './i18n.js';
/* Survey and A/B compare — choosing between frames, not judging one.
 *
 * The existing compare is before/after on a single photo. Culling a burst is a
 * different question: which of these five is the keeper? Survey lays the
 * current selection out together at fit size and lets rating, flag, and label
 * keys land on whichever cell is active. A/B narrows the same model to two.
 *
 * This view deliberately owns its own element tree. The editor's canvas and
 * `GradeRenderer` are single-instance and hold pointer capture for crop, mask,
 * and compare gestures; borrowing them here would mean fighting for that
 * ownership. Survey cells are plain images from the preview endpoints instead,
 * requested at cell width and at low priority so a survey never delays the
 * photo being edited.
 */

import { labelSwatch } from '/web/labels.js';

/* Up and down cross rows rather than scrolling the grid. A short final row
 * keeps the column, so the last cell in that column takes the press, and the
 * ends wrap the way the left and right keys already do. */
export function surveyRowNeighbour(index, count, columns, direction) {
  if (count < 1 || columns < 1) return -1;
  const next = index + direction * columns;
  if (next >= 0 && next < count) return next;
  const column = index % columns;
  if (direction > 0) return column < count ? column : column - columns;
  const last = Math.floor((count - 1) / columns) * columns + column;
  return last < count ? last : last - columns;
}

export function createSurvey(ctx) {
  const { el, onActivate, onOpen } = ctx;
  let names = [];
  let activeName = null;
  let mode = 'survey';

  function columnCount(count) {
    if (mode === 'compare') return 2;
    return Math.min(4, Math.max(1, Math.ceil(Math.sqrt(count))));
  }

  const container = el('survey');
  const grid = el('surveyGrid');
  const title = el('surveyTitle');
  const swapButton = el('surveySwap');

  function imageFor(name) {
    return (ctx.images() || []).find((image) => image.name === name) || null;
  }

  function imageURL(image) {
    return `/api/orig?name=${encodeURIComponent(image.name)}&w=800&v=2&key=${encodeURIComponent(image.fileKey || image.mtime || '')}`;
  }

  function syncSurveyCell(cell, image, isActive) {
    const stars = '★'.repeat(image.rating || 0)
      + '☆'.repeat(Math.max(0, 5 - (image.rating || 0)));
    const flag = image.status === 'approved' ? '⚑'
      : image.status === 'skipped' ? '⚐' : '';
    const encodedName = encodeURIComponent(image.name);
    cell.className = `survey-cell${isActive ? ' active' : ''}`;
    cell.dataset.name = encodedName;
    const preview = cell.querySelector('img');
    const nextURL = imageURL(image);
    if (preview.getAttribute('src') !== nextURL) preview.setAttribute('src', nextURL);
    const remove = cell.querySelector('.survey-remove');
    remove.dataset.remove = encodedName;
    const name = cell.querySelector('.survey-name');
    const nextName = image.displayName || image.name;
    if (name.textContent !== nextName) name.textContent = nextName;
    const marks = cell.querySelector('.survey-marks');
    const nextMarks = `${labelSwatch(image.label)}<span class="survey-stars">${stars}</span><span class="survey-flag">${flag}</span>`;
    if (marks.innerHTML !== nextMarks) marks.innerHTML = nextMarks;
  }

  function createSurveyCell(image, isActive) {
    const cell = document.createElement('figure');
    cell.tabIndex = 0;
    cell.innerHTML = `
        <div class="survey-frame">
          <img loading="lazy" decoding="async" alt="">
          <button class="survey-remove" title="${i18nHTML(tr("Remove from survey"))}">×</button>
        </div>
        <figcaption>
          <span class="survey-name"></span>
          <span class="survey-marks"></span>
        </figcaption>`;
    syncSurveyCell(cell, image, isActive);
    return cell;
  }

  function reconcileSurveyCells(ordered) {
    let cursor = grid.firstElementChild;
    for (const cell of ordered) {
      if (cell === cursor) cursor = cursor.nextElementSibling;
      else grid.insertBefore(cell, cursor);
    }
    while (cursor) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
  }

  function render() {
    if (!container || !grid) return;
    const rows = names.map(imageFor).filter(Boolean);
    if (swapButton) {
      swapButton.hidden = mode !== 'compare';
      swapButton.disabled = mode !== 'compare' || rows.length < 2;
    }
    if (title) {
      title.textContent = mode === 'compare' ? tr("Compare {rowsLength} photos", {rowsLength: rows.length}) : tr("Survey {rowsLength} photos", {rowsLength: rows.length});
    }
    grid.className = `survey-grid ${mode === 'compare' ? 'compare-two' : ''}`;
    grid.style.setProperty('--survey-columns', String(columnCount(rows.length)));
    const existing = new Map([...grid.children].map((cell) =>
      [decodeURIComponent(cell.dataset.name || ''), cell]));
    const ordered = rows.map((image) => {
      const cell = existing.get(image.name)
        || createSurveyCell(image, image.name === activeName);
      syncSurveyCell(cell, image, image.name === activeName);
      return cell;
    });
    reconcileSurveyCells(ordered);
  }

  function setActive(name) {
    activeName = name;
    render();
    const cell = grid?.querySelector('.survey-cell.active');
    cell?.scrollIntoView({ block: 'nearest' });
    if (onActivate) onActivate(name);
  }

  function step(direction) {
    if (!names.length) return;
    const index = Math.max(0, names.indexOf(activeName));
    setActive(names[(index + direction + names.length) % names.length]);
  }

  function stepRow(direction) {
    if (!names.length) return;
    const index = Math.max(0, names.indexOf(activeName));
    const next = surveyRowNeighbour(index, names.length,
      columnCount(names.length), direction);
    if (next >= 0) setActive(names[next]);
  }

  function open(list, nextMode = 'survey') {
    names = Array.from(new Set(list || [])).slice(0, 16);
    if (nextMode === 'compare') names = names.slice(0, 2);
    mode = nextMode;
    if (!names.length) return false;
    activeName = names.includes(activeName) ? activeName : names[0];
    container.hidden = false;
    document.body.classList.add('survey-open');
    render();
    if (onActivate) onActivate(activeName);
    return true;
  }

  function close() {
    container.hidden = true;
    document.body.classList.remove('survey-open');
    if (ctx.onClose) ctx.onClose();
  }

  function remove(name) {
    names = names.filter((entry) => entry !== name);
    if (!names.length) { close(); return; }
    if (activeName === name) {
      // The host tracks its own current photo; switching the active cell must
      // go through the same notification path as navigation.
      setActive(names[0]);
      return;
    }
    render();
  }

  function swap() {
    if (mode !== 'compare' || names.map(imageFor).filter(Boolean).length < 2) return;
    names = [names[1], names[0]];
    render();
  }

  if (grid) {
    grid.addEventListener('click', (event) => {
      const removeButton = event.target.closest('[data-remove]');
      if (removeButton) {
        remove(decodeURIComponent(removeButton.dataset.remove));
        return;
      }
      const cell = event.target.closest('.survey-cell');
      if (cell) setActive(decodeURIComponent(cell.dataset.name));
    });
    grid.addEventListener('dblclick', (event) => {
      const cell = event.target.closest('.survey-cell');
      if (cell && onOpen) {
        close();
        onOpen(decodeURIComponent(cell.dataset.name));
      }
    });
  }

  return {
    open,
    close,
    render,
    swap,
    step,
    stepRow,
    remove,
    get isOpen() { return container && !container.hidden; },
    get active() { return activeName; },
    get names() { return names.slice(); },
    get mode() { return mode; },
  };
}
