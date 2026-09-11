// SPDX-License-Identifier: GPL-3.0-only
// Browser-side candidate oracles. A hit requires screenshot inspection, not automatic bug classification.
export function probe({state = {}, direction = 'ltr'} = {}) {
  const hits = [];
  const id = el => {
    if (el === document.documentElement) return 'html';
    if (el === document.body) return 'body';
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (n.id) { parts.unshift(`#${CSS.escape(n.id)}`); break; }
      parts.unshift(`${n.localName}:nth-child(${[...n.parentElement.children].indexOf(n) + 1})`);
    }
    return parts.join(' > ');
  };
  const add = (rule, el, detail) => hits.push({rule, selector: id(el), detail});
  const styled = el => {
    for (let n = el; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility !== 'visible' || Number(s.opacity) === 0 ||
          (n.matches('details:not([open])') && el !== n && !n.querySelector('summary')?.contains(el))) return false;
    }
    return true;
  };
  const inViewport = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  };
  const exposed = el => {
    if (!styled(el) || !inViewport(el)) return false;
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(innerWidth - 1, r.x + r.width / 2));
    const y = Math.max(0, Math.min(innerHeight - 1, r.y + r.height / 2));
    const top = document.elementFromPoint(x, y);
    return top === el || el.contains(top);
  };
  if (Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > innerWidth + 1) add('horizontal-overflow', document.documentElement,
    {scrollWidth: document.documentElement.scrollWidth, viewport: innerWidth});
  if (document.documentElement.dir !== direction) add('document-direction', document.documentElement,
    {expected: direction, actual: document.documentElement.dir});
  for (const el of document.querySelectorAll('[hidden]')) {
    if (styled(el) && inViewport(el)) add('hidden-paints', el, {});
  }
  const controls = [...document.querySelectorAll('button,input:not([type=hidden]),select,textarea,a[href],[tabindex],summary')];
  for (const el of controls) {
    if (!styled(el)) continue;
    const r = el.getBoundingClientRect();
    // Styled custom inputs intentionally retain an invisible native input; report as candidates.
    if (!r.width || !r.height) add('zero-size-control', el, {width:r.width, height:r.height});
    if (!exposed(el)) continue;
    if (el.tabIndex > 0) add('positive-tab-order', el, {tabIndex:el.tabIndex});
    if (el.closest('[aria-hidden=true]')) add('focus-in-aria-hidden', el, {});
    if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).textOverflow !== 'ellipsis' &&
        !el.matches('input,textarea,select')) add('control-text-overflow', el,
      {scrollWidth:el.scrollWidth, clientWidth:el.clientWidth});
  }
  const visibleControls = controls.filter(el => styled(el) && inViewport(el) && !el.closest('[inert]'));
  for (let i = 0; i < visibleControls.length; i++) for (let j = i + 1; j < visibleControls.length; j++) {
    const a = visibleControls[i], b = visibleControls[j];
    if (a.contains(b) || b.contains(a)) continue;
    const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
    const w = Math.min(x.right,y.right)-Math.max(x.left,y.left), h = Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top);
    // Ignore controls in different overlays, deliberate label/input relationships, and tiny edge contacts.
    if (w > 3 && h > 3 && w*h > Math.min(x.width*x.height,y.width*y.height)*.25 &&
        a.closest('[role=dialog],.modal-backdrop,#survey,.loupe-container') === b.closest('[role=dialog],.modal-backdrop,#survey,.loupe-container') &&
        (exposed(a) || exposed(b))) add('overlapping-controls', a, {other:id(b), width:w, height:h});
  }
  const rgb = s => { const m = s.match(/^rgba?\(([^)]+)\)$/); return m ? m[1].split(/[, /]+/).map(Number) : null; };
  const lum = c => c.slice(0,3).map(v => v/255).map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4)
    .reduce((sum,v,i) => sum+v*[.2126,.7152,.0722][i],0);
  for (const el of document.querySelectorAll('label,button,summary,p,h1,h2,h3,span,strong,small')) {
    if (!exposed(el) || ![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (el.scrollWidth > el.clientWidth + 2 && s.textOverflow !== 'ellipsis' &&
        ['hidden','clip'].includes(s.overflowX)) add('clipped-text', el, {text:el.textContent.trim().slice(0,100)});
    // Only solid, opaque foreground/background pairs: no guessed contrast over photos or gradients.
    const fg = rgb(s.color); let bg = null, uncertain = false;
    for (let n = el; n; n = n.parentElement) {
      const ns = getComputedStyle(n), color = rgb(ns.backgroundColor);
      if (ns.backgroundImage !== 'none' || Number(ns.opacity) !== 1) { uncertain = true; break; }
      if (color && (color[3] ?? 1) === 1) { bg = color; break; }
      if (color && color[3] > 0) { uncertain = true; break; }
    }
    if (fg && bg && !uncertain && (fg[3] ?? 1) === 1 && !el.closest(':disabled')) {
      const ratio = (Math.max(lum(fg),lum(bg))+.05)/(Math.min(lum(fg),lum(bg))+.05);
      const large = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.66 && parseInt(s.fontWeight) >= 700);
      if (ratio < (large ? 3 : 4.5)) add('text-contrast', el, {ratio:+ratio.toFixed(2), floor:large?3:4.5});
    }
    if (r.bottom > innerHeight + 1 && !el.closest('.modal-backdrop') && exposed(el) &&
        ![...function*(){for(let n=el.parentElement;n;n=n.parentElement)yield n;}()].some(n =>
          /auto|scroll/.test(getComputedStyle(n).overflowY))) add('viewport-clipping', el, {bottom:r.bottom});
  }
  const cv = document.querySelector('#cv'), wrap = document.querySelector('#zoomwrap');
  if (state.zoomMode === 'fit' && state.viewMode === 'detail' && state.render?.state === 'ready' &&
      cv && wrap && exposed(cv) && !state.activeTool) {
    const a = cv.getBoundingClientRect(), b = wrap.getBoundingClientRect();
    if (a.left < b.left-2 || a.right > b.right+2 || a.top < b.top-2 || a.bottom > b.bottom+2)
      add('fit-geometry', cv, {photo:a.toJSON(), viewport:b.toJSON()});
  }
  return hits;
}
