// SPDX-License-Identifier: GPL-3.0-only
/* Custom dropdowns.
 *
 * Every <select> stays in the DOM as the single source of truth: the rest of
 * the app populates `.options`, reads `.value`, and listens for `change`
 * exactly as before. This module only replaces the presentation. A styled
 * trigger button shows the current option, and a dark popover lists the
 * choices in place of the platform's native popup.
 *
 * Programmatic writes (`select.value = …`, `select.selectedIndex = …`) are
 * caught by instance-level property hooks, option and attribute changes by a
 * MutationObserver, and user changes by the `change` event, so the trigger
 * never shows a stale label.
 */

const CHEVRON = '<svg class="dd-chevron" viewBox="0 0 12 12" aria-hidden="true">'
  + '<path d="m2.75 4.5 3.25 3.25L9.25 4.5"/></svg>';
const CHECK = '<svg viewBox="0 0 12 12" aria-hidden="true">'
  + '<path d="m2.25 6.25 2.5 2.5 5-5.5"/></svg>';
const TYPEAHEAD_RESET_MS = 800;

const proto = HTMLSelectElement.prototype;
const nativeValue = Object.getOwnPropertyDescriptor(proto, 'value');
const nativeIndex = Object.getOwnPropertyDescriptor(proto, 'selectedIndex');

let uid = 0;
let current = null;

function labelFor(select) {
  if (select.id) {
    const byFor = document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
    if (byFor) return byFor;
  }
  return select.closest('label');
}

function enhance(select) {
  if (select.dataset.dd || select.multiple || select.closest('.no-dd')) return;
  select.dataset.dd = '1';

  const wrap = document.createElement('span');
  wrap.className = 'dd';
  if (select.classList.contains('stack-gap')) wrap.classList.add('stack-gap');

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'dd-trigger';
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.innerHTML = `<span class="dd-value"></span>${CHEVRON}`;
  const value = trigger.firstElementChild;
  if (select.title) trigger.title = select.title;
  const label = labelFor(select);
  if (label) {
    if (!label.id) label.id = `dd-label-${++uid}`;
    trigger.setAttribute('aria-labelledby', label.id);
  } else if (select.getAttribute('aria-label')) {
    trigger.setAttribute('aria-label', select.getAttribute('aria-label'));
  }

  select.parentNode.insertBefore(wrap, select);
  wrap.append(select, trigger);
  select.classList.add('dd-native');
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');

  const sync = () => {
    const option = select.options[select.selectedIndex];
    value.textContent = option ? option.textContent : '';
    trigger.disabled = select.disabled;
    if (current && current.select === select) render(current);
  };
  Object.defineProperty(select, 'value', {
    configurable: true,
    get() { return nativeValue.get.call(this); },
    set(next) { nativeValue.set.call(this, next); sync(); },
  });
  Object.defineProperty(select, 'selectedIndex', {
    configurable: true,
    get() { return nativeIndex.get.call(this); },
    set(next) { nativeIndex.set.call(this, next); sync(); },
  });
  new MutationObserver(() => sync()).observe(select, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['disabled', 'selected', 'label', 'value'],
  });
  select.addEventListener('change', sync);
  select.addEventListener('focus', () => trigger.focus());

  trigger.addEventListener('click', () => {
    if (current && current.select === select) close();
    else open(select, trigger);
  });
  trigger.addEventListener('keydown', (event) => onTriggerKey(event, select, trigger));
  sync();
}

function render(state) {
  const { select, menu } = state;
  menu.replaceChildren();
  state.items = [];
  const addOption = (option) => {
    if (option.hidden) return;
    const el = document.createElement('div');
    el.className = 'dd-option';
    el.id = `${menu.id}-${option.index}`;
    el.setAttribute('role', 'option');
    el.setAttribute('aria-selected', option.selected ? 'true' : 'false');
    if (option.disabled) el.setAttribute('aria-disabled', 'true');
    el.innerHTML = `${CHECK}<span></span>`;
    el.lastElementChild.textContent = option.textContent;
    const index = state.items.length;
    el.addEventListener('pointerenter', () => {
      if (!option.disabled) setActive(state, index, false);
    });
    el.addEventListener('click', () => choose(state, index));
    menu.appendChild(el);
    state.items.push({ el, option });
  };
  for (const child of select.children) {
    if (child.tagName === 'OPTGROUP') {
      const group = document.createElement('div');
      group.className = 'dd-group';
      group.setAttribute('role', 'presentation');
      group.textContent = child.label;
      menu.appendChild(group);
      for (const option of child.children) addOption(option);
    } else if (child.tagName === 'OPTION') {
      addOption(child);
    }
  }
  if (state.items[state.active]) state.items[state.active].el.classList.add('active');
}

function open(select, trigger) {
  close();
  const menu = document.createElement('div');
  menu.className = 'dd-menu';
  menu.id = `dd-menu-${++uid}`;
  menu.setAttribute('role', 'listbox');
  menu.tabIndex = -1;
  const state = { select, trigger, menu, items: [], active: -1, typed: '', typedAt: 0 };
  render(state);
  document.body.appendChild(menu);
  trigger.setAttribute('aria-expanded', 'true');
  trigger.setAttribute('aria-controls', menu.id);
  current = state;
  place(state);
  const selected = state.items.findIndex(
    (item) => item.option.selected && !item.option.disabled);
  setActive(state, selected >= 0 ? selected : nextEnabled(state, -1, 1), true);
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', close);
  window.addEventListener('blur', close);
}

function close() {
  if (!current) return;
  const { trigger, menu } = current;
  menu.remove();
  trigger.setAttribute('aria-expanded', 'false');
  trigger.removeAttribute('aria-controls');
  trigger.removeAttribute('aria-activedescendant');
  document.removeEventListener('pointerdown', onPointerDown, true);
  document.removeEventListener('scroll', onScroll, true);
  window.removeEventListener('resize', close);
  window.removeEventListener('blur', close);
  current = null;
}

function place(state) {
  const { trigger, menu } = state;
  const rect = trigger.getBoundingClientRect();
  const margin = 8;
  const gap = 4;
  const below = window.innerHeight - rect.bottom - gap - margin;
  const above = rect.top - gap - margin;
  const wanted = Math.min(menu.scrollHeight, 360);
  const openBelow = below >= wanted || below >= above;
  const room = openBelow ? below : above;
  menu.style.minWidth = `${Math.round(rect.width)}px`;
  menu.style.maxHeight = `${Math.max(80, Math.min(360, room))}px`;
  const height = menu.offsetHeight;
  const width = menu.offsetWidth;
  const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
  const top = openBelow ? rect.bottom + gap : rect.top - gap - height;
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
  menu.dataset.placement = openBelow ? 'below' : 'above';
}

function setActive(state, index, scroll) {
  const previous = state.items[state.active];
  if (previous) previous.el.classList.remove('active');
  state.active = index;
  const item = state.items[index];
  if (!item) {
    state.trigger.removeAttribute('aria-activedescendant');
    return;
  }
  item.el.classList.add('active');
  state.trigger.setAttribute('aria-activedescendant', item.el.id);
  if (!scroll) return;
  const { menu } = state;
  const top = item.el.offsetTop;
  const bottom = top + item.el.offsetHeight;
  if (top < menu.scrollTop) menu.scrollTop = top - 4;
  else if (bottom > menu.scrollTop + menu.clientHeight) {
    menu.scrollTop = bottom - menu.clientHeight + 4;
  }
}

function nextEnabled(state, from, direction) {
  const { items } = state;
  for (let i = from + direction; i >= 0 && i < items.length; i += direction) {
    if (!items[i].option.disabled) return i;
  }
  return from >= 0 && from < items.length ? from : -1;
}

function choose(state, index) {
  const item = state.items[index];
  if (!item || item.option.disabled) return;
  const { select, trigger } = state;
  const changed = select.selectedIndex !== item.option.index;
  close();
  if (changed) {
    select.selectedIndex = item.option.index;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  trigger.focus();
}

function typeahead(state, char) {
  const now = performance.now();
  if (now - state.typedAt > TYPEAHEAD_RESET_MS) state.typed = '';
  state.typedAt = now;
  state.typed += char.toLowerCase();
  const repeated = state.typed.length > 1 && /^(.)\1+$/.test(state.typed);
  const query = repeated ? state.typed[0] : state.typed;
  const start = repeated || state.typed.length === 1 ? state.active + 1 : state.active;
  const { items } = state;
  for (let step = 0; step < items.length; step++) {
    const i = (Math.max(start, 0) + step) % items.length;
    const { option } = items[i];
    if (option.disabled) continue;
    if (option.textContent.trim().toLowerCase().startsWith(query)) {
      setActive(state, i, true);
      return;
    }
  }
}

function onTriggerKey(event, select, trigger) {
  const isOpen = current && current.select === select;
  const { key } = event;
  if (!isOpen) {
    const opens = key === 'ArrowDown' || key === 'ArrowUp' || key === 'Enter' || key === ' '
      || (key === 'ArrowDown' && event.altKey);
    if (opens && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      event.stopPropagation();
      open(select, trigger);
    }
    return;
  }
  const state = current;
  const last = state.items.length - 1;
  switch (key) {
    case 'ArrowDown': setActive(state, nextEnabled(state, state.active, 1), true); break;
    case 'ArrowUp': setActive(state, nextEnabled(state, state.active, -1), true); break;
    case 'Home': setActive(state, nextEnabled(state, -1, 1), true); break;
    case 'End': setActive(state, nextEnabled(state, last + 1, -1), true); break;
    case 'PageDown': {
      const target = Math.min(last, state.active + 8);
      setActive(state, state.items[target]?.option.disabled ? nextEnabled(state, target, -1) : target, true);
      break;
    }
    case 'PageUp': {
      const target = Math.max(0, state.active - 8);
      setActive(state, state.items[target]?.option.disabled ? nextEnabled(state, target, 1) : target, true);
      break;
    }
    case 'Enter':
    case ' ':
      choose(state, state.active);
      break;
    case 'Escape':
      close();
      trigger.focus();
      break;
    case 'Tab':
      close();
      return;
    default:
      if (key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
        typeahead(state, key);
        break;
      }
      return;
  }
  event.preventDefault();
  event.stopPropagation();
}

function onPointerDown(event) {
  if (!current) return;
  if (current.menu.contains(event.target) || current.trigger.contains(event.target)) return;
  close();
}

function onScroll(event) {
  if (!current || event.target === current.menu) return;
  const rect = current.trigger.getBoundingClientRect();
  if (rect.bottom < 0 || rect.top > window.innerHeight) {
    close();
  } else {
    place(current);
  }
}

document.querySelectorAll('select').forEach(enhance);
new MutationObserver((mutations) => {
  for (const mutation of mutations) {
    for (const node of mutation.addedNodes) {
      if (node.nodeType !== 1) continue;
      if (node.tagName === 'SELECT') enhance(node);
      else node.querySelectorAll?.('select').forEach(enhance);
    }
  }
}).observe(document.body, { childList: true, subtree: true });

export { enhance, close };
