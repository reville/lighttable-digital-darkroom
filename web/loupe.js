// SPDX-License-Identifier: GPL-3.0-only
/* Secondary Monitor Loupe Controller for Film Lab / LightTable.
 * Synchronizes with the primary window via BroadcastChannel.
 */

const channel = new BroadcastChannel('lighttable-loupe');

const $ = (id) => document.getElementById(id);
const hud = $('hud');
const photoName = $('photoName');
const photoMeta = $('photoMeta');
const viewport = $('viewport');
const stage = $('stage');
const loupeImg = $('loupeImg');
const emptyState = $('emptyState');
const btnFit = $('btnFit');
const btn100 = $('btn100');
const btn200 = $('btn200');
const lockFocusCheck = $('lockFocusCheck');

let state = {
  currentPhoto: null,
  zoomMode: '100', // 'fit', '100', '200'
  scale: 1.0,
  panX: 0,
  panY: 0,
  imgWidth: 0,
  imgHeight: 0,
  relativeFocus: { x: 0.5, y: 0.5 }, // Normalized focal point
  isDragging: false,
  dragStartX: 0,
  dragStartY: 0,
  hudTimer: null,
};

function showHudBriefly() {
  hud.classList.remove('idle');
  clearTimeout(state.hudTimer);
  state.hudTimer = setTimeout(() => {
    hud.classList.add('idle');
  }, 4500);
}

document.addEventListener('mousemove', showHudBriefly);
document.addEventListener('pointerdown', showHudBriefly);
hud.addEventListener('pointerenter', () => {
  clearTimeout(state.hudTimer);
  hud.classList.remove('idle');
});
hud.addEventListener('pointerleave', showHudBriefly);

function updateStageTransform() {
  stage.style.transform = `translate(${state.panX}px, ${state.panY}px) scale(${state.scale})`;
}

function updateButtons() {
  btnFit.classList.toggle('active', state.zoomMode === 'fit');
  btn100.classList.toggle('active', state.zoomMode === '100');
  btn200.classList.toggle('active', state.zoomMode === '200');
}

function applyZoom(mode, centerScreen = true) {
  state.zoomMode = mode;
  updateButtons();
  if (!state.imgWidth || !state.imgHeight) return;

  const vpW = viewport.clientWidth;
  const vpH = viewport.clientHeight;

  if (mode === 'fit') {
    const scale = Math.min(vpW / state.imgWidth, vpH / state.imgHeight);
    state.scale = scale;
    state.panX = (vpW - state.imgWidth * scale) / 2;
    state.panY = (vpH - state.imgHeight * scale) / 2;
  } else if (mode === '100') {
    state.scale = 1.0;
    if (centerScreen) {
      if (lockFocusCheck.checked && state.relativeFocus) {
        state.panX = vpW / 2 - state.imgWidth * state.relativeFocus.x;
        state.panY = vpH / 2 - state.imgHeight * state.relativeFocus.y;
      } else {
        state.panX = (vpW - state.imgWidth) / 2;
        state.panY = (vpH - state.imgHeight) / 2;
      }
    }
  } else if (mode === '200') {
    state.scale = 2.0;
    if (centerScreen) {
      if (lockFocusCheck.checked && state.relativeFocus) {
        state.panX = vpW / 2 - state.imgWidth * 2.0 * state.relativeFocus.x;
        state.panY = vpH / 2 - state.imgHeight * 2.0 * state.relativeFocus.y;
      } else {
        state.panX = (vpW - state.imgWidth * 2.0) / 2;
        state.panY = (vpH - state.imgHeight * 2.0) / 2;
      }
    }
  }
  updateStageTransform();
}

btnFit.onclick = () => applyZoom('fit');
btn100.onclick = () => applyZoom('100');
btn200.onclick = () => applyZoom('200');

viewport.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  state.isDragging = true;
  state.dragStartX = e.clientX - state.panX;
  state.dragStartY = e.clientY - state.panY;
  viewport.classList.add('dragging');
  viewport.setPointerCapture(e.pointerId);
});

viewport.addEventListener('pointermove', (e) => {
  if (!state.isDragging) return;
  state.panX = e.clientX - state.dragStartX;
  state.panY = e.clientY - state.dragStartY;
  updateStageTransform();

  // Update relative focus point under viewport center
  const vpW = viewport.clientWidth;
  const vpH = viewport.clientHeight;
  const centerX = (vpW / 2 - state.panX) / (state.imgWidth * state.scale);
  const centerY = (vpH / 2 - state.panY) / (state.imgHeight * state.scale);
  state.relativeFocus = {
    x: Math.max(0, Math.min(1, centerX)),
    y: Math.max(0, Math.min(1, centerY)),
  };
});

viewport.addEventListener('pointerup', (e) => {
  state.isDragging = false;
  viewport.classList.remove('dragging');
});

viewport.addEventListener('wheel', (e) => {
  e.preventDefault();
  const zoomFactor = e.deltaY < 0 ? 1.15 : 0.87;
  const newScale = Math.max(0.1, Math.min(8.0, state.scale * zoomFactor));

  // Zoom towards mouse cursor
  const mouseX = e.clientX;
  const mouseY = e.clientY;
  state.panX = mouseX - (mouseX - state.panX) * (newScale / state.scale);
  state.panY = mouseY - (mouseY - state.panY) * (newScale / state.scale);
  state.scale = newScale;
  state.zoomMode = Math.abs(state.scale - 1.0) < 0.05 ? '100' : 'custom';
  updateButtons();
  updateStageTransform();
}, { passive: false });

window.addEventListener('resize', () => {
  if (state.zoomMode === 'fit') applyZoom('fit');
});

function loadPhoto(data) {
  if (!data || !data.name) {
    emptyState.hidden = false;
    stage.hidden = true;
    photoName.textContent = '—';
    photoMeta.textContent = '';
    return;
  }

  state.currentPhoto = data.name;
  photoName.textContent = data.name.split('/').pop();
  photoMeta.textContent = data.metadata || '';

  emptyState.hidden = true;
  stage.hidden = false;

  const url = data.url || `/api/orig?name=${encodeURIComponent(data.name)}&w=3840&v=2`;
  const isNew = loupeImg.src !== url;

  loupeImg.onload = () => {
    state.imgWidth = loupeImg.naturalWidth;
    state.imgHeight = loupeImg.naturalHeight;
    applyZoom(state.zoomMode, isNew);
  };
  loupeImg.src = url;
  showHudBriefly();
}

channel.onmessage = (event) => {
  const msg = event.data;
  if (!msg) return;
  if (msg.type === 'sync') {
    loadPhoto(msg);
  }
};

// Ping the main window on launch
channel.postMessage({ type: 'ready' });
showHudBriefly();
