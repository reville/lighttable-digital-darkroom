// SPDX-License-Identifier: GPL-3.0-only
/* Keep keyboard navigation in the open dialog; existing close handlers retain
   their operation-specific Escape/Cancel behavior. */
export function installDialogFocus() {
  let active = null, opener = null, lastOutside = document.activeElement;
  const visible = node => Boolean(node?.isConnected && node.getClientRects().length);
  const controls = dialog => [...dialog.querySelectorAll(
    'button, input, select, textarea, a[href], summary, [tabindex]')]
    .filter(node => node.tabIndex >= 0 && !node.matches(':disabled, [aria-disabled="true"]') && visible(node));
  const remember = node => {
    if (node.closest('.modal-backdrop')) return;
    const menu = node.closest('[role="menu"]');
    lastOutside = menu?.id
      ? document.querySelector(`[aria-controls="${CSS.escape(menu.id)}"]`) || node
      : node;
  };
  document.addEventListener('focusin', event => remember(event.target));
  document.addEventListener('pointerdown', event => remember(event.target.closest('button, [role="menuitem"]') || event.target), true);
  new MutationObserver(() => {
    const next = [...document.querySelectorAll('.modal-backdrop.on')].at(-1) || null;
    if (next === active) return;
    const previous = active;
    active = next;
    if (next) {
      if (!previous) opener = lastOutside;
      const dialog = next.querySelector('[role="dialog"]') || next;
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-modal', 'true');
      if (!next.contains(document.activeElement)) {
        const initial = controls(next)[0] || dialog;
        if (initial === dialog) initial.tabIndex = -1;
        initial.focus({preventScroll: true});
      }
    } else if (previous) {
      if ((previous.contains(document.activeElement) || document.activeElement === document.body) && visible(opener)) {
        opener.focus({preventScroll: true});
      }
      opener = null;
    }
  }).observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['class']});
  document.addEventListener('keydown', event => {
    if (!active || event.key !== 'Tab' || event.defaultPrevented) return;
    const items = controls(active), first = items[0], last = items.at(-1);
    if (!first) { event.preventDefault(); return; }
    const current = document.activeElement;
    if (!active.contains(current) || (event.shiftKey && current === first) || (!event.shiftKey && current === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  }, true);
}
