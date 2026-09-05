/* Slider interaction upgrades for the edit panel.
 *
 * These apply to every range input inside #panel whatever skin the panel wears:
 *   - a detent at zero while dragging a bipolar slider, held off with Option
 *   - a zero tick drawn on the track (CSS, positioned from --zero-frac here)
 *   - values you can drag sideways to scrub and double-click to type
 *   - a per-row reset that appears on hover once a slider is off its default
 *   - a changed indicator: the number brightens when it differs from default
 *   - a leading + on positive values of bipolar sliders
 *   - Shift with an arrow key for a ten-times step
 *   - a short flash on reset
 *
 * Every write goes through the app's own listeners by dispatching real input
 * and change events on the native range input, so undo, autosave, MIDI learn
 * and the render scheduler keep working untouched. Nothing here writes app
 * state directly.
 */

const SNAP_FRACTION = 0.015;      /* detent band, as a share of the range */
const SCRUB_TRAVEL = 190;         /* pixels of drag for one full range at gain 1 */
const SCRUB_GAIN = 0.6;
const FLASH_MS = 200;

const meta = new WeakMap();
let activeInput = null;           /* range input under an active pointer drag */
let altHeld = false;
let dirty = new Set();
let flushQueued = false;

const isRange = (el) => el instanceof HTMLInputElement && el.type === 'range';

function decimalsOf(step) {
  const text = String(step);
  const dot = text.indexOf('.');
  return dot < 0 ? 0 : text.length - dot - 1;
}

function quantize(input, value) {
  const min = +input.min;
  const max = +input.max;
  const step = +input.step || 0;
  let next = Math.min(max, Math.max(min, value));
  if (step > 0) next = min + Math.round((next - min) / step) * step;
  return +next.toFixed(Math.max(decimalsOf(input.step || 1), 6));
}

/* A default is only claimed when the markup states one, or when the slider is
   bipolar and zero is its neutral point. Mask and healing sliders carry the
   selected object's own values, so they get no default and no reset. */
function defaultFor(input) {
  const stated = input.dataset.default ?? input.getAttribute('value');
  if (stated !== null && stated !== undefined && stated !== '') return +stated;
  const min = +input.min;
  const max = +input.max;
  if (min < 0 && max > 0) return 0;
  return null;
}

function infoFor(input) {
  let info = meta.get(input);
  if (!info) {
    const row = input.closest('.row');
    if (!row) return null;
    const min = +input.min;
    const max = +input.max;
    info = {
      row,
      value: row.querySelector('.val'),
      min,
      max,
      bipolar: min < 0 && max > 0,
      def: defaultFor(input),
    };
    meta.set(input, info);
  }
  return info;
}

/* ---------------------------------------------------------------- readouts */

function refresh(input) {
  const info = infoFor(input);
  if (!info) return;
  const v = +input.value;
  const changed = info.def !== null && Math.abs(v - info.def) > 1e-9;
  info.row.classList.toggle('changed', changed);
  sign(info, v);
}

/* Bipolar readouts get a leading +, replacing the space the app already pads
   them with so positive and negative values stay on the same left edge. This
   rewrites the readout rather than adding a pseudo-element because several
   formatters sign their own output and would otherwise double up. */
function sign(info, v) {
  const el = info.value;
  if (!el || !info.bipolar || !(v > 0)) return;
  const text = el.textContent;
  if (!text || /^[+\-−]/.test(text)) return;
  const next = text.startsWith(' ') ? `+${text.slice(1)}` : `+${text}`;
  if (next !== text) el.textContent = next;
}

function markDirty(input) {
  dirty.add(input);
  if (flushQueued) return;
  flushQueued = true;
  requestAnimationFrame(() => {
    flushQueued = false;
    const pending = dirty;
    dirty = new Set();
    pending.forEach(refresh);
  });
}

/* ------------------------------------------------------------- value writes */

function commit(input, value, { change = false } = {}) {
  const next = quantize(input, value);
  if (String(next) !== input.value) {
    input.value = String(next);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (change) input.dispatchEvent(new Event('change', { bubbles: true }));
  markDirty(input);
}

/* ------------------------------------------------------------- zero detent */

/* Runs before the app's own input listeners, so the snapped value is the one
   every downstream handler sees. */
document.addEventListener('input', (event) => {
  const input = event.target;
  if (!isRange(input) || !input.closest('#panel')) return;
  if (input === activeInput && !altHeld) {
    const info = infoFor(input);
    if (info && info.bipolar) {
      const band = (info.max - info.min) * SNAP_FRACTION;
      const v = +input.value;
      if (v !== 0 && Math.abs(v) <= band) input.value = '0';
    }
  }
  markDirty(input);
}, true);

document.addEventListener('change', (event) => {
  if (isRange(event.target)) markDirty(event.target);
}, true);

document.addEventListener('pointerdown', (event) => {
  const input = event.target;
  if (!isRange(input) || !input.closest('#panel')) return;
  activeInput = input;
  altHeld = event.altKey;
}, true);

for (const name of ['pointerup', 'pointercancel']) {
  window.addEventListener(name, () => { activeInput = null; }, true);
}
window.addEventListener('keydown', (event) => { if (event.key === 'Alt') altHeld = true; });
window.addEventListener('keyup', (event) => { if (event.key === 'Alt') altHeld = false; });
window.addEventListener('blur', () => { altHeld = false; activeInput = null; });

/* -------------------------------------------------- Shift arrow: ten steps */

document.addEventListener('keydown', (event) => {
  const input = event.target;
  if (!isRange(input) || !input.closest('#panel')) return;
  if (!event.shiftKey || event.metaKey || event.ctrlKey) return;
  const dir = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[event.key];
  if (!dir) return;
  event.preventDefault();
  const step = (+input.step || 1) * 10;
  commit(input, +input.value + dir * step, { change: true });
});

/* ------------------------------------------------------ reset, with a flash */

let resetFrom = null;

document.addEventListener('dblclick', (event) => {
  const input = rangeForReset(event.target);
  if (input) resetFrom = { input, value: +input.value };
}, true);

document.addEventListener('dblclick', (event) => {
  const input = rangeForReset(event.target);
  if (!input || !resetFrom || resetFrom.input !== input) return;
  const before = resetFrom.value;
  resetFrom = null;
  markDirty(input);
  if (Math.abs(+input.value - before) > 1e-9) flash(input);
});

function rangeForReset(target) {
  if (!(target instanceof Element)) return null;
  const row = target.closest('#panel .row');
  if (!row) return null;
  if (!target.closest('input[type=range], .name')) return null;
  return row.querySelector('input[type=range]');
}

function flash(input) {
  const info = infoFor(input);
  if (!info) return;
  info.row.classList.add('flash');
  setTimeout(() => info.row.classList.remove('flash'), FLASH_MS);
}

/* ------------------------------------------- drag to scrub, type to set it */

let scrub = null;

document.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  const value = event.target instanceof Element ? event.target.closest('#panel .row .val') : null;
  if (!value) return;
  const row = value.closest('.row');
  const input = row.querySelector('input[type=range]');
  if (!input || input.disabled) return;
  if (row.querySelector('.val-edit')) return;
  event.preventDefault();
  value.setPointerCapture(event.pointerId);
  scrub = { input, value, row, x: event.clientX, from: +input.value, moved: false };
  row.classList.add('scrubbing');
  /* The app hangs its undo checkpoint off pointerdown on the slider itself. */
  input.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: -1 }));
});

document.addEventListener('pointermove', (event) => {
  if (!scrub) return;
  const dx = event.clientX - scrub.x;
  if (!scrub.moved && Math.abs(dx) < 2) return;
  scrub.moved = true;
  const info = infoFor(scrub.input);
  const span = info.max - info.min;
  const gain = event.altKey ? 0.15 : event.shiftKey ? 2.4 : SCRUB_GAIN;
  commit(scrub.input, scrub.from + (dx / SCRUB_TRAVEL) * span * gain);
});

for (const name of ['pointerup', 'pointercancel']) {
  document.addEventListener(name, () => {
    if (!scrub) return;
    const { input, row, moved } = scrub;
    scrub = null;
    row.classList.remove('scrubbing');
    if (moved) input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

document.addEventListener('dblclick', (event) => {
  const value = event.target instanceof Element ? event.target.closest('#panel .row .val') : null;
  if (value) openEditor(value);
});

function openEditor(valueEl) {
  const row = valueEl.closest('.row');
  const input = row.querySelector('input[type=range]');
  if (!input || input.disabled || row.querySelector('.val-edit')) return;
  const field = document.createElement('input');
  field.type = 'text';
  field.className = 'val-edit';
  field.value = String(+input.value);
  field.spellcheck = false;
  row.appendChild(field);
  field.focus();
  field.select();

  let done = false;
  const close = (apply) => {
    if (done) return;
    done = true;
    const typed = parseFloat(field.value.replace(/[^\d.+-]/g, ''));
    field.remove();
    if (apply && Number.isFinite(typed)) commit(input, typed, { change: true });
  };
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); close(true); }
    if (event.key === 'Escape') { event.preventDefault(); close(false); }
    event.stopPropagation();
  });
  field.addEventListener('blur', () => close(true));
}

/* ------------------------------------------------------------------- setup */

function prepare(input) {
  const info = infoFor(input);
  if (!info) return;
  if (info.bipolar) {
    info.row.classList.add('bipolar');
    info.row.style.setProperty('--zero-frac', String((0 - info.min) / (info.max - info.min)));
  }
  if (info.def === null) info.row.classList.add('no-default');
  /* The label column is narrow, so long names ellipsize. Measure on hover,
     when the row is certainly laid out, and only then offer a tooltip. */
  if (!info.hoverBound) {
    info.hoverBound = true;
    info.row.addEventListener('pointerenter', () => {
      const name = info.row.querySelector('.name');
      if (!name) return;
      const clipped = name.scrollWidth > name.clientWidth + 1;
      if (clipped && !name.title) name.title = name.textContent.trim();
      else if (!clipped && name.title) name.removeAttribute('title');
    }, { passive: true });
  }
  if (info.def !== null && !info.row.querySelector('.row-reset')) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'row-reset';
    button.tabIndex = -1;
    button.title = 'Reset this slider';
    button.setAttribute('aria-label', `Reset ${info.row.querySelector('.name')?.textContent?.trim() || 'slider'}`);
    button.textContent = '↺';
    button.addEventListener('click', (event) => {
      event.preventDefault();
      /* Every slider group already binds its own reset to dblclick, so this
         reuses that logic instead of duplicating each group's defaults. */
      input.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    });
    info.row.appendChild(button);
  }
  refresh(input);
}

function scan() {
  document.querySelectorAll('#panel .row input[type=range]').forEach(prepare);
}

/* The app rewrites readouts on photo switches and pane syncs without firing
   input events, so watch the text instead of guessing when to re-check. */
function watch() {
  const panel = document.getElementById('panel');
  if (!panel) return;
  new MutationObserver((records) => {
    for (const record of records) {
      const node = record.target instanceof Element ? record.target : record.target.parentElement;
      const row = node?.closest?.('.row');
      const input = row?.querySelector('input[type=range]');
      if (input) markDirty(input);
    }
  }).observe(panel, { subtree: true, childList: true, characterData: true });
}

function start() {
  scan();
  watch();
  /* Panes and masks fill in later; a second pass catches rows added by then. */
  setTimeout(scan, 1200);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}

export { scan as rescanSliderRows };
