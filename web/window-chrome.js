import { nativeBridge, sendNative } from '/web/native-bridge.js';

const CONTROL_SELECTOR = [
  'button', 'input', 'select', 'textarea', 'a[href]', 'summary',
  '[role="button"]', '[role="link"]', '[contenteditable="true"]',
  '[data-window-no-drag]',
].join(',');

function rectPayload(rect) {
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}

function isVisible(element, rect) {
  const style = getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
    style.visibility !== 'hidden' && style.pointerEvents !== 'none';
}

function blocksWindowDrag(element, rect) {
  return isVisible(element, rect) &&
    !element.matches(':disabled, [aria-disabled="true"]');
}

export function installNativeWindowChrome() {
  if (!nativeBridge() || window.__LIGHTTABLE_PLATFORM__ === 'windows') return;
  const topBar = document.querySelector('.topbar');
  if (!topBar) return;

  let frame = 0;
  let dragBlocked = false;
  const sync = () => {
    frame = 0;
    const topBarRect = topBar.getBoundingClientRect();
    const controls = [...topBar.querySelectorAll(CONTROL_SELECTOR)]
      .map((element) => [element, element.getBoundingClientRect()])
      .filter(([element, rect]) => blocksWindowDrag(element, rect))
      .map(([, rect]) => rectPayload(rect));
    sendNative('windowChromeLayout', {
      topBar: rectPayload(topBarRect),
      controls,
      blocked: dragBlocked,
    });
  };
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(sync);
  };

  const resizeObserver = new ResizeObserver(schedule);
  resizeObserver.observe(topBar);
  for (const control of topBar.querySelectorAll(CONTROL_SELECTOR)) {
    resizeObserver.observe(control);
  }
  const mutationObserver = new MutationObserver(schedule);
  mutationObserver.observe(topBar, {
    attributes: true,
    characterData: true,
    childList: true,
    subtree: true,
  });
  const blockerObserver = new MutationObserver(() => {
    const next = Boolean(document.querySelector('.modal-backdrop.on'));
    if (next === dragBlocked) return;
    dragBlocked = next;
    schedule();
  });
  blockerObserver.observe(document.body, {
    attributes: true,
    attributeFilter: ['class'],
    subtree: true,
  });
  window.addEventListener('resize', schedule);
  window.addEventListener('pageshow', schedule);
  document.fonts?.ready.then(schedule);
  schedule();
}
