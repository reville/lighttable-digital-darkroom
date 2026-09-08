import { t as tr } from './i18n.js';
/* Web MIDI hardware controller integration for Film Lab / LightTable.
 *
 * Connects to physical MIDI controllers (knobs, faders, consoles like Behringer
 * X-Touch, Loupedeck, MIDI Fighter) via navigator.requestMIDIAccess().
 * Maps incoming MIDI CC (Control Change) messages to live Develop and Film sliders
 * with zero latency, on-screen HUD readouts, and interactive MIDI Learn.
 */

const STORAGE_KEY = 'lighttable-midi-mappings';

const DEFAULT_MAPPINGS = {
  14: { type: 'grade', control: 'exposure', min: -3, max: 3, label: tr("Exposure") },
  15: { type: 'grade', control: 'contrast', min: -1, max: 1, label: tr("Contrast") },
  16: { type: 'grade', control: 'highlights', min: -1, max: 1, label: tr("Highlights") },
  17: { type: 'grade', control: 'shadows', min: -1, max: 1, label: tr("Shadows") },
  18: { type: 'grade', control: 'whites', min: -1, max: 1, label: tr("Whites") },
  19: { type: 'grade', control: 'blacks', min: -1, max: 1, label: tr("Blacks") },
  20: { type: 'grade', control: 'temp', min: -1, max: 1, label: tr("Temp") },
  21: { type: 'grade', control: 'tint', min: -1, max: 1, label: tr("Tint") },
  22: { type: 'grade', control: 'vibrance', min: -1, max: 1, label: tr("Vibrance") },
  23: { type: 'grade', control: 'saturation', min: -1, max: 1, label: tr("Saturation") },
  24: { type: 'grade', control: 'clarity', min: -1, max: 1, label: tr("Clarity") },
  25: { type: 'grade', control: 'texture', min: -1, max: 1, label: tr("Texture") },
  26: { type: 'params', control: 'grain_amount', min: 0.1, max: 2, label: tr("Grain size") },
  27: { type: 'params', control: 'halation_amount', min: 0, max: 3, label: tr("Halation") },
  28: { type: 'params', control: 'couplers_amount', min: 0, max: 2, label: tr("Couplers") },
  29: { type: 'params', control: 'exposure_ev', min: -3, max: 3, label: tr("Camera EV") },
};

let midiAccess = null;
let mappings = { ...DEFAULT_MAPPINGS };
let isLearning = false;
let learnTarget = null;
let changeDebounceTimers = new Map();
let hudTimeout = null;

function loadStoredMappings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        mappings = { ...DEFAULT_MAPPINGS, ...parsed };
      }
    }
  } catch {
    mappings = { ...DEFAULT_MAPPINGS };
  }
}

function saveMappings() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(mappings));
  } catch {
    // quota or privacy restriction
  }
}

export function resetMidiMappings() {
  mappings = { ...DEFAULT_MAPPINGS };
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {}
  updateMidiStatus();
}

function findControlInput(mapping) {
  if (mapping.type === 'grade') {
    return document.querySelector(`[data-g="${mapping.control}"]`);
  }
  return document.getElementById(mapping.control);
}

function showMidiHud(text) {
  const hud = document.getElementById('speedHud');
  if (!hud) return;
  hud.textContent = text;
  hud.hidden = false;
  clearTimeout(hudTimeout);
  hudTimeout = setTimeout(() => {
    hud.hidden = true;
  }, 1200);
}

function handleMidiMessage(event) {
  const data = event.data;
  if (!data || data.length < 3) return;

  const status = data[0] & 0xf0;
  // Control Change status byte: 0xB0 (channel 1) to 0xBF (channel 16)
  if (status !== 0xb0) return;

  const cc = data[1];
  const value7 = data[2];

  if (isLearning && learnTarget) {
    mappings[cc] = { ...learnTarget };
    saveMappings();
    showMidiHud(tr("Mapped CC {cc} → {value}", {cc: cc, value: (learnTarget.label || learnTarget.control)}));
    isLearning = false;
    learnTarget = null;
    updateMidiStatus();
    return;
  }

  const mapping = mappings[cc];
  if (!mapping) return;

  const input = findControlInput(mapping);
  if (!input) return;

  const min = mapping.min ?? Number(input.min) ?? 0;
  const max = mapping.max ?? Number(input.max) ?? 1;
  const step = Number(input.step) || 0.01;

  // Scale 0..127 to [min, max]
  const scaled = min + (value7 / 127.0) * (max - min);
  const stepped = Math.round(scaled / step) * step;
  const clamped = Math.max(min, Math.min(max, stepped));

  const decimals = step < 0.01 ? 3 : step < 0.1 ? 2 : 1;
  input.value = clamped.toFixed(decimals);
  input.dispatchEvent(new Event('input', { bubbles: true }));

  const row = input.closest('.slider-row') || input.closest('.row');
  const label = row?.querySelector('.name')?.textContent?.trim() || mapping.label || mapping.control;
  showMidiHud(tr("MIDI: {label}  {value}", {label: label, value: Number(input.value).toFixed(decimals)}));

  // Debounce change event to save state
  if (changeDebounceTimers.has(mapping.control)) {
    clearTimeout(changeDebounceTimers.get(mapping.control));
  }
  changeDebounceTimers.set(
    mapping.control,
    setTimeout(() => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
      changeDebounceTimers.delete(mapping.control);
    }, 300)
  );
}

export function setMidiLearnTarget(target) {
  if (!target) {
    isLearning = false;
    learnTarget = null;
    updateMidiStatus();
    return;
  }
  isLearning = true;
  learnTarget = target;
  showMidiHud(tr("Turn any knob to map → {value}", {value: (target.label || target.control)}));
  updateMidiStatus();
}

export function toggleMidiLearn() {
  isLearning = !isLearning;
  if (!isLearning) learnTarget = null;
  updateMidiStatus();
}

export function updateMidiStatus() {
  const pill = document.getElementById('midiPill');
  if (!pill) return;

  if (!navigator.requestMIDIAccess) {
    pill.textContent = tr("MIDI: Unavailable");
    pill.classList.remove('active', 'learning');
    pill.title = tr("Web MIDI API not supported in this browser");
    return;
  }

  if (!midiAccess) {
    pill.textContent = tr("MIDI: Off");
    pill.classList.remove('active', 'learning');
    return;
  }

  const inputs = Array.from(midiAccess.inputs.values());
  if (isLearning) {
    pill.textContent = learnTarget ? tr("MIDI Learn: {value}…", {value: (learnTarget.label || learnTarget.control)}) : tr("MIDI Learn: Click slider");
    pill.classList.add('learning');
    pill.classList.remove('active');
    pill.title = tr("Move any MIDI knob to map it to the active slider");
  } else if (inputs.length > 0) {
    const names = inputs.map((input) => input.name).join(', ');
    pill.textContent = tr("MIDI: {inputsLength} connected", {inputsLength: inputs.length});
    pill.classList.add('active');
    pill.classList.remove('learning');
    pill.title = tr("Connected: {names}. Turn knobs to adjust develop and film sliders.", {names: names});
  } else {
    pill.textContent = tr("MIDI: No devices");
    pill.classList.remove('active', 'learning');
    pill.title = tr("Connect a USB MIDI controller (knobs/faders) to control sliders directly.");
  }
}

function attachInputs() {
  if (!midiAccess) return;
  for (const input of midiAccess.inputs.values()) {
    input.onmidimessage = handleMidiMessage;
  }
  updateMidiStatus();
}

export async function initMidi() {
  loadStoredMappings();
  if (typeof navigator === 'undefined' || !navigator.requestMIDIAccess) {
    updateMidiStatus();
    return false;
  }

  try {
    midiAccess = await navigator.requestMIDIAccess({ sysex: false });
    midiAccess.onstatechange = () => {
      attachInputs();
    };
    attachInputs();
    return true;
  } catch (err) {
    console.warn('Web MIDI access declined or unavailable:', err);
    updateMidiStatus();
    return false;
  }
}
