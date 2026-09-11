import { t as tr, tn as trn } from './i18n.js';
const i18nHTML = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
import { installKeywordBatch } from '/web/keyword-batch.js';
import { installLibraryFilters, matchesLibraryFilters, photoHasEdits } from '/web/library-filters.js';
import { close as closeDropdown } from '/web/dropdown.js';
import {installDialogFocus} from '/web/dialog-focus.js';
import {installMaskBatch, mergeMaskDelta} from '/web/batch-masks.js';
import {createSelectionRequest} from '/web/selection-request.js';
import { createFilmBrowser, filmParamsForStock, filmChoiceValue, filmSelectionForChoice,
  normalizeFilmTuning, mergeFilmTuning, filmStockGroups } from '/web/film-browser.js';
import { GradeRenderer, GRADE_DEFAULTS, HSL_BANDS } from '/web/gl.js';
import { api } from '/web/api.js';
import { nativeBridge, sendNative } from '/web/native-bridge.js';
import { createPhotoClipboard, installPhotoCopyContextMenu } from '/web/photo-clipboard.js';
import { createCloseBarrier } from '/web/close-barrier.js';
import { installDesktopUpdates } from '/web/desktop-updates.js';
import { createEditRecovery, recoveryPayloadMatches, recoveryAcknowledged } from '/web/edit-recovery.js';
import { createAppState, cloneValue } from '/web/state.js';
import { createEditSaveQueue } from '/web/edit-save-queue.js';
import { createCullBatch, cullFlagTargets } from '/web/cull-batch.js';
import { groupSimilarPhotos, cullSuggestion } from '/web/cull-similarity.js';
import { createPhotoUndoHistory } from '/web/photo-undo.js';
import { previewDetailLabel, previewFailureMessage } from '/web/preview-detail.js';
import { createZoomMotion, smoothZoomEnabled } from '/web/zoom-motion.js';
import { createPhotoPanMemory } from '/web/photo-pan.js';
import { previewResolutionPreference } from '/web/preview-preferences.js';
import { createPreviewProgress, waitForRawRefinement } from '/web/preview-progress.js';
import { screenOverlayGeometry, prepareScreenOverlay } from '/web/screen-overlay.js';
import { clampComparePosition, compareViewGeometry, comparePositionAtViewCenter } from '/web/compare-view.js';
import { installCaptureTime, captureSortValue } from '/web/capture-time.js';
import { TRANSFER_GROUPS, transferChoices, transferPatch, regenerateTransferMasks,
  cropGeometry, restoreCropGeometry } from '/web/edit-transfer.js';
import { pairKey, indexPairs, pairViewPreference, collapsePairs, pairedTargets } from '/web/photo-pairs.js';
import {
  LOCAL_GRADE_DEFAULTS, OPTICS_DEFAULTS, MAX_MASKS, MAX_MASK_COMPONENTS,
  MAX_TOTAL_MASK_POINTS, MAX_HEALS, LINEAR_MIN_SPAN,
  normalizeMasks, normalizeHeals, normalizeOptics, localToolLabel,
} from '/web/editor-panels.js';
import {
  photoMatchesQuery as matchesPhotoQuery,
  photoMatchesRules as matchesPhotoRules,
} from '/web/library.js';
import {
  LABELS, LABEL_KEYS, LABEL_COLORS, LABEL_TITLES, KEY_SCHEMES,
  cleanLabel, labelSwatch, renderLabelRow,
} from '/web/labels.js';
import { CULL_SELECT, CULL_REJECT, cullVerdict, cullMatches, cullTally, aiSkippedSummary } from '/web/local-ai.js';
import { createSurvey } from '/web/survey.js';
import { createHistoryPanel } from '/web/history-panel.js';
import { createMetadataPanel } from '/web/metadata-panel.js';
import { createCatalogUI } from '/web/catalog-ui.js';
import { createEnhancePanel } from '/web/enhance-panel.js';
import { createPeoplePanel } from '/web/people.js';
import { installFirstRunSetup } from '/web/first-run.js';
import { installApplePhotosBrowser } from '/web/apple-photos.js';
import { installRecovery } from '/web/recovery.js';
import {
  initMidi, setMidiLearnTarget, toggleMidiLearn, resetMidiMappings,
} from '/web/midi.js';

installDialogFocus();

/* Declared here, built at the end of the file once every function they close
 * over exists, so start-up code never touches a `const` before it exists. */
let SURVEY = null;
let HISTORY = null;
let METADATA = null;
let CATALOG_UI = null;
let FIRST_RUN = null;
let APPLE_PHOTOS = null;
let RECOVERY = null;
let CAPTURE_TIME = null;
let UI_BRIDGE = null;
let PRESET_BROWSER = null;
let ENHANCE = null;
let MASK_CURVE = null;
let EXTERNAL_EDITORS = [];
let EXTERNAL_PREFS = {};
import { afterVisiblePaint, createFrameScheduler, debounce } from '/web/render-scheduler.js';
import {
  monotoneLUT, isIdentityPoints, evalParametricLUT, rgbHue,
  emptyColorGrading as makeEmptyColorGrading,
} from '/web/color-tools.js';
import { bytesToBase64, hasApplicablePresetSettings, composePresetState } from '/web/presets.js';
import { presetEditState, blendPresetState, reconcilePresetAdjustment, presetControlledSettings } from '/web/preset-amount.js';
import { syncNumericControl, readNumericControl } from '/web/numeric-controls.js';
import { createPresetBrowser, presetKey, migratePresetFavorites } from '/web/preset-browser.js';
import { installNativeWindowChrome } from '/web/window-chrome.js';
import { installDesktopTheme } from '/web/desktop-theme.js';
import { installUIBridge } from '/web/ui-bridge.js';
import { installSettings } from '/web/settings.js';
import { HELP_SECTION_TOPICS } from '/web/help-search.js';
import { createInteractionRecorder } from '/web/interaction-perf.js';
import { createPresentationCache, renderRequestKey } from '/web/presentation-cache.js';
import { gradeBakeRequest, gradeBakeKey } from '/web/preview-processing.js';
import { createStrokeRasterCache, autoMaskValues } from '/web/mask-raster.js';
import { linearHandleAt, editLinear, radialHandles, radialHandleAt, editRadial } from '/web/mask-shape.js';
import { editOverlayCursor, healHandleAt as findHealHandle, installCanvasHandleCursor } from '/web/edit-cursor.js';
import { installMaskCurve } from '/web/mask-curve.js';
import { createGridLayout, gridRowNeighbour, visibleGridPositions, automaticPreviewWidth, createSummaryCache } from '/web/view-performance.js';
import { bindThumbnailErrors } from '/web/thumbnail-errors.js';
import { createPhotoDisplayStatus } from '/web/photo-display-status.js';

const $ = (id) => document.getElementById(id);

const FILM_SLIDERS = ['exposure_ev', 'print_exposure', 'gamma',
  'couplers_amount', 'halation_amount', 'grain_amount', 'glare_amount',
  'wb_temperature', 'wb_tint', 'camera_diffusion_strength', 'print_preflash',
  'print_y_filter_shift', 'print_m_filter_shift', 'scan_softness',
  'scan_sharpness'];
const FILM_TOGGLES = ['auto_exposure', 'couplers_on', 'halation_on',
  'grain_on', 'glare_on', 'scan_sharpen', 'paper_locked'];
const FILM_SELECTS = ['workflow_mode', 'wb_mode', 'film_format', 'output_recipe',
  'raw_profile', 'raw_highlight_recovery', 'raw_sensor_denoise',
  'developProfile'];
const RESET_GROUPS = {
  raw: ['raw_profile', 'raw_highlight_recovery', 'raw_sensor_denoise',
        'learned_denoise', 'learned_denoise_strength',
        'developProfile'],
  // Film resets restore sliders while keeping switches and selected options.
  film: ['wb_temperature', 'wb_tint', 'exposure_ev', 'print_exposure', 'gamma'],
  stages: ['couplers_amount', 'halation_amount', 'grain_amount', 'glare_amount',
           'camera_diffusion_strength', 'print_preflash',
           'print_y_filter_shift', 'print_m_filter_shift',
           'scan_softness', 'scan_sharpness'],
  tone: ['exposure', 'contrast', 'highlights', 'shadows', 'whites', 'blacks'],
  colour: ['temp', 'tint', 'vibrance', 'saturation', 'monochrome'],
  effects: ['texture', 'clarity', 'dehaze', 'vignette', 'vignetteSize', 'vignetteFeather'],
  detail: ['sharpness', 'sharpenRadius', 'sharpenDetail', 'sharpenMasking',
           'luminanceNoise', 'colorNoise'],
  optics: ['chromaticAberrationRedCyan', 'chromaticAberrationBlueYellow'],
};
const S = createAppState(GRADE_DEFAULTS, OPTICS_DEFAULTS);
S.priorPhotoSettings = null;
const photoUndo = createPhotoUndoHistory();
const APP_PREFS = {};
const PHOTO_DISPLAY_STATUS = createPhotoDisplayStatus();
const LIBRARY_FILTERS = installLibraryFilters({ el: $, closeDropdown,
  onChange: () => { refreshFilteredView(); savePrefs(); } });
let KEYWORD_BATCH = null;
let MASK_BATCH = null;
let SELECTION_REQUEST = null;
let KEY_SCHEME_NAME = 'lighttable';
let KEYS = KEY_SCHEMES[KEY_SCHEME_NAME];
const CLIENT_ID = (crypto.randomUUID && crypto.randomUUID()) ||
  Math.random().toString(36).slice(2);
window.__LIGHTTABLE_CLIENT_ID__ = CLIENT_ID;
const NATIVE_PREVIEW = Boolean(window.__LIGHTTABLE_NATIVE_PREVIEW__ &&
  nativeBridge());
const nativePreviewPending = new Map();
const presentationCache = createPresentationCache();
const GRADE_PERF = createInteractionRecorder(Boolean(window.__LIGHTTABLE_BENCHMARK__));

/* ------------------------------------------------------------------ utils */
function toast(msg, action = null, duration = null) {
  const t = $('toast');
  t.replaceChildren(document.createTextNode(msg));
  t.classList.toggle('toast-stacked', !!action?.link);
  if (action?.run) {
    const button = document.createElement(action.link ? 'a' : 'button');
    if (action.link) { button.href = '#'; button.className = 'toast-link'; }
    else button.type = 'button';
    button.textContent = action.label || tr('Undo');
    button.onclick = (event) => {
      event.preventDefault();
      t.classList.remove('show');
      t.inert = true;
      action.run();
    };
    t.appendChild(button);
  }
  t.classList.add('show');
  t.inert = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    t.classList.remove('show');
    t.inert = true;
  }, duration ?? (action ? 5000 : 1800));
}

function notifyCompletion(title, message) {
  if (document.body.dataset.completionNotifications !== '1') return;
  sendNative('notify', { title, message });
}

let nameDialogResolve = null;
function closeNameDialog(value) {
  $('nameDialog').classList.remove('on');
  $('nameDialog').setAttribute('aria-hidden', 'true');
  if (nameDialogResolve) nameDialogResolve(value);
  nameDialogResolve = null;
}
function askName(title, initial = '') {
  if (nameDialogResolve) closeNameDialog(null);
  $('nameDialogTitle').textContent = title;
  $('nameDialogInput').value = initial;
  $('nameDialog').classList.add('on');
  $('nameDialog').setAttribute('aria-hidden', 'false');
  setTimeout(() => { $('nameDialogInput').focus(); $('nameDialogInput').select(); }, 0);
  return new Promise((resolve) => { nameDialogResolve = resolve; });
}
$('nameDialogCancel').onclick = () => closeNameDialog(null);
$('nameDialogSave').onclick = () => closeNameDialog($('nameDialogInput').value.trim() || null);
$('nameDialogInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('nameDialogSave').click(); }
  else if (e.key === 'Escape') { e.preventDefault(); closeNameDialog(null); }
});
$('nameDialog').addEventListener('pointerdown', (e) => {
  if (e.target === $('nameDialog')) closeNameDialog(null);
});
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const cur = () => S.images[S.idx];
const isRawInput = () => cur()?.raw === true;
const fmtG = (v) => (v >= 0 ? ' ' : '') + (+v).toFixed(2);
const displayName = (im) => im && (im.displayName || im.name.split('/').pop());
const editId = (prefix) => `${prefix}-${(crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`}`;

const selectedMask = () => S.masks.find((mask) => mask.id === S.selectedMaskId) || null;
const selectedHeal = () => S.heals.find((spot) => spot.id === S.selectedHealId) || null;

function postNative(action, detail = {}, silent = false) {
  if (!sendNative(action, detail)) {
    if (!silent) toast(tr("Folder actions are available in the LightTable desktop app"));
    return false;
  }
  return true;
}

let nativeMenuStateTimer = null;
function menuTextEditing() {
  if (document.getElementById('peopleDialog')?.classList.contains('on')) return true;
  const active = document.activeElement;
  return !!active && (active.isContentEditable ||
    ['INPUT', 'TEXTAREA'].includes(active.tagName));
}

const PHOTO_CLIPBOARD = createPhotoClipboard({
  getPayload() {
    const image = cur();
    if (menuTextEditing() || document.querySelector('.modal-backdrop.on') ||
        !image || isVideo(image) || !S.params || S.editingName !== image.name) return null;
    readControls();
    return { name: image.name, engine: $('engine').value, state: JSON.parse(snapshot()) };
  },
  nativeBridge: () => window.webkit?.messageHandlers?.lightTable,
  notify: message => toast(message),
});

installPhotoCopyContextMenu($('zoomwrap'), {
  available: () => !!window.webkit?.messageHandlers?.lightTable &&
    !!cur() && !isVideo(cur()) && !!S.params && S.editingName === cur().name &&
    !document.querySelector('.modal-backdrop.on'),
  openMenu: point => postNative('showPhotoCopyMenu', point, true),
});

function isLibraryVisible() {
  return window.innerWidth <= 800
    ? $('appShell').classList.contains('left-expanded')
    : !$('appShell').classList.contains('left-collapsed');
}

function nativeMenuState() {
  const image = cur();
  const targets = transferTargets();
  const collection = activeCollection();
  const ordered = visible();
  const visibleIndex = ordered.indexOf(image);
  return {
    ready: !!S.params,
    textEditing: menuTextEditing(),
    hasImages: S.images.length > 0,
    hasPhoto: !!image,
    editablePhoto: !!image && !isVideo(image),
    canCopyPhoto: !!image && !isVideo(image) && !!S.params && S.editingName === image.name &&
      !document.querySelector('.modal-backdrop.on'),
    selectedCount: targets.length,
    hasMultiSelection: S.msel.size > 0,
    currentVirtual: !!image?.virtual,
    currentFlag: image?.status || '',
    currentRating: +(image?.rating || 0),
    currentLabel: cleanLabel(image?.label),
    hasPreviousPhoto: visibleIndex > 0,
    hasNextPhoto: visibleIndex >= 0 && visibleIndex < ordered.length - 1,
    hasRejected: visible().some((item) => item.status === 'skipped'),
    canUndo: S.undo.length > 0,
    canRedo: S.redo.length > 0,
    canPaste: !!S.clipboard && targets.length > 0,
    canPrevious: Boolean(S.priorPhotoSettings && image && S.priorPhotoSettings.sourceName !== image.name),
    hasCrop: !!S.crop || !!(S.params?.rotate % 360) ||
      ['rotate', 'vertical', 'horizontal', 'scale', 'flipHorizontal', 'flipVertical']
        .some((key) => S.optics[key] !== OPTICS_DEFAULTS[key]),
    hasMasks: S.masks.length > 0,
    hasHealing: S.heals.length > 0,
    hasLensCorrections: ['profileEnabled', 'profileDistortion', 'profileVignette', 'distortion', 'vignette'].some(
      (key) => S.optics[key] !== OPTICS_DEFAULTS[key]),
    canStack: targets.length >= 2,
    canUnstack: targets.some((item) => !!stackForImage(item.name)),
    canAddToCollection: !!collection && collection.type === 'regular' &&
      targets.length > 0,
    catalogEnabled: !!S.catalogEnabled,
    viewMode: S.viewMode,
    activePane: S.activePane,
    filmEnabled: S.params?.profile_enabled !== false,
    compareActive: !!S.compareActive,
    compareEnabled: !!cur() && !S.wbPick && !S.pointColorPick && !S.maskColorPick,
    softProofEnabled: !!S.softProof.enabled,
    libraryVisible: isLibraryVisible(),
    filmstripVisible: !document.querySelector('.workspace')
      .classList.contains('filmstrip-hidden'),
    hasSelectedPreset: !!selectedPreset(),
    keyScheme: APP_PREFS.keyScheme === 'classic' ? 'classic' : 'lighttable',
  };
}

function sendNativeMenuState() {
  nativeMenuStateTimer = null;
  if (!nativeBridge()) return;
  postNative('menuState', { state: nativeMenuState() }, true);
}

function scheduleNativeMenuState(delay = 0) {
  if (!nativeBridge()) return;
  clearTimeout(nativeMenuStateTimer);
  nativeMenuStateTimer = setTimeout(sendNativeMenuState, delay);
}

function showShortcutSettings() {
  window.LightTableSettings?.open('shortcuts');
}

async function revealCurrentPhoto() {
  const image = cur();
  if (!image) return;
  const result = await api('/api/photos/reveal', { name: image.name });
  if (result.error || !result.path) {
    toast(((result.error || tr("That photo is not available in Finder"))));
    return;
  }
  postNative('revealFolder', { path: result.path });
}

function setActionDialog(id, open) {
  const dialog = $(id);
  if (!dialog) return;
  dialog.classList.toggle('on', open);
  dialog.setAttribute('aria-hidden', String(!open));
  if (open) {
    updateTransferActions();
    if (id === 'enhanceDialog') void ENHANCE?.refresh();
    requestAnimationFrame(() => dialog.querySelector('select, input, button')?.focus());
  }
}
for (const [dialogId, cancelId] of [
  ['enhanceDialog', 'enhanceDialogCancel'], ['mergeDialog', 'mergeDialogCancel'],
]) {
  $(cancelId).onclick = () => setActionDialog(dialogId, false);
  $(dialogId).addEventListener('pointerdown', (event) => {
    if (event.target === $(dialogId)) setActionDialog(dialogId, false);
  });
}
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if ($('enhanceDialog').classList.contains('on')) setActionDialog('enhanceDialog', false);
  if ($('mergeDialog').classList.contains('on')) setActionDialog('mergeDialog', false);
});

function selectionScope() {
  return JSON.stringify([S.activeFolder, S.includeSubfolders, S.activeCollection,
    ...['filter', 'ratingFilter', 'kindFilter', 'labelFilter', 'editFilter', 'search', 'sort'].map(id => $(id)?.value),
    LIBRARY_FILTERS.types(), LIBRARY_FILTERS.metadata(), LIBRARY_FILTERS.hideUndisplayable(),
    PHOTO_DISPLAY_STATUS.revision, S.library.stacks, S.cull, pairViewPreference(APP_PREFS), [...pairOverrides]]);
}
function setAllPhotoSelection(selected) {
  if (selected) return SELECTION_REQUEST.selectAll();
  SELECTION_REQUEST.cancel();
  S.msel.clear();
  refreshLists();
}

function performNativeMenuCommand(command) {
  if (command === 'help') { window.LightTableHelp?.open(); return; }
  if (window.LightTableHelp?.isOpen()) return;
  let result;
  if (command.startsWith('flag:')) {
    setStatus(command.slice('flag:'.length));
  } else if (command.startsWith('rating:')) {
    setRating(+command.slice('rating:'.length));
  } else if (command.startsWith('label:')) {
    setLabel(command.slice('label:'.length));
  } else if (command.startsWith('view:')) {
    setViewMode(command.slice('view:'.length));
  } else if (command.startsWith('pane:')) {
    const pane = `${command.slice('pane:'.length)}Pane`;
    switchPane(pane);
  } else {
    switch (command) {
      case 'undo': undo(); break;
      case 'redo': redo(); break;
      case 'selectAll': result = setAllPhotoSelection(true); break;
      case 'deselectAll': setAllPhotoSelection(false); break;
      case 'search': $('search').focus(); $('search').select(); break;
      case 'preferences': window.LightTableSettings?.open('general'); break;
      case 'keyboardShortcuts': showShortcutSettings(); break;
      case 'importCard': $('ingestOpen').click(); break;
      case 'exportPhotos': $('exportBtn').click(); break;
      case 'newCollection': $('addCollection').click(); break;
      case 'newSmartCollection': $('addSmartCollection').click(); break;
      case 'addToCollection': $('addToCollection').click(); break;
      case 'virtualCopy': $('virtualCopyBtn').click(); break;
      case 'deleteVirtualCopy': $('deleteVirtualBtn').click(); break;
      case 'stack': $('stackBtn').click(); break;
      case 'unstack': $('unstackBtn').click(); break;
      case 'importCatalog': $('importCatalogBtn').click(); break;
      case 'importSidecars': $('importSidecarsBtn').click(); break;
      case 'backupCatalog': $('catalogBackup').click(); break;
      case 'findDuplicates': $('catalogDuplicates').click(); break;
      case 'previousPhoto': goRelative(-1); break;
      case 'nextPhoto': goRelative(1); break;
      case 'rotateLeft': rotate(-90); break;
      case 'rotateRight': rotate(90); break;
      case 'renamePhoto': $('renameOpen').click(); break;
      case 'revealPhoto': result = revealCurrentPhoto(); break;
      case 'editExternal': $('editExternalOpen').click(); break;
      case 'deleteRejected': result = trashRejected(); break;
      case 'copySettings': $('copyBtn').click(); break;
      case 'copyPhoto': result = PHOTO_CLIPBOARD.copy(); break;
      case 'pasteSettings': $('pasteBtn').click(); break;
      case 'previousSettings': $('previousBtn')?.click(); break;
      case 'pasteAllVisible': $('pasteAllBtn').click(); break;
      case 'matchExposure': $('matchExposureBtn').click(); break;
      case 'buildPreviews': $('pregenPreviewsBtn').click(); break;
      case 'batchAiMask': $('batchAiMaskBtn').click(); break;
      case 'enhancePhoto': setActionDialog('enhanceDialog', true); break;
      case 'photoMerge': setActionDialog('mergeDialog', true); break;
      case 'resetEdit': $('resetEdit').click(); break;
      case 'resetFilm': $('resetFilm').click(); break;
      case 'resetCrop': $('cropReset').click(); break;
      case 'resetMasks': $('maskReset').click(); break;
      case 'resetHealing': $('healReset').click(); break;
      case 'resetLens': $('lensReset').click(); break;
      case 'filmToggle': $('filmProfileToggle').click(); break;
      case 'savePreset': $('presetSave').click(); break;
      case 'importPreset': $('presetImport').click(); break;
      case 'exportPreset': $('presetExport').click(); break;
      case 'survey': openSurvey('survey'); break;
      case 'toggleLibrary': $('leftPanelToggle').click(); break;
      case 'toggleFilmstrip': $('filmstripToggle').click(); break;
      case 'compare': setCompareActive(!S.compareActive); break;
      case 'secondaryLoupe': $('secondaryLoupeBtn').click(); break;
      case 'softProof':
        $('softProofEnabled').checked = !$('softProofEnabled').checked;
        $('softProofEnabled').dispatchEvent(new Event('change', { bubbles: true }));
        break;
      case 'zoomIn': zoomCentre(1.25); break;
      case 'zoomOut': zoomCentre(1 / 1.25); break;
      case 'zoomFit': zoomReset({animate: true}); break;
      case 'zoomActual': $('zoom1').click(); break;
      default: return;
    }
  }
  Promise.resolve(result).finally(() => scheduleNativeMenuState());
  scheduleNativeMenuState(250);
  return Promise.resolve(result);
}

window.lightTableNativeEvent = (event) => {
  if (event?.type === 'photoClipboardReply') { PHOTO_CLIPBOARD.complete(event); return; }
  if (event?.type === 'closeCancelled') { window.lightTableCancelClose?.(); return; }
  if (event?.type === 'editJournalReply') {
    window.dispatchEvent(new CustomEvent('lighttable-edit-journal', {detail: event}));
    return;
  }
  if (event?.type === 'error') return toast(((event.message || tr("Folder action failed"))));
  if (event?.type === 'menuCommand') {
    performNativeMenuCommand(event.command || '');
    return;
  }
  if (event?.type === 'requestMenuState') {
    scheduleNativeMenuState();
    return;
  }
  if (event?.type === 'exportFolderSelected') {
    const dest = event.path || 'film-exports';
    $('exDestination').value = dest;
    if ($('modalExDestination')) $('modalExDestination').value = dest;
    if ($('exportDialog').classList.contains('on')) scheduleExportPreview();
    return;
  }
  if (event?.type === 'editors') {
    EXTERNAL_EDITORS = Array.isArray(event.editors) ? event.editors : [];
    for (const select of [$('externalEditor'), $('settingsExternalEditor')].filter(Boolean)) {
      const saved = APP_PREFS.externalEditor || EXTERNAL_PREFS.externalEditor || {};
      select.replaceChildren(new Option(tr("Default application"), ''),
        ...EXTERNAL_EDITORS.map((editor) => new Option(editor.name, editor.path)));
      if (['windows', 'linux'].includes(window.__LIGHTTABLE_PLATFORM__)) {
        select.appendChild(new Option(tr("Choose application…"), '__choose__'));
      }
      if (saved.path && !select.querySelector(
        `option[value="${CSS.escape(saved.path)}"]`)) {
        select.appendChild(new Option(((saved.name || saved.path.split('/').pop())), saved.path));
      }
      select.value = saved.path || '';
    }
    return;
  }
  if (event?.type === 'editorChosen') {
    for (const select of [$('externalEditor'), $('settingsExternalEditor')].filter(Boolean)) {
      const option = new Option(((event.name || tr("Application"))), event.path || '');
      select.insertBefore(option, select.querySelector('[value="__choose__"]'));
      select.value = event.path || '';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return;
  }
  if (event?.type === 'preferenceFolderSelected') {
    window.dispatchEvent(new CustomEvent('lighttable-preference-folder', {
      detail: { key: event.key, path: event.path },
    }));
    return;
  }
  if (event?.type === 'presetSaved') return toast(tr("Exported {eventFilename}", {eventFilename: event.filename}));
  if (event?.type === 'presetFilesSelected') {
    importPresetUploads(event.files || [], event.failures || []);
    return;
  }
  if (event?.type === 'photosImported') {
    const count = +event.count || 0;
    const failures = +event.failures || 0;
    const message = trn("Imported {count} photo from Apple Photos{value}", "Imported {count} photos from Apple Photos{value}", count, {count: count, value: failures ? tr(" · {failures} failed", {failures: failures}) : ''});
    toast(event.cancelled ? [tr('Import stopped'), message].join(' · ') : message);
    return;
  }
  if (event?.type === 'trashed') {
    void reloadLibrary();
    return;
  }
  if (event?.type === 'nativeInteractionPresented') {
    if (event.sample?.generation === S.seq) {
      GRADE_PERF.presented(event.sample, 'native-metal-acknowledged');
    }
    return;
  }
  if (event?.type === 'nativePreviewPresented' || event?.type === 'nativePreviewFailed') {
    const pending = nativePreviewPending.get(event.generation);
    if (!pending) return;
    nativePreviewPending.delete(event.generation);
    if (event.type === 'nativePreviewFailed') {
      const error = event.message || tr('Native preview unavailable');
      if (event.generation === S.seq) toast(error);
      pending.resolve({
        decodeMs: 0, uploadMs: 0, uploadedAt: performance.now(),
        presentedAt: performance.now(), presentation: 'native-failed',
        failed: true, error,
      });
      return;
    }
    const timings = event.timings || {};
    const presentedAt = performance.now();
    pending.resolve({
      decodeMs: +timings.decodeMs || 0,
      uploadMs: +timings.uploadMs || 0,
      uploadedAt: presentedAt - (+timings.gpuMs || 0),
      presentedAt,
      presentation: 'native-metal',
      nativeFetchMs: +timings.fetchMs || 0,
      nativeSharedMemory: Boolean(timings.sharedMemory),
      nativeGpuMs: +timings.gpuMs || 0,
      nativeTotalMs: +timings.totalMs || 0,
      textureCacheHit: Boolean(timings.textureCacheHit),
    });
    return;
  }
  if (event?.type !== 'sources') return;
  S.sources = Array.isArray(event.sources) ? event.sources : [];
  if (event.active) S.nativeActiveFolder = event.active;
  renderFolders();
};

function fmtFilm(id, v) {
  if (id === 'exposure_ev') return (v >= 0 ? '+' : '') + v.toFixed(1);
  if (id === 'halation_amount') return v.toFixed(1);
  if (id === 'grain_amount') {
    const stock = selectedFilmProfile()?.id || S.params?.stock;
    return (grainBaseline(stock) * v).toFixed(2) + ' µm²';
  }
  if (id === 'wb_temperature') return Math.round(v) + ' K';
  if (['halation_amount', 'print_y_filter_shift', 'print_m_filter_shift'].includes(id)) return v.toFixed(1);
  if (id === 'print_preflash') return v.toFixed(3);
  return v.toFixed(2);
}

function grainBaseline(stock) {
  return +(S.grainBaselines[stock] || 0.20);
}

function profileOption(profile) {
  const option = document.createElement('option');
  option.value = profile.id;
  option.textContent = profile.name;
  option.disabled = profile.rustOnly && !S.rustAvailable;
  return option;
}

function profileFor(id) {
  return S.profileById[id] || null;
}

function selectedFilmProfile() {
  const choice = $('stock')?.value;
  return profileFor(choice ? filmSelectionForChoice(choice, S.profiles).stock : S.params?.stock);
}

function selectedPaperProfile() {
  return profileFor($('paper')?.value || S.params?.paper);
}

function populatePaperOptions() {
  const film = selectedFilmProfile();
  const papers = S.profiles.filter((profile) =>
    profile.stage === 'printing' &&
    (!film || profile.channelModel === film.channelModel));
  const allowed = new Set(papers.map((profile) => profile.id));
  let selected = S.params.paper;
  if (!allowed.has(selected)) {
    selected = allowed.has(film?.targetPrint) ? film.targetPrint : papers[0]?.id;
    if (selected) S.params.paper = selected;
  }
  $('paper').replaceChildren(...papers.map(profileOption));
  if (selected) $('paper').value = selected;
}

function populateDevelopmentTimes() {
  const field = $('developmentTimeField');
  const profile = selectedFilmProfile();
  const times = profile?.developmentTimes || [];
  field.hidden = times.length === 0;
  $('development_time').replaceChildren(...times.map((minutes) => {
    const option = document.createElement('option');
    option.value = String(minutes);
    option.textContent = tr("{value} min", {value: Number(minutes).toLocaleString(undefined, {
      maximumFractionDigits: 2,
    })});
    return option;
  }));
  if (!times.length) {
    S.params.development_time = 0;
    return;
  }
  const current = +S.params.development_time;
  const selected = times.includes(current)
    ? current
    : (profile.defaultDevelopmentTime ?? times[0]);
  S.params.development_time = selected;
  $('development_time').value = String(selected);
}

function populatePrintDevelopmentTimes() {
  const field = $('printDevelopmentTimeField');
  const profile = selectedPaperProfile();
  const times = profile?.developmentTimes || [];
  const positive = selectedFilmProfile()?.type === 'positive';
  field.hidden = positive || times.length === 0;
  $('print_development_time').replaceChildren(...times.map((minutes) => {
    const option = document.createElement('option');
    option.value = String(minutes);
    option.textContent = tr("{value} min", {value: Number(minutes).toLocaleString(undefined, {
      maximumFractionDigits: 2,
    })});
    return option;
  }));
  if (positive || !times.length) {
    S.params.print_development_time = 0;
    return;
  }
  const current = +S.params.print_development_time;
  const selected = times.includes(current)
    ? current
    : (profile.defaultDevelopmentTime ?? times[0]);
  S.params.print_development_time = selected;
  $('print_development_time').value = String(selected);
}

function syncEngineForProfile() {
  // The prefs request usually lands before the library payload that reports
  // the Rust engine, so forcing Python here on a fresh profile persisted the
  // slow reference engine on machines that ship the GPU build. Leave the
  // selection alone until the capability is actually known.
  if (!S.engineCapabilityKnown) return;
  const rustOnly = !!selectedFilmProfile()?.rustOnly;
  const rustOption = $('engine').querySelector('[value="rs"]');
  const pythonOption = $('engine').querySelector('[value="py"]');
  rustOption.disabled = !S.rustAvailable;
  pythonOption.disabled = rustOnly;
  if (rustOnly && S.rustAvailable) $('engine').value = 'rs';
  else if (!S.rustAvailable) $('engine').value = 'py';
}

function normalizeFilmParams(raw = {}) {
  const source = raw && typeof raw === 'object' ? { ...raw } : {};
  const legacyStock = Object.prototype.hasOwnProperty.call(source, 'stock') &&
    !Object.prototype.hasOwnProperty.call(source, 'film_tuning') &&
    (source.profile_enabled !== false || source.stock !== (S.filmDefaults?.stock || 'kodak_portra_400'));
  const params = normalizeFilmTuning({ ...S.filmDefaults, ...source,
    film_tuning: source.film_tuning || (legacyStock ? 'original' : (S.filmDefaults?.film_tuning || 'lighttable')),
    film_tuning_version: source.film_tuning_version || (legacyStock ? '1' : (S.filmDefaults?.film_tuning_version || '1')) }, S.profiles);
  if (!Object.prototype.hasOwnProperty.call(source, 'grain_amount')) {
    const legacyArea = +source.grain_um2;
    params.grain_amount = Number.isFinite(legacyArea)
      ? legacyArea / grainBaseline(params.stock)
      : (S.filmDefaults.grain_amount ?? 1);
  }
  delete params.grain_um2;
  const film = (S.profiles || []).find((p) => p.id === params.stock);
  if (film) {
    const times = film.developmentTimes || [];
    if (times.length) {
      const current = +params.development_time;
      params.development_time = times.includes(current)
        ? current
        : (film.defaultDevelopmentTime ?? times[0]);
    } else if (Object.hasOwn(params, 'development_time')) {
      params.development_time = 0;
    }
    const compatiblePapers = (S.profiles || []).filter((profile) =>
      profile.stage === 'printing' && (!film.channelModel || profile.channelModel === film.channelModel));
    const allowedPaperIds = new Set(compatiblePapers.map((profile) => profile.id));
    if (params.paper && !allowedPaperIds.has(params.paper)) {
      params.paper = (allowedPaperIds.has(film.targetPrint) ? film.targetPrint : compatiblePapers[0]?.id) || params.paper;
    }
  }
  const paper = (S.profiles || []).find((p) => p.id === params.paper);
  if (film?.type === 'positive') {
    if (Object.hasOwn(params, 'print_development_time')) {
      params.print_development_time = 0;
    }
  } else if (paper) {
    const paperTimes = paper.developmentTimes || [];
    if (paperTimes.length) {
      const currentPrint = +params.print_development_time;
      params.print_development_time = paperTimes.includes(currentPrint)
        ? currentPrint
        : (paper.defaultDevelopmentTime ?? paperTimes[0]);
    } else if (Object.hasOwn(params, 'print_development_time')) {
      params.print_development_time = 0;
    }
  }
  return params;
}

function mergeFilmParams(base, overlay = {}) {
  const merged = mergeFilmTuning(base, overlay, S.profiles);
  // A legacy preset carries absolute grain_um2 but no grain_amount. Remove
  // the base multiplier so normalizeFilmParams converts the preset value.
  if (Object.prototype.hasOwnProperty.call(overlay, 'grain_um2') &&
      !Object.prototype.hasOwnProperty.call(overlay, 'grain_amount')) {
    delete merged.grain_amount;
  }
  return normalizeFilmParams(merged);
}

function syncFilmReadout(id, value) {
  const out = $(id + 'V');
  out.textContent = fmtFilm(id, value);
  if (id === 'grain_amount') {
    out.title = tr("{value}× stock baseline", {value: value.toFixed(2)});
  } else {
    out.removeAttribute('title');
  }
}

/* --------------------------------------------------------------- history */
function snapshot() {
  const image = cur();
  return JSON.stringify({
    params: S.params, grade: S.grade, crop: S.crop, preset: S.preset || null,
    cropChoices: cur()?.cropChoices,
    masks: serializableMasks(), heals: S.heals, optics: S.optics,
    status: image?.status, rating: image?.rating, label: image?.label,
    keywords: image?.keywords, versions: image?.versions,
  });
}
function filmRenderFingerprint() {
  return JSON.stringify(S.params);
}
function baseEditsFingerprint() {
  return JSON.stringify({ optics: S.optics, heals: S.heals });
}
function updateUndoRedoButtons() {
  const u = $('undoBtn');
  const r = $('redoBtn');
  if (u) u.disabled = !S.undo.length;
  if (r) r.disabled = !S.redo.length;
}
function pushUndoState(state) {
  if (S.editingName !== cur()?.name) return;
  photoUndo.push(state);
  updateUndoRedoButtons();
  scheduleNativeMenuState();
}
function pushUndo() { const state = snapshot(); pushUndoState(state); return state; }
/* Withdraw a step a cancelled gesture pushed, so Undo does not become a
 * press that visibly does nothing. Guarded: it only drops the snapshot if
 * it is still the newest one. */
function dropUndo(state) {
  if (typeof state !== 'string' || !photoUndo.dropLast(state)) return false;
  updateUndoRedoButtons();
  scheduleNativeMenuState();
  return true;
}
function restore(json, stack, persist = true) {
  const previousFilm = filmRenderFingerprint();
  const previousBaseEdits = baseEditsFingerprint();
  const st = JSON.parse(json);
  S.preset = cloneValue(st.preset || null);
  presetAmountGesture = null;
  S.params = normalizeFilmParams(st.params); S.grade = st.grade; S.crop = st.crop;
  restoreCropChoices(st.cropChoices);
  S.masks = normalizeMasks(st.masks); S.heals = normalizeHeals(st.heals);
  S.optics = normalizeOptics(st.optics); S.maskTextureDirty = true;
  S.selectedMaskId = S.masks[0]?.id || null;
  S.selectedHealId = S.heals[0]?.id || null;
  S.maskCreateOpen = !S.masks.length;
  S.maskRefineMode = selectedMask()?.type === 'brush' ? 'add' : null;
  S.healToolMode = selectedHeal()?.mode || 'remove';
  const image = cur();
  if (image) {
    for (const key of ['status', 'rating', 'label', 'keywords', 'versions']) {
      if (Object.prototype.hasOwnProperty.call(st, key)) image[key] = cloneValue(st[key]);
    }
  }
  syncControls(); syncGrade(); syncCurveFromGrade(); syncHsl();
  syncMaskPanel(); syncHealPanel(); syncOpticsPanel();
  renderKeywords(); renderVersions(); refreshLists();
  drawGrade(); applyCropVisual();
  if (persist) saveState();
  if (filmRenderFingerprint() !== previousFilm) renderFilm();
  else if (baseEditsFingerprint() !== previousBaseEdits) refreshBaseEdits();
  updateUndoRedoButtons();
  PRESET_BROWSER?.refresh();
}
function undo() {
  if (S.editingName !== cur()?.name) return;
  const previous = photoUndo.undo(snapshot());
  if (previous === null) return;
  restore(previous);
  updateUndoRedoButtons();
  toast(tr("Undo"));
  scheduleNativeMenuState();
}
function redo() {
  if (S.editingName !== cur()?.name) return;
  const next = photoUndo.redo(snapshot());
  if (next === null) return;
  restore(next);
  updateUndoRedoButtons();
  toast(tr("Redo"));
  scheduleNativeMenuState();
}

/* ------------------------------------------------------------- controls */
function syncControls() {
  const profileEnabled = S.params.profile_enabled !== false;
  $('stock').value = filmChoiceValue(S.params);
  populatePaperOptions();
  populateDevelopmentTimes();
  populatePrintDevelopmentTimes();
  $('paper').value = S.params.paper;
  FILM_SELECTS.forEach((id) => { $(id).value = S.params[id]; });
  FILM_SLIDERS.forEach((id) => {
    const value = +S.params[id];
    if (id === 'grain_amount') {
      // Old absolute edits can migrate outside the normal adjustment range.
      // Expand only as needed so loading one never silently clamps it.
      $(id).min = String(Math.min(0.1, value));
      $(id).max = String(Math.max(2, value));
    }
    syncNumericControl($(id), value);
    syncFilmReadout(id, value);
  });
  FILM_TOGGLES.forEach((id) => { $(id).checked = !!S.params[id]; });
  $('filmProfileToggle').classList.toggle('on', profileEnabled);
  $('filmProfileToggle').setAttribute('aria-checked', String(profileEnabled));
  $('filmProfileToggle').setAttribute('aria-label', (profileEnabled ? tr("Film profile on") : tr("Film profile off")));
  $('filmProfileToggle').title = (profileEnabled ? tr("Turn film profile off") : tr("Turn film profile on"));
  $('filmProfileState').textContent = profileEnabled ? tr("On") : tr("Off");
  $('filmProfileOffNote').hidden = profileEnabled;
  $('filmProfileSection').classList.toggle('profile-off', !profileEnabled);
  document.querySelector('#filmProfileSection [data-reset="film"]').hidden =
    !profileEnabled && RESET_GROUPS.film.every((key) => S.params[key] === S.filmDefaults[key]);
  const filmStages = $('filmStagesSection');
  if (profileEnabled && filmStages.classList.contains('profile-off')) {
    filmStages.open = true;
  }
  filmStages.classList.toggle('profile-off', !profileEnabled);
  document.querySelectorAll('#filmProfileControls input, #filmProfileControls select, #filmStagesSection .secbody input, #filmStagesSection .secbody select').forEach((el) => {
    el.disabled = !profileEnabled;
  });
  $('couplers_amount').disabled = !profileEnabled || !S.params.couplers_on;
  $('halation_amount').disabled = !profileEnabled || !S.params.halation_on;
  $('grain_amount').disabled = !profileEnabled || !S.params.grain_on;
  $('glare_amount').disabled = !profileEnabled || !S.params.glare_on;
  $('scan_sharpness').disabled = !profileEnabled || !S.params.scan_sharpen;
  const positive = selectedFilmProfile()?.type === 'positive';
  $('paperField').hidden = positive;
  $('paperLockRow').hidden = positive;
  $('directScanNote').hidden = !positive;
  $('printExposureRow').hidden = positive;
  $('glareRow').hidden = positive;
  $('preflashRow').hidden = positive;
  $('yellowFilterRow').hidden = positive;
  $('magentaFilterRow').hidden = positive;
  const rawInput = isRawInput();
  $('rawDevelopSection').hidden = !rawInput;
  $('rawDetailControls').hidden = !rawInput;
  $('rawSaveCameraDefault').disabled = !rawInput;
  $('rawResetCameraDefault').disabled = !rawInput || !S.rawDefault?.settings;
  $('learnedDenoiseApply').disabled = !rawInput ||
    $('learnedDenoiseApply').dataset.available !== '1';
  syncNumericControl($('learned_denoise_strength'), S.params.learned_denoise_strength ?? 0.6);
  $('learnedDenoiseStrengthV').textContent = Number(
    S.params.learned_denoise_strength ?? 0.6).toFixed(2);
  if (!rawInput) $('rawCameraDefaultStatus').textContent = tr("RAW originals only.");
  $('wbProcessedNote').hidden = rawInput;
  $('wbCustom').hidden = !rawInput || S.params.wb_mode !== 'custom';
  $('wb_mode').disabled = !rawInput;
  $('browseFilmStocks').disabled = !cur();
  $('stock').disabled = !cur();
  $('wb_temperature').disabled = !rawInput;
  $('wb_tint').disabled = !rawInput;
  $('paper').disabled = !profileEnabled || positive;
  $('development_time').disabled = !profileEnabled ||
    (selectedFilmProfile()?.developmentTimes?.length || 0) <= 1;
  $('print_development_time').disabled = !profileEnabled || positive ||
    (selectedPaperProfile()?.developmentTimes?.length || 0) <= 1;
  syncEngineForProfile();
}

function setDevelopMode(profileEnabled) {
  if (!cur() || (S.params.profile_enabled !== false) === profileEnabled) return;
  readControls();
  pushUndo();
  S.params.profile_enabled = profileEnabled;
  if (profileEnabled && S.params.stock === 'kodak_portra_400' && S.params.film_tuning === 'original') {
    Object.assign(S.params, filmSelectionForChoice('kodak_portra_400::lighttable::1', S.profiles));
  }
  syncControls();
  saveState();
  renderFilm(0);
  toast(profileEnabled ? tr("Film mode — physical film processing on") : tr("Develop mode — editing the neutral source"));
}
function readControls() {
  S.params.profile_enabled = $('filmProfileToggle').getAttribute('aria-checked') === 'true';
  Object.assign(S.params, filmSelectionForChoice($('stock').value, S.profiles));
  S.params.paper = $('paper').value;
  S.params.development_time = +$('development_time').value || 0;
  S.params.print_development_time = +$('print_development_time').value || 0;
  FILM_SELECTS.forEach((id) => { S.params[id] = $(id).value; });
  FILM_SLIDERS.forEach((id) => { S.params[id] = readNumericControl($(id), S.params[id]); });
  FILM_TOGGLES.forEach((id) => { S.params[id] = $(id).checked; });
  S.params.learned_denoise_strength = readNumericControl(
    $('learned_denoise_strength'), S.params.learned_denoise_strength ?? 0.6);
}
function syncGrade() {
  document.querySelectorAll('[data-g]').forEach((el) => {
    const k = el.dataset.g;
    el.value = S.grade[k] ?? 0;
    const out = document.querySelector(`[data-gv="${k}"]`);
    if (out) out.textContent = fmtG(el.value);
  });
  syncPointColor();
  syncColorGrading();
}

/* ------------------------------------------------------------------ view */
const photoPanMemory = createPhotoPanMemory();
let photoPanKey = null;
function rememberPhotoPan() {
  if (S.editingName !== cur()?.name) return; // Navigation may still be loading.
  photoPanMemory.remember(photoPanKey, S, $('cmp').getBoundingClientRect());
}
function restorePhotoPan() {
  if (S.viewMode !== 'detail' || S.cropping || S.cropTransition) return;
  // Establish the incoming photo's pixel scale before restoring its position.
  onViewportResize();
  photoPanMemory.restore(photoPanKey, S, $('cmp').getBoundingClientRect());
  applyViewNow();
}
function clampPan() {
  // The crop view places the photo wherever the centred frame needs it.
  if (S.cropping || S.cropTransition) return;
  if (S.zoom <= 1 && S.zoomMode === 'fit') { S.panX = 0; S.panY = 0; return; }
  const w = $('zoomwrap').getBoundingClientRect();
  const r = $('cmp').getBoundingClientRect();
  const maxX = Math.max(0, (r.width - w.width) / 2);
  const maxY = Math.max(0, (r.height - w.height) / 2);
  S.panX = clamp(S.panX, -maxX, maxX);
  S.panY = clamp(S.panY, -maxY, maxY);
}
function applyViewNow() {
  syncPreviewDetailStatus();
  const cmp = $('cmp');
  const fit = cropViewState().fit;
  if ((S.cropping || S.cropTransition) && fit) {
    // Cropping zooms the frame itself; only the pan is a transform.
    cmp.style.width = `${fit.width * S.zoom}px`;
    cmp.style.height = `${fit.height * S.zoom}px`;
    cmp.style.transform = `translate(${S.panX}px,${S.panY}px)`;
  } else {
    cmp.style.transform =
      `translate(${S.panX}px,${S.panY}px) scale(${S.zoom})`;
  }

  // Clamp against this frame's scale, not the preceding frame's smaller bounds.
  const oldPanX = S.panX, oldPanY = S.panY;
  clampPan();
  if (oldPanX !== S.panX || oldPanY !== S.panY) {
    cmp.style.transform = `translate(${S.panX}px,${S.panY}px) scale(${S.zoom})`;
  }

  const cv = $('cv');
  const naturalW = displaySourcePixelWidth();
  const rect = cv?.getBoundingClientRect();
  const displayedW = rect?.width || 0;
  const actualScale = naturalW > 0 ? (displayedW / naturalW) : null;
  const actualPct = actualScale === null ? null : Math.round(actualScale * 100);

  const isFit = S.zoomMode === 'fit';
  const is1to1 = !isFit && Math.abs(actualScale - 1.0) < 0.02;

  $('zoomVal').textContent = actualPct === null ? '—' : `${actualPct}%`;
  $('zoomVal').title = actualPct === null ? '' : isFit ? tr("Fit to window ({actualPct}%)", {actualPct: actualPct}) : is1to1 ? tr("Actual size (100%)") : tr("Zoom: {actualPct}%", {actualPct: actualPct});

  const fitBtn = $('zoomFit');
  if (fitBtn) {
    fitBtn.classList.toggle('on', isFit);
    fitBtn.setAttribute('aria-pressed', String(isFit));
  }
  const oneBtn = $('zoom1');
  if (oneBtn) {
    oneBtn.disabled = naturalW <= 0;
    oneBtn.classList.toggle('on', is1to1);
    oneBtn.setAttribute('aria-pressed', String(is1to1));
  }

  const isZoomed = !isFit && S.zoom > 1.01;
  cmp.classList.toggle('is-zoomed', isZoomed);
  syncCompareView();
  syncViewerChrome();
  drawEditOverlayNow();

  scheduleNativeViewportLayout();
  scheduleViewportRegionRender();
}
const viewFrameScheduler = createFrameScheduler(() => applyViewNow());
function applyView() {
  markContinuousInput();
  viewFrameScheduler.request({ view: true });
  scheduleAutomaticPreview();
}
function zoomView() {
  return {zoom: S.zoom, panX: S.panX, panY: S.panY};
}
function requestZoomDetail() {
  applyView();
  const cv = $('cv');
  if (!viewportRegionEnabled() && S.zoomMode === '100' &&
      sourceLongEdge() > Math.max(cv.width, cv.height) + 1) {
    doRender(performance.now(), {width: requestedPreviewWidth(), phase: 'settled',
      background: S.presentedPhotoName === cur()?.name && S.renderState === 'ready'});
  }
}
const zoomMotion = createZoomMotion({
  reducedMotion: () => !smoothZoomEnabled(APP_PREFS),
  paint(view) {
    Object.assign(S, view);
    markContinuousInput();
    applyViewNow();
  },
  settled: requestZoomDetail,
});
function stopZoomMotion({finish = false} = {}) {
  const target = zoomMotion.target;
  zoomMotion.cancel();
  if (!target) return;
  if (finish) {
    Object.assign(S, target);
    applyViewNow();
  } else {
    const sourceWidth = displaySourcePixelWidth();
    S.targetPixelScale = sourceWidth > 0 ? $('cv').getBoundingClientRect().width / sourceWidth : null;
    S.zoomMode = 'custom';
  }
}
function presentZoomChange(from, animate) {
  if (animate && S.viewMode === 'detail' && !S.cropping && !S.cropTransition &&
      S.presentedPhotoName === cur()?.name && !document.hidden) {
    clearTimeout(automaticPreviewTimer);
    clearTimeout(viewportRegionTimer);
    zoomMotion.start(from, zoomView());
  } else requestZoomDetail();
}
function zoomAt(factor, sx, sy, { actual = false, animate = false } = {}) {
  if (animate && !actual && zoomMotion.target) factor *= zoomMotion.target.zoom / S.zoom;
  zoomMotion.cancel();
  const from = zoomView();
  const currentZoom = S.zoom;
  const r = $('cmp').getBoundingClientRect();
  const actualZoom = r.width > 0 ? displaySourcePixelWidth() * currentZoom / r.width : 0;
  const next = actual ? currentZoom * factor : clamp(currentZoom * factor,
    Math.min(1, actualZoom || 1), Math.max(32, currentZoom));
  if (!(next > 0 && Number.isFinite(next))) return;
  if (!actual && Math.abs(next - 1) < 0.001) {
    zoomReset({animate});
    return;
  }
  const c0x = r.left + r.width / 2 - S.panX;
  const c0y = r.top + r.height / 2 - S.panY;
  const dx = sx - c0x, dy = sy - c0y, k = next / currentZoom;
  S.panX = dx - k * (dx - S.panX);
  S.panY = dy - k * (dy - S.panY);
  S.zoom = next;

  const cv = $('cv');
  if (cv && cv.width && r.width) {
    // `r` was measured before the new transform, so remove the old zoom to
    // recover the CSS fit width. Using the new ratio here made a visually
    // correct 1:1 click remember the old Fit percentage; the next window or
    // panel resize then jumped back to that percentage.
    const baseW = currentZoom > 0 ? r.width / currentZoom : r.width;
    const nextDisplayedWidth = baseW * next;
    const sourceWidth = displaySourcePixelWidth();
    const nextActualScale = sourceWidth > 0 ? nextDisplayedWidth / sourceWidth : null;
    if (nextActualScale !== null && Math.abs(nextActualScale - 1.0) < 0.02) {
      S.zoomMode = '100';
      S.targetPixelScale = 1;
    } else {
      S.zoomMode = 'custom';
      S.targetPixelScale = nextActualScale;
    }
  }
  presentZoomChange(from, animate);
}
function zoomCentre(f) {
  const w = $('zoomwrap').getBoundingClientRect();
  zoomAt(f, w.left + w.width / 2, w.top + w.height / 2, {animate: true});
}
function zoomReset({animate = false} = {}) {
  zoomMotion.cancel();
  rememberPhotoPan();
  const from = zoomView();
  if (S.cropping && !S.cropTransition) {
    // Fit means the cropping view itself while the crop tool is open.
    const target = cropViewTarget(S.crop);
    if (target) { applyCropView(target, { immediate: true }); return; }
  }
  S.zoomMode = 'fit';
  S.zoom = 1;
  S.panX = 0;
  S.panY = 0;
  S.targetPixelScale = null;
  presentZoomChange(from, animate);
}

function toggleActualZoomAt(x, y) {
  const cv = $('cv');
  const rect = cv?.getBoundingClientRect();
  const sourceWidth = displaySourcePixelWidth();
  if (!sourceWidth || !rect?.width) return;
  if (S.zoomMode === '100' || Math.abs(rect.width - sourceWidth) < 2) {
    zoomReset({animate: true});
    return;
  }
  S.zoomMode = '100';
  S.targetPixelScale = 1;
  zoomAt(sourceWidth / rect.width, x, y, { actual: true, animate: true });
  S.zoomMode = '100';
  S.targetPixelScale = 1;
}

function toggleActualZoom() {
  const wrap = $('zoomwrap').getBoundingClientRect();
  toggleActualZoomAt(wrap.left + wrap.width / 2, wrap.top + wrap.height / 2);
}

function sourceLongEdge(image = cur()) {
  const measured = Math.max(+(image?.width || 0), +(image?.height || 0));
  const fallback = +$('pw')?.value || INTERACTIVE_PREVIEW_WIDTH;
  return clamp(Math.round(measured || fallback), 64, 8000);
}

function displaySourcePixelWidth() {
  // A preview texture is useful for layout, but is never evidence of the
  // original's pixel count (including the canvas's initial 300px width).
  if (cur()?.width > 0 && cur()?.height > 0) return +cropSourceSize().width;
  const decoded = S.viewportSourceGeometry?.key === viewportSourceGeometryKey()
    ? S.viewportSourceGeometry : null;
  return decoded?.width || 0;
}

function viewportRegionEnabled() {
  return nativePreviewActive() && $('engine').value === 'rs' &&
    S.zoomMode === '100' && S.params.profile_enabled !== false &&
    S.presentedPhotoName === cur()?.name && !S.crop && !S.cropSession &&
    !S.compareActive && !S.holdBefore && !S.wbPick && !S.pointColorPick && !S.maskColorPick &&
    !S.reference?.active && !(S.heals || []).length && !(S.masks || []).length &&
    !Object.keys(OPTICS_DEFAULTS).some((key) =>
      (S.optics?.[key] ?? OPTICS_DEFAULTS[key]) !== OPTICS_DEFAULTS[key]);
}

function viewportPixelWindow(canvas, clip, width, height, margin = 96) {
  if (!(canvas.width > 0 && canvas.height > 0 && width > 0 && height > 0)) return null;
  const left = Math.max(canvas.left, clip.left), top = Math.max(canvas.top, clip.top);
  const right = Math.min(canvas.right, clip.right), bottom = Math.min(canvas.bottom, clip.bottom);
  if (right <= left || bottom <= top) return null;
  const x = Math.max(0, Math.floor(((left - canvas.left) / canvas.width * width - margin) / 64) * 64);
  const y = Math.max(0, Math.floor(((top - canvas.top) / canvas.height * height - margin) / 64) * 64);
  const endX = Math.min(width, Math.ceil(((right - canvas.left) / canvas.width * width + margin) / 64) * 64);
  const endY = Math.min(height, Math.ceil(((bottom - canvas.top) / canvas.height * height + margin) / 64) * 64);
  return { x, y, width: endX - x, height: endY - y };
}

function viewportSourceGeometryKey() {
  return JSON.stringify([cur()?.name, cur()?.recoverySourceKey || null, cur()?.fileKey || null, cur()?.mtime || null,
    Math.abs(Math.round((+S.params?.rotate || 0) / 90)) % 2]);
}

function requestedViewportRegion() {
  if (!viewportRegionEnabled()) return null;
  const decoded = S.viewportSourceGeometry?.key === viewportSourceGeometryKey()
    ? S.viewportSourceGeometry : null;
  let width = decoded?.width || +cur()?.width || 0;
  let height = decoded?.height || +cur()?.height || 0;
  if (!(width > 0 && height > 0)) return null;
  if (!decoded && Math.abs(Math.round((+S.params?.rotate || 0) / 90)) % 2) [width, height] = [height, width];
  return viewportPixelWindow($('cv').getBoundingClientRect(),
    $('zoomwrap').getBoundingClientRect(), width, height);
}

let viewportRegionTimer = null;
let lastViewportRenderKey = null;
function scheduleViewportRegionRender() {
  if (zoomMotion.active) return;
  const region = requestedViewportRegion();
  if (!region && !S.nativeViewport) return;
  const key = JSON.stringify([cur()?.name, region]);
  if (key === lastViewportRenderKey) return;
  clearTimeout(viewportRegionTimer);
  viewportRegionTimer = setTimeout(() => {
    doRender(performance.now(), { width: requestedPreviewWidth(), phase: 'settled',
      background: S.presentedPhotoName === cur()?.name && S.renderState === 'ready' });
  }, 45);
}

function requestedPreviewWidth(image = cur(), params = S.params, crop = previewCrop()) {
  if ($('pw').value === 'auto') {
    // Catalog dimensions already include EXIF orientation. Do not borrow the
    // outgoing canvas's orientation while the next photo is still loading.
    const source = { width: +image?.width || 0,
      height: +image?.height || 0 };
    if (Math.abs(Math.round((+params?.rotate || 0) / 90)) % 2) {
      [source.width, source.height] = [source.height, source.width];
    }
    const viewport = $('zoomwrap');
    let zoom = S.cropping || S.cropTransition ? 1 : S.zoom;
    if (image !== cur() && S.zoomMode === 'custom' && S.targetPixelScale > 0 &&
        source.width > 0 && source.height > 0) {
      const fit = Math.min(viewport.clientWidth / (crop?.w || 1) / source.width,
        viewport.clientHeight / (crop?.h || 1) / source.height);
      if (fit > 0) zoom = S.targetPixelScale / fit;
    }
    return automaticPreviewWidth({ sourceWidth: +source.width, sourceHeight: +source.height,
      viewportWidth: viewport.clientWidth, viewportHeight: viewport.clientHeight,
      deviceScale: window.devicePixelRatio || 1,
      // The cropping view's zoom is presentation only; re-rendering for it
      // would swap textures under a drag.
      zoom, crop,
      actualSize: S.zoomMode === '100' });
  }
  const selected = +$('pw').value || INTERACTIVE_PREVIEW_WIDTH;
  return S.zoomMode === '100'
    ? Math.max(selected, sourceLongEdge(image))
    : selected;
}

/* ---------------------------------------------------------------- scopes */
let scopeMode = 'histogram';

function drawScopeGrid(ctx, width, height, divisions = 4) {
  ctx.strokeStyle = 'rgba(255,255,255,.08)';
  ctx.lineWidth = 1;
  for (let i = 1; i < divisions; i++) {
    const y = Math.round(i * height / divisions) + 0.5;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
}

function drawDensity(ctx, density, width, height, color) {
  let maximum = 1;
  for (const value of density) maximum = Math.max(maximum, value);
  const pixels = ctx.getImageData(0, 0, width, height);
  for (let i = 0; i < density.length; i++) {
    if (!density[i]) continue;
    const alpha = Math.min(235, 26 + Math.round(209 * Math.sqrt(density[i] / maximum)));
    const mix = alpha / 255;
    pixels.data[i * 4] = Math.min(255, pixels.data[i * 4] + color[0] * mix);
    pixels.data[i * 4 + 1] = Math.min(255, pixels.data[i * 4 + 1] + color[1] * mix);
    pixels.data[i * 4 + 2] = Math.min(255, pixels.data[i * 4 + 2] + color[2] * mix);
    pixels.data[i * 4 + 3] = Math.max(pixels.data[i * 4 + 3], alpha);
  }
  ctx.putImageData(pixels, 0, 0);
}

function drawHistogramScope(ctx, cv, sample) {
  const bins = [new Uint32Array(64), new Uint32Array(64), new Uint32Array(64)];
  const stride = 4 * 7;
  for (let i = 0; i < sample.px.length; i += stride) {
    bins[0][sample.px[i] >> 2]++;
    bins[1][sample.px[i + 1] >> 2]++;
    bins[2][sample.px[i + 2] >> 2]++;
  }
  let mx = 1;
  for (const b of bins) for (const v of b) mx = Math.max(mx, v);
  const colors = ['#f87171', '#4ade80', '#6b8ff0'];
  ctx.globalCompositeOperation = 'lighter';
  bins.forEach((binsForChannel, channel) => {
    ctx.beginPath(); ctx.moveTo(0, cv.height);
    for (let i = 0; i < 64; i++) {
      ctx.lineTo(i / 63 * cv.width,
        cv.height - Math.sqrt(binsForChannel[i] / mx) * (cv.height - 3));
    }
    ctx.lineTo(cv.width, cv.height); ctx.closePath();
    ctx.fillStyle = `${colors[channel]}66`; ctx.fill();
  });
  ctx.globalCompositeOperation = 'source-over';
  if (!S.clip) return;
  let low = 0, high = 0, count = 0;
  for (let i = 0; i < sample.px.length; i += stride) {
    const r = sample.px[i], g = sample.px[i + 1], b = sample.px[i + 2];
    if (r >= 254 || g >= 254 || b >= 254) high++;
    if (r <= 1 && g <= 1 && b <= 1) low++;
    count++;
  }
  ctx.font = '9px "Space Mono", monospace';
  for (const [side, value, color] of [['low', low, '#6b8ff0'], ['high', high, '#f87171']]) {
    const percentage = 100 * value / Math.max(count, 1);
    if (percentage <= 0.01) continue;
    ctx.fillStyle = color;
    const x = side === 'low' ? 0 : cv.width - 4;
    ctx.fillRect(x, 0, 4, cv.height);
    const textValue = `${percentage.toFixed(1)}%`;
    ctx.fillText(textValue, side === 'low' ? 6 : cv.width - 8 - ctx.measureText(textValue).width, 10);
  }
}

function drawWaveformScope(ctx, cv, sample, parade = false) {
  drawScopeGrid(ctx, cv.width, cv.height);
  const channels = parade ? 3 : 1;
  const colors = parade ? [[248, 113, 113], [74, 222, 128], [107, 143, 240]] : [[218, 226, 236]];
  const sectionWidth = Math.floor(cv.width / channels);
  for (let channel = 0; channel < channels; channel++) {
    const density = new Uint16Array(cv.width * cv.height);
    for (let y = 0; y < sample.h; y++) for (let x = 0; x < sample.w; x++) {
      const offset = (y * sample.w + x) * 4;
      const value = parade ? sample.px[offset + channel]
        : sample.px[offset] * 0.2126 + sample.px[offset + 1] * 0.7152 + sample.px[offset + 2] * 0.0722;
      const px = parade
        ? channel * sectionWidth + Math.min(sectionWidth - 1, Math.floor(x / Math.max(sample.w - 1, 1) * (sectionWidth - 1)))
        : Math.min(cv.width - 1, Math.floor(x / Math.max(sample.w - 1, 1) * (cv.width - 1)));
      const py = Math.min(cv.height - 1, Math.max(0,
        cv.height - 1 - Math.round(value / 255 * (cv.height - 1))));
      density[py * cv.width + px]++;
    }
    drawDensity(ctx, density, cv.width, cv.height, colors[channel]);
    if (parade && channel) {
      ctx.strokeStyle = 'rgba(255,255,255,.12)';
      ctx.beginPath(); ctx.moveTo(channel * sectionWidth + 0.5, 0);
      ctx.lineTo(channel * sectionWidth + 0.5, cv.height); ctx.stroke();
    }
  }
}

function drawVectorscope(ctx, cv, sample) {
  const cx = cv.width / 2, cy = cv.height / 2;
  ctx.strokeStyle = 'rgba(255,255,255,.11)';
  for (const radius of [0.25, 0.46]) {
    ctx.beginPath(); ctx.arc(cx, cy, Math.min(cv.width, cv.height) * radius, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, cv.height);
  ctx.moveTo(0, cy); ctx.lineTo(cv.width, cy); ctx.stroke();
  const density = new Uint16Array(cv.width * cv.height);
  for (let i = 0; i < sample.px.length; i += 4) {
    const r = sample.px[i] / 255, g = sample.px[i + 1] / 255, b = sample.px[i + 2] / 255;
    const y = r * 0.2126 + g * 0.7152 + b * 0.0722;
    const cb = (b - y) / 1.8556, cr = (r - y) / 1.5748;
    const x = Math.round(cx + cb * cv.width * 0.88);
    const py = Math.round(cy - cr * cv.height * 0.88);
    if (x >= 0 && x < cv.width && py >= 0 && py < cv.height) density[py * cv.width + x]++;
  }
  drawDensity(ctx, density, cv.width, cv.height, [126, 236, 206]);
  ctx.fillStyle = 'rgba(255,255,255,.48)'; ctx.font = '8px "Space Mono", monospace';
  ctx.fillText(tr("Cb"), cv.width - 15, cy - 3); ctx.fillText(tr("Cr"), cx + 4, 9);
}

function drawHistogram() {
  const cv = $('hist'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  refreshWebGLSamplingSurface();
  const sample = S.gl && S.gl.sample();
  if (!sample) return;
  if (scopeMode === 'waveform') drawWaveformScope(ctx, cv, sample);
  else if (scopeMode === 'parade') drawWaveformScope(ctx, cv, sample, true);
  else if (scopeMode === 'vectorscope') drawVectorscope(ctx, cv, sample);
  else drawHistogramScope(ctx, cv, sample);
}

document.querySelectorAll('[data-scope]').forEach((button) => {
  button.onclick = () => {
    scopeMode = button.dataset.scope;
    document.querySelectorAll('[data-scope]').forEach((candidate) =>
      candidate.setAttribute('aria-pressed', String(candidate === button)));
    $('scopeLowLabel').textContent = scopeMode === 'histogram' ? '0' : scopeMode === 'vectorscope' ? '' : tr("0 IRE");
    $('scopeHighLabel').textContent = scopeMode === 'histogram' ? '255' : scopeMode === 'vectorscope' ? '' : tr("100 IRE");
    $('scopeMenuLabel').textContent = button.textContent;
    $('scopeMenu').classList.remove('on');
    $('scopeMenu').setAttribute('aria-hidden', 'true');
    $('scopeMenuButton').setAttribute('aria-expanded', 'false');
    drawHistogram();
  };
});
$('scopeMenuButton').onclick = (event) => {
  event.stopPropagation();
  const open = !$('scopeMenu').classList.contains('on');
  $('scopeMenu').classList.toggle('on', open);
  $('scopeMenu').setAttribute('aria-hidden', String(!open));
  $('scopeMenuButton').setAttribute('aria-expanded', String(open));
};

/* Keep technical guidance at the point of need without making every
 * inspector section carry a permanent block of copy. */
for (const section of document.querySelectorAll('#editPane .sec, #filmPane .sec')) {
  const summary = section.querySelector(':scope > summary');
  if (!summary) continue;
  const hints = [...section.querySelectorAll('.hint:not([id])')]
    .filter((hint) => hint.closest('.sec') === section);
  const topicCategory = section.closest('#filmPane') ? 'Film' : 'Editing';
  const topicSpan = summary.querySelector('span');
  const topicLabel = topicSpan?.textContent.trim() || '';
  let topicSource = topicLabel;
  try {
    topicSource = Object.values(JSON.parse(topicSpan?.dataset.i18nText || '{}')).join(' ') || topicLabel;
  } catch { /* Unmarked sections retain their visible label. */ }
  const topicId = HELP_SECTION_TOPICS[topicCategory]?.[topicSource];
  if (!hints.length && !topicId) continue;
  const help = document.createElement('button');
  help.type = 'button';
  help.className = 'section-help';
  help.textContent = '?';
  help.title = ((hints.map((hint) => hint.textContent.trim()).join(' ') || tr("Read help for {topicLabel}", {topicLabel: topicLabel})));
  help.setAttribute('aria-label', tr("Help: {value}", {value: (summary.querySelector('span')?.textContent || tr("section"))}));
  help.onclick = (event) => {
    event.preventDefault(); event.stopPropagation();
    window.LightTableHelp?.open({
      article: topicId, query: topicId ? '' : topicLabel,
      category: topicId ? '' : topicCategory,
    });
  };
  hints.forEach((hint) => { hint.hidden = true; });
  summary.appendChild(help);
}

let histogramTimer = null;
const histogramFrameScheduler = createFrameScheduler(() => drawHistogram());
function scheduleHistogram(immediate = false) {
  clearTimeout(histogramTimer);
  histogramFrameScheduler.cancel();
  const isInteracting = (performance.now() - lastContinuousInputAt) < 120;
  const delay = immediate ? 0 : (isInteracting ? 140 : 55);
  histogramTimer = setTimeout(() => histogramFrameScheduler.request(), delay);
}

let packedMaskData = null;
const smoothStep = (edge0, edge1, value) => {
  const t = clamp((value - edge0) / Math.max(edge1 - edge0, 1e-6), 0, 1);
  return t * t * (3 - 2 * t);
};

/* Each entry holds a full canvas at mask-texture resolution and was only ever
 * removed when its stroke list emptied. Bounded, oldest entry out first. */
const BRUSH_RASTER_CACHE_MAX = 24;
const brushRasterCache = new Map();
function legacyBrushStrokeValues(strokes, width, height, cacheKey = '') {
  const source = strokes || [];
  if (!source.length) {
    brushRasterCache.delete(cacheKey);
    return new Uint8Array(width * height);
  }
  const minimum = Math.min(width, height);
  let entry = brushRasterCache.get(cacheKey);
  const reusable = entry && entry.width === width && entry.height === height &&
    entry.counts.length <= source.length && source.every((stroke, index) => {
      const count = entry.counts[index] || 0;
      const points = stroke.points || [];
      const settings = `${stroke.size || 0.08}|${stroke.feather ?? 0.65}|${stroke.flow ?? 1}`;
      const anchor = count ? points[count - 1] : null;
      return count <= points.length && entry.settings[index] === settings &&
        (!count || (anchor && entry.last[index]?.[0] === anchor[0] &&
          entry.last[index]?.[1] === anchor[1]));
    });
  if (!reusable) {
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    entry = { canvas, ctx: canvas.getContext('2d', { willReadFrequently: true }), width, height,
      counts: [], settings: [], last: [] };
    entry.ctx.globalCompositeOperation = 'lighten';
    brushRasterCache.set(cacheKey, entry);
    while (brushRasterCache.size > BRUSH_RASTER_CACHE_MAX) {
      const oldest = brushRasterCache.keys().next().value;
      if (oldest === cacheKey) break;
      brushRasterCache.delete(oldest);
    }
  }
  for (let index = 0; index < source.length; index++) {
    const stroke = source[index];
    const points = stroke.points || [];
    if (!points.length) continue;
    const previousCount = entry.counts[index] || 0;
    if (previousCount === points.length) continue;
    const ctx = entry.ctx;
    const diameter = Math.max(1, +(stroke.size || 0.08) * minimum);
    ctx.strokeStyle = ctx.fillStyle = `rgba(255,255,255,${clamp(+(stroke.flow ?? 1), 0.05, 1)})`;
    ctx.lineWidth = diameter;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    const blur = clamp(+(stroke.feather ?? 0.65), 0, 1) * diameter * 0.35;
    if (blur > 0.25) ctx.filter = `blur(${blur}px)`;
    if (previousCount === 0 && points.length === 1) {
      ctx.beginPath();
      ctx.arc(points[0][0] * width, points[0][1] * height, diameter / 2, 0, Math.PI * 2);
      ctx.fill();
    } else {
      ctx.beginPath();
      const start = Math.max(0, previousCount - 1);
      ctx.moveTo(points[start][0] * width, points[start][1] * height);
      points.slice(Math.max(1, previousCount)).forEach((point) =>
        ctx.lineTo(point[0] * width, point[1] * height));
      ctx.stroke();
    }
    entry.counts[index] = points.length;
    entry.settings[index] = `${stroke.size || 0.08}|${stroke.feather ?? 0.65}|${stroke.flow ?? 1}`;
    entry.last[index] = [...points.at(-1)];
  }
  entry.counts.length = source.length;
  entry.settings.length = source.length;
  entry.last.length = source.length;
  const pixels = entry.ctx.getImageData(0, 0, width, height).data;
  const combined = new Uint8Array(width * height);
  for (let index = 0; index < combined.length; index++) {
    combined[index] = pixels[index * 4 + 3];
  }
  return combined;
}

const cumulativeBrushCache = createStrokeRasterCache({
  legacyValues: (stroke, width, height) => legacyBrushStrokeValues([stroke], width, height),
  edgeValues: (stroke, width, height) => {
    const bitmap = stroke.edgeMask;
    const values = semanticBitmapValues({bitmap}, width, height);
    return bitmap.encoding === 'png' && !semanticPngCache.get(String(bitmap.data || ''))?.source
      ? null : values;
  },
});
function brushStrokeValues(strokes, width, height, cacheKey = '') {
  if (!(strokes || []).some(stroke => stroke.buildUp)) {
    return legacyBrushStrokeValues(strokes, width, height, cacheKey);
  }
  return cumulativeBrushCache.raster(strokes, width, height, cacheKey);
}

function captureBrushEdgeMask(point) {
  if (!S.baseImg?.complete || !S.baseImg.naturalWidth) return null;
  const scale = Math.min(1, 1024 / Math.max(S.baseImg.naturalWidth, S.baseImg.naturalHeight));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(S.baseImg.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(S.baseImg.naturalHeight * scale));
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  ctx.drawImage(S.baseImg, 0, 0, canvas.width, canvas.height);
  const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const values = autoMaskValues(pixels.data, canvas.width, canvas.height, point, S.brushTolerance);
  for (let i = 0; i < values.length; i++) {
    pixels.data[i * 4] = pixels.data[i * 4 + 1] = pixels.data[i * 4 + 2] = values[i];
    pixels.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(pixels, 0, 0);
  const data = canvas.toDataURL('image/png').split(',')[1];
  cacheSemanticPng(data, {source: values, width: canvas.width, height: canvas.height});
  return {width: canvas.width, height: canvas.height, encoding: 'png', data};
}

function refineMaskValues(values, mask, width, height) {
  const added = brushStrokeValues(mask.addStrokes, width, height, `${mask.id}:add`);
  const subtracted = brushStrokeValues(mask.subtractStrokes, width, height, `${mask.id}:subtract`);
  const intersected = brushStrokeValues(mask.intersectStrokes, width, height, `${mask.id}:intersect`);
  for (let i = 0; i < values.length; i++) {
    const withAdds = Math.max(values[i], added[i]);
    const withoutSubtracts = withAdds * (1 - subtracted[i] / 255);
    /* Intersect takes the smaller of the two weights, matching the component
     * path here and `_raster_mask` in edits.py. Multiplying instead made the
     * preview up to a third weaker than the exported file through a feathered
     * transition, and saving migrates these strokes into a component that does
     * use the minimum, so the same mask changed on reload. */
    values[i] = Math.round(mask.intersectStrokes?.length
      ? Math.min(withoutSubtracts, intersected[i])
      : withoutSubtracts);
  }
  return values;
}

function primaryMaskComponent(mask) {
  const base = { ...(mask.components?.[0] || {}), type: mask.type };
  for (const key of ['strokes', 'start', 'end', 'center', 'radius', 'radiusX', 'radiusY', 'angle', 'feather',
    'bitmap', 'provider', 'depthLow', 'depthHigh']) {
    if (mask[key] !== undefined) base[key] = mask[key];
  }
  base.combine = 'add';
  return base;
}

function maskComponents(mask) {
  return [primaryMaskComponent(mask), ...(mask.components || []).slice(1)];
}

function depthMaskComponent(mask) {
  if (!mask) return null;
  return mask.type === 'depth'
    ? primaryMaskComponent(mask)
    : (mask.components || []).find((component) => component.type === 'depth') || null;
}

function serializableMasks() {
  return S.masks.map((mask) => ({ ...mask, components: maskComponents(mask) }));
}

function semanticBitmapValues(component, width, height) {
  const bitmap = component.bitmap || {};
  const sourceWidth = Math.max(1, Math.round(+bitmap.width || 1));
  const sourceHeight = Math.max(1, Math.round(+bitmap.height || 1));
  if (bitmap.encoding === 'png') {
    const key = String(bitmap.data || '');
    const cached = semanticPngCache.get(key);
    if (!cached) {
      cacheSemanticPng(key, { loading: true });
      const image = new Image();
      image.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = sourceWidth; canvas.height = sourceHeight;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        context.drawImage(image, 0, 0, sourceWidth, sourceHeight);
        const rgba = context.getImageData(0, 0, sourceWidth, sourceHeight).data;
        const source = new Uint8Array(sourceWidth * sourceHeight);
        for (let i = 0; i < source.length; i++) source[i] = rgba[i * 4];
        cacheSemanticPng(key, { source, width: sourceWidth, height: sourceHeight });
        // A first render may have cached the still-loading PNG as empty.
        maskGeometryCache.clear();
        cumulativeBrushCache.clear();
        S.maskTextureDirty = true; drawGrade();
      };
      image.onerror = () => cacheSemanticPng(key, { failed: true });
      image.src = `data:image/png;base64,${key}`;
      return new Uint8Array(width * height);
    }
    if (!cached.source) return new Uint8Array(width * height);
    return resizeMaskValues(cached.source, cached.width, cached.height, width, height);
  }
  let binary;
  try {
    binary = atob(String(bitmap.data || ''));
  } catch (_) {
    binary = '';
  }
  if (binary.length !== sourceWidth * sourceHeight) return new Uint8Array(width * height);
  const source = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) source[i] = binary.charCodeAt(i);
  return resizeMaskValues(source, sourceWidth, sourceHeight, width, height);
}

const semanticPngCache = new Map();
/* Keyed by the base64 PNG itself and holding a decoded raster up to
 * 1024 x 1024, so roughly a megabyte per entry. Every refine stroke and every
 * AI mask painted in the session adds one, and nothing used to remove them.
 * Bounded like the two neighbouring caches, oldest entry out first. */
const SEMANTIC_PNG_CACHE_MAX = 48;
function cacheSemanticPng(key, value) {
  if (semanticPngCache.has(key)) semanticPngCache.delete(key);
  semanticPngCache.set(key, value);
  while (semanticPngCache.size > SEMANTIC_PNG_CACHE_MAX) {
    semanticPngCache.delete(semanticPngCache.keys().next().value);
  }
  return value;
}
function resizeMaskValues(source, sourceWidth, sourceHeight, width, height) {
  const values = new Uint8Array(width * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const sx = clamp((x + 0.5) * sourceWidth / width - 0.5, 0, sourceWidth - 1);
    const sy = clamp((y + 0.5) * sourceHeight / height - 0.5, 0, sourceHeight - 1);
    const x0 = Math.floor(sx), x1 = Math.min(x0 + 1, sourceWidth - 1);
    const y0 = Math.floor(sy), y1 = Math.min(y0 + 1, sourceHeight - 1);
    const top = source[y0 * sourceWidth + x0] * (1 - sx + x0) + source[y0 * sourceWidth + x1] * (sx - x0);
    const bottom = source[y1 * sourceWidth + x0] * (1 - sx + x0) + source[y1 * sourceWidth + x1] * (sx - x0);
    values[y * width + x] = Math.round(top * (1 - sy + y0) + bottom * (sy - y0));
  }
  return values;
}

function canvasGeometryValues(component, width, height) {
  const canvas = document.createElement('canvas');
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, width, height);
  if (component.type === 'linear') {
    /* Judge a collapsed gradient in normalized coordinates, exactly as
     * edits.py does. A one-pixel test answers differently on this canvas than
     * at export size, which showed no mask here while the export graded half
     * the frame. */
    if (Math.hypot(component.end[0] - component.start[0],
                   component.end[1] - component.start[1]) < LINEAR_MIN_SPAN) {
      return new Uint8Array(width * height);
    }
    const sx = component.start[0] * (width - 1);
    const sy = component.start[1] * (height - 1);
    const ex = component.end[0] * (width - 1);
    const ey = component.end[1] * (height - 1);
    const gradient = ctx.createLinearGradient(sx, sy, ex, ey);
    for (let index = 0; index <= 16; index++) {
      const position = index / 16;
      const value = Math.round(smoothStep(0, 1, position) * 255);
      gradient.addColorStop(position, `rgb(${value},${value},${value})`);
    }
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, width, height);
  } else {
    const minimum = Math.min(width, height);
    const cx = component.center[0] * (width - 1);
    const cy = component.center[1] * (height - 1);
    const rx = Math.max(1, (component.radiusX ?? component.radius) * minimum);
    const ry = Math.max(1, (component.radiusY ?? component.radius) * minimum);
    const angle = (component.angle || 0) * Math.PI / 180;
    const inner = clamp(1 - (component.feather ?? 0.65), 0, 1);
    const values = new Uint8Array(width * height);
    const cosine = Math.cos(angle), sine = Math.sin(angle);
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      const dx = x - cx, dy = y - cy;
      const distance = Math.hypot((dx * cosine + dy * sine) / rx, (-dx * sine + dy * cosine) / ry);
      values[y * width + x] = Math.round((inner >= 1 ? +(distance < 1) : 1 - smoothStep(inner, 1, distance)) * 255);
    }
    return values;
  }
  const pixels = ctx.getImageData(0, 0, width, height).data;
  const values = new Uint8Array(width * height);
  for (let index = 0; index < values.length; index++) {
    values[index] = pixels[index * 4];
  }
  return values;
}

function componentGeometryValues(component, width, height) {
  let values;
  if (['subject', 'sky', 'object', 'depth', 'person', 'face-skin', 'eyes', 'eyebrows',
    'lips', 'teeth', 'hair'].includes(component.type)) {
    values = semanticBitmapValues(component, width, height);
  } else if (component.type === 'brush') {
    values = brushStrokeValues(component.strokes, width, height,
      `${component.id || 'brush'}:primary`);
  } else {
    values = canvasGeometryValues(component, width, height);
  }
  if (component.type === 'depth') {
    const low = clamp(+(component.depthLow ?? 0.55), 0, 1);
    const high = clamp(+(component.depthHigh ?? 1), 0, 1);
    for (let i = 0; i < values.length; i++) {
      const value = values[i] / 255;
      const lower = low <= 0 ? 1 : smoothStep(low - 0.04, low + 0.04, value);
      const upper = high >= 1 ? 1 : 1 - smoothStep(high - 0.04, high + 0.04, value);
      values[i] = Math.round(lower * upper * 255);
    }
  }
  if (component.invert) for (let i = 0; i < values.length; i++) values[i] = 255 - values[i];
  return values;
}

const maskGeometryCache = new Map();
function maskGeometryKey(mask, width, height) {
  const geometry = {
    type: mask.type, invert: mask.invert, strokes: mask.strokes,
    start: mask.start, end: mask.end, center: mask.center,
    radius: mask.radius, radiusX: mask.radiusX, radiusY: mask.radiusY, angle: mask.angle,
    feather: mask.feather, bitmap: mask.bitmap,
    components: mask.components, addStrokes: mask.addStrokes,
    subtractStrokes: mask.subtractStrokes,
    intersectStrokes: mask.intersectStrokes,
  };
  return `${width}x${height}:${JSON.stringify(geometry)}`;
}

function maskGeometryValues(mask, width, height) {
  const key = maskGeometryKey(mask, width, height);
  const cached = maskGeometryCache.get(mask.id);
  if (cached?.key === key) return cached.values;
  const values = new Uint8Array(width * height);
  maskComponents(mask).forEach((component, componentIndex) => {
    const layer = componentGeometryValues(component, width, height);
    const combine = componentIndex ? component.combine : 'add';
    for (let i = 0; i < values.length; i++) {
      if (combine === 'subtract') values[i] = Math.round(values[i] * (1 - layer[i] / 255));
      else if (combine === 'intersect') values[i] = Math.min(values[i], layer[i]);
      else values[i] = Math.max(values[i], layer[i]);
    }
  });
  refineMaskValues(values, mask, width, height);
  if (mask.invert) for (let i = 0; i < values.length; i++) values[i] = 255 - values[i];
  maskGeometryCache.set(mask.id, { key, values });
  return values;
}

function maskTextureSize(edge = 1024) {
  const aspect = $('cv').width / $('cv').height;
  return {
    width: aspect >= 1 ? edge : Math.max(1, Math.round(edge * aspect)),
    height: aspect >= 1 ? Math.max(1, Math.round(edge / aspect)) : edge,
  };
}

function buildMaskTexture(edge = S.editGesture ? 512 : 1024) {
  if (!S.masks.length || !$('cv').width || !$('cv').height) {
    maskGeometryCache.clear();
    return new ImageData(new Uint8ClampedArray(4), 1, 1);
  }
  const { width, height } = maskTextureSize(edge);
  const tiles = Math.max(1, Math.ceil(Math.min(S.masks.length, MAX_MASKS) / 4));
  const rgba = new Uint8ClampedArray(width * height * tiles * 4);
  S.masks.slice(0, MAX_MASKS).forEach((mask, index) => {
    const tile = Math.floor(index / 4);
    const channel = index % 4;
    const values = maskGeometryValues(mask, width, height);
    const tileOffset = tile * width * height * 4;
    for (let i = 0; i < values.length; i++) {
      rgba[tileOffset + i * 4 + channel] = values[i];
    }
  });
  const activeIds = new Set(S.masks.slice(0, MAX_MASKS).map((mask) => mask.id));
  for (const id of maskGeometryCache.keys()) {
    if (!activeIds.has(id)) maskGeometryCache.delete(id);
  }
  return new ImageData(rgba, width, height * tiles);
}

function drawBrushCursor(ctx, surface, point, size, feather, accent = '#fff') {
  if (!point) return;
  const x = point[0] * surface.width, y = point[1] * surface.height;
  const outer = Math.max(3, size * Math.min(surface.width, surface.height) / 2);
  const inner = Math.max(1.5, outer * (1 - feather));
  ctx.save();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1;
  ctx.shadowColor = 'rgba(0,0,0,.9)'; ctx.shadowBlur = 2 * surface.pixelRatio;
  ctx.beginPath(); ctx.arc(x, y, outer, 0, Math.PI * 2); ctx.stroke();
  if (feather > 0.02) {
    ctx.strokeStyle = 'rgba(255,255,255,.68)';
    ctx.beginPath(); ctx.arc(x, y, inner, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.restore();
}

function syncOverlayCursorClass() {
  const cursor = editOverlayCursor(S, selectedMask(), $('cv').getBoundingClientRect());
  $('editOverlay').style.cursor = cursor;
  return cursor;
}

function syncViewerChrome() {
  const chrome = $('viewerChrome');
  const frame = $('cmp').getBoundingClientRect();
  const geometry = screenOverlayGeometry(frame, frame, $('zoomwrap').getBoundingClientRect());
  chrome.hidden = !geometry;
  if (!geometry) return;
  for (const key of ['left', 'top', 'width', 'height']) chrome.style[key] = `${geometry[key]}px`;
}

function drawEditOverlayNow() {
  const overlay = $('editOverlay');
  const canvas = $('cv');
  const active = canvas.width && canvas.height &&
    (S.activePane === 'maskPane' || S.activePane === 'healPane');
  const geometry = active ? screenOverlayGeometry(
    canvas.getBoundingClientRect(), $('cmp').getBoundingClientRect(),
    $('zoomwrap').getBoundingClientRect(), window.devicePixelRatio) : null;
  const surface = prepareScreenOverlay(overlay, geometry);
  if (!surface) { syncOverlayCursorClass(); return; }
  const { ctx } = surface;
  if (S.overlayHoverClientPoint) {
    const [clientX, clientY] = S.overlayHoverClientPoint;
    const rect = overlay.getBoundingClientRect();
    S.overlayHoverPoint = clientX >= rect.left && clientX <= rect.right &&
      clientY >= rect.top && clientY <= rect.bottom ? overlayPoint({clientX, clientY}) : null;
  }
  const cursor = syncOverlayCursorClass();
  if (S.activePane === 'maskPane') {
    const mask = selectedMask();
    if (!mask) return;
    if ($('maskShowOverlay').checked && packedMaskData) {
      const index = Math.max(0, S.masks.indexOf(mask));
      const tile = Math.floor(index / 4);
      const channel = index % 4;
      const tiles = Math.max(1, Math.ceil(Math.min(S.masks.length, MAX_MASKS) / 4));
      const tileHeight = Math.floor(packedMaskData.height / tiles);
      const tinted = document.createElement('canvas');
      tinted.width = packedMaskData.width; tinted.height = tileHeight;
      const tintCtx = tinted.getContext('2d');
      const pixels = tintCtx.createImageData(tinted.width, tinted.height);
      let sourcePixels = null;
      if (S.baseImg) {
        const sourceCanvas = document.createElement('canvas');
        sourceCanvas.width = tinted.width; sourceCanvas.height = tinted.height;
        const sourceContext = sourceCanvas.getContext('2d', { willReadFrequently: true });
        sourceContext.drawImage(S.baseImg, 0, 0, tinted.width, tinted.height);
        sourcePixels = sourceContext.getImageData(
          0, 0, tinted.width, tinted.height).data;
      }
      for (let i = 0; i < tinted.width * tinted.height; i++) {
        let rangeWeight = 1;
        if (sourcePixels) {
          const red = sourcePixels[i * 4] / 255;
          const green = sourcePixels[i * 4 + 1] / 255;
          const blue = sourcePixels[i * 4 + 2] / 255;
          const luminance = red * 0.2126 + green * 0.7152 + blue * 0.0722;
          const lower = mask.lumaLow <= 0 ? 1 : smoothStep(
            mask.lumaLow - 0.04, mask.lumaLow + 0.04, luminance);
          const upper = mask.lumaHigh >= 1 ? 1 : 1 - smoothStep(
            mask.lumaHigh - 0.04, mask.lumaHigh + 0.04, luminance);
          rangeWeight = lower * upper;
          if (mask.colorHue != null && mask.colorAmount > 0) {
            const hue = rgbHue(red, green, blue);
            const maximum = Math.max(red, green, blue);
            const saturation = maximum > 1e-5
              ? (maximum - Math.min(red, green, blue)) / maximum : 0;
            const difference = Math.abs(((hue - mask.colorHue + 540) % 360) - 180);
            const selected = (1 - smoothStep(mask.colorRange * 0.45,
              mask.colorRange, difference)) * saturation;
            rangeWeight *= 1 - mask.colorAmount * (1 - selected);
          }
        }
        const sourceOffset = ((tile * tileHeight * tinted.width) + i) * 4;
        pixels.data[i * 4] = 245; pixels.data[i * 4 + 1] = 56; pixels.data[i * 4 + 2] = 72;
        pixels.data[i * 4 + 3] = Math.round(
          packedMaskData.data[sourceOffset + channel] * rangeWeight * 0.42);
      }
      tintCtx.putImageData(pixels, 0, 0);
      ctx.drawImage(tinted, 0, 0, surface.width, surface.height);
    }
    ctx.strokeStyle = '#fff'; ctx.fillStyle = '#4b9cf5'; ctx.lineWidth = 1.5;
    if (S.localPinsVisible && !S.maskRefineMode && mask.type === 'linear') {
      const [sx, sy] = [mask.start[0] * surface.width, mask.start[1] * surface.height];
      const [ex, ey] = [mask.end[0] * surface.width, mask.end[1] * surface.height];
      ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
      for (const [x, y] of [[sx, sy], [ex, ey]]) { ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
    } else if (S.localPinsVisible && !S.maskRefineMode && mask.type === 'radial') {
      const x = mask.center[0] * surface.width, y = mask.center[1] * surface.height;
      const minimum = Math.min(surface.width, surface.height);
      ctx.beginPath(); ctx.ellipse(x, y, mask.radiusX * minimum, mask.radiusY * minimum,
        (mask.angle || 0) * Math.PI / 180, 0, Math.PI * 2); ctx.stroke();
      const handles = radialHandles(mask, surface.width, surface.height);
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, y);
      ctx.lineTo(handles.rotate[0] * surface.width, handles.rotate[1] * surface.height); ctx.stroke();
      ctx.setLineDash([]);
      for (const [u, v] of Object.values(handles)) {
        ctx.beginPath(); ctx.arc(u * surface.width, v * surface.height, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      }
    }
    if (cursor === 'none') {
      drawBrushCursor(ctx, surface, S.overlayHoverPoint, S.brushSize, S.brushFeather,
        (S.maskRefineMode === 'subtract' || S.overlayAltKey) ? '#ff9c9c' : '#fff');
    }
  } else if (S.activePane === 'healPane') {
    for (const spot of S.localPinsVisible ? S.heals : []) {
      const selected = spot.id === S.selectedHealId;
      const tx = spot.target[0] * surface.width, ty = spot.target[1] * surface.height;
      const sx = spot.source[0] * surface.width, sy = spot.source[1] * surface.height;
      const radius = spot.radius * Math.min(surface.width, surface.height);
      ctx.save();
      ctx.globalAlpha = spot.enabled === false ? 0.35 : 1;
      ctx.lineWidth = selected ? 2.2 : 1.25;
      ctx.strokeStyle = selected ? '#fff' : 'rgba(255,255,255,.62)';
      if (spot.mode !== 'remove') {
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(sx, sy); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.fillStyle = selected ? 'rgba(75,156,245,.18)' : 'rgba(160,160,160,.12)';
      ctx.beginPath(); ctx.arc(tx, ty, radius, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = selected ? '#4b9cf5' : 'rgba(255,255,255,.72)';
      ctx.beginPath(); ctx.arc(tx, ty, selected ? 4.5 : 3, 0, Math.PI * 2); ctx.fill();
      if (spot.mode !== 'remove') {
        ctx.fillStyle = 'rgba(20,20,20,.38)';
        ctx.beginPath(); ctx.arc(sx, sy, radius, 0, Math.PI * 2); ctx.fill();
        ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = selected ? '#4b9cf5' : 'rgba(255,255,255,.72)';
        ctx.beginPath(); ctx.arc(sx, sy, selected ? 4.5 : 3, 0, Math.PI * 2); ctx.fill();
      }
      ctx.restore();
    }
    if (cursor === 'none') drawBrushCursor(ctx, surface, S.overlayHoverPoint,
      S.healBrush.radius * 2, S.healBrush.feather);
  }
}

const previewFrameScheduler = createFrameScheduler((work) => {
  if (work.reference) updateReferenceCompositeNow(!work.grade);
  if (work.grade) drawGradeNow(Boolean(work.forceWebGL));
  if (work.edits && nativePreviewActive()) {
    postNative('nativeEdits', nativeEditsPayload());
  }
  if (work.visualization && nativePreviewActive()) {
    postNative('nativeSpotVisualization', spotVisualization());
  }
  if (work.overlay) drawEditOverlayNow();
});

function drawEditOverlay() {
  previewFrameScheduler.request({ overlay: true });
}

const CURVE_KEYS = ['curveL', 'curveR', 'curveG', 'curveB'];
let nativeCurveRefs = [];
function nativeGradePayload(grade) {
  const references = CURVE_KEYS.map((key) => grade?.[key]);
  const curvesChanged = references.some((value, index) =>
    value !== nativeCurveRefs[index]);
  nativeCurveRefs = references;
  const values = { ...grade };
  if (!curvesChanged) CURVE_KEYS.forEach((key) => delete values[key]);
  return { grade: values, curvesChanged, interaction: GRADE_PERF.take(),
    softProof: S.softProof };
}

function drawGradeNow(forceWebGL = false, refreshScope = true) {
  scheduleViewportRegionRender();
  if (S.renderState === 'pending' && S.presentedPhotoName && S.presentedPhotoName !== cur()?.name) return;
  if (S.previewLoadGeneration != null) return;
  const requestedGradeKey = gradeBakeKey(gradeBakeRequest(S.grade, S.masks));
  if (requestedGradeKey !== (S.presentedGradeKey ?? null)) {
    renderPhysicalPreview();
    return;
  }
  const activeGrade = S.gradeEditsBaked ? GRADE_DEFAULTS : S.grade;
  syncPreviewBackend();
  let upload;
  let channelUpload;
  const native = nativePreviewActive();
  const interactiveMask = native && S.activePane === 'maskPane' &&
    !!S.editGesture;
  if (S.maskTextureDirty || !packedMaskData) {
    const dirtyChannel = interactiveMask ? S.masks.indexOf(selectedMask()) : -1;
    if (dirtyChannel >= 0) {
      channelUpload = nativeMaskChannelPayload(dirtyChannel, 256);
    } else {
      packedMaskData = buildMaskTexture();
      upload = packedMaskData;
    }
    S.maskTextureDirty = false;
  }
  if (native) {
    postNative('nativeGrade', nativeGradePayload(activeGrade));
    // Local sliders and range/enable controls change mask settings without
    // changing the raster. Send those settings on every grade update; omit
    // bitmap data when the existing mask texture can be reused.
    postNative('nativeMasks', channelUpload || nativeMaskPayload(upload));
  }
  // The WebGL surface is hidden while Metal is presenting. Avoid duplicating
  // continuous draws, but allow an explicit one-shot refresh before sampling
  // or after a new helper texture arrives.
  if (S.gl && (!native || forceWebGL) && !interactiveMask) {
    S.gl.draw(activeGrade, S.gradeEditsBaked ? [] : S.masks, upload,
      S.softProof, spotVisualization());
    if (!native) {
      const input = GRADE_PERF.take();
      if (input) afterVisiblePaint().then(() => GRADE_PERF.presented(input, 'webgl-paint-proxy'));
    }
  }
  if (refreshScope && S.gl && !interactiveMask) scheduleHistogram();
}

function drawGrade() {
  markContinuousInput();
  GRADE_PERF.input(S.seq);
  previewFrameScheduler.request({
    grade: true,
    overlay: S.maskTextureDirty,
  });
}

function refreshWebGLSamplingSurface() {
  if (S.gl && nativePreviewActive()) drawGradeNow(true, false);
}

function nativeMaskPayload(imageData) {
  const masks = (S.gradeEditsBaked ? [] : S.masks).slice(0, MAX_MASKS).map((mask) => ({
    enabled: mask.enabled !== false,
    opacity: +mask.opacity || 0,
    lumaLow: +mask.lumaLow || 0,
    lumaHigh: mask.lumaHigh == null ? 1 : +mask.lumaHigh,
    colorHue: mask.colorHue,
    colorRange: +mask.colorRange || 30,
    colorAmount: mask.colorAmount == null ? 1 : +mask.colorAmount,
    grade: mask.grade || LOCAL_GRADE_DEFAULTS,
  }));
  if (!imageData) return { masks };
  return { width: imageData.width, height: imageData.height,
    data: bytesToBase64(imageData.data), masks };
}

function nativeMaskChannelPayload(channel, edge) {
  const masks = nativeMaskPayload().masks;
  const mask = S.masks[channel];
  if (!mask || !$('cv').width || !$('cv').height) return { masks };
  const { width, height } = maskTextureSize(edge);
  return { width, height, channel: channel % 4, tile: Math.floor(channel / 4),
    data: bytesToBase64(maskGeometryValues(mask, width, height)), masks };
}

function spotVisualization() {
  return {
    enabled: S.activePane === 'healPane' && $('healVisualize').checked,
    threshold: +$('healVisualizeThreshold').value,
    clipping: Boolean(S.clip),
  };
}

function refreshSpotVisualization() {
  previewFrameScheduler.request({ grade: true, visualization: true, overlay: true });
}

function nativeEditsPayload(baked = S.baseEditsBaked) {
  return {
    optics: baked ? OPTICS_DEFAULTS : (S.optics || OPTICS_DEFAULTS),
    heals: baked ? [] : (S.heals || []).filter((spot) => spot.enabled !== false).slice(0, 16),
  };
}

function nativeBaseRequiresBake() {
  return !!S.optics.profileEnabled || S.heals.some((spot) => spot.enabled !== false);
}

/* ---------------------------------------------------------- local tools */
function renderEditItems(kind) {
  const isMask = kind === 'mask';
  const list = isMask ? S.masks : S.heals;
  const selectedId = isMask ? S.selectedMaskId : S.selectedHealId;
  const host = $(isMask ? 'maskList' : 'healList');
  host.replaceChildren();
  if (!list.length) {
    const empty = document.createElement('div');
    empty.className = 'version-empty';
    empty.textContent = isMask ? tr("Choose a tool to create your first mask") : tr("Click or drag on the photo to make a correction");
    host.appendChild(empty);
    return;
  }
  list.forEach((item, index) => {
    const row = document.createElement('div');
    row.className = 'edit-item' + (item.id === selectedId ? ' on' : '');
    const button = document.createElement('button');
    button.className = 'edit-item-main';
    button.type = 'button';
    const preview = document.createElement('span');
    preview.className = `item-preview ${isMask ? item.type : item.mode}`;
    preview.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'item-label';
    label.textContent = isMask ? item.name : tr('{tool} {number}', {tool: localToolLabel(item.mode), number: index + 1});
    button.append(preview, label);
    button.onclick = () => {
      if (isMask) {
        S.selectedMaskId = item.id;
        S.maskRefineMode = item.type === 'brush' ? 'add' : null;
        S.maskCreateOpen = false;
      } else {
        S.selectedHealId = item.id;
        S.healToolMode = item.mode;
      }
      isMask ? syncMaskPanel() : syncHealPanel();
      drawEditOverlay();
    };
    const visibility = document.createElement('button');
    visibility.className = 'item-visibility' + (item.enabled === false ? ' off' : '');
    visibility.type = 'button';
    visibility.title = item.enabled === false ? tr("Show") : tr("Hide");
    const itemName = isMask ? item.name : tr('correction {number}', {number: index + 1});
    visibility.setAttribute('aria-label', item.enabled === false
      ? tr('Show {name}', {name: itemName}) : tr('Hide {name}', {name: itemName}));
    visibility.textContent = item.enabled === false ? '○' : '◉';
    visibility.onclick = (event) => {
      event.stopPropagation(); pushUndo(); item.enabled = item.enabled === false;
      if (isMask) { S.maskTextureDirty = true; syncMaskPanel(); drawGrade(); }
      else { syncHealPanel(); drawEditOverlay(); refreshBaseEdits(); }
      saveState();
    };
    const more = document.createElement('button');
    more.className = 'item-more'; more.type = 'button';
    if (isMask) {
      more.textContent = '•••'; more.title = tr("Rename mask"); more.setAttribute('aria-label', tr("Rename {itemName}", {itemName: item.name}));
      more.onclick = async (event) => {
        event.stopPropagation();
        const name = await askName(tr("Rename mask"), item.name);
        if (!name || name === item.name) return;
        pushUndo(); item.name = name.slice(0, 60); syncMaskPanel(); saveState();
      };
    } else {
      more.textContent = '×'; more.title = tr("Delete correction"); more.setAttribute('aria-label', tr("Delete correction {value}", {value: index + 1}));
      more.onclick = (event) => { event.stopPropagation(); deleteHeal(item.id); };
    }
    row.append(button, visibility, more);
    host.appendChild(row);
  });
}

function syncMaskPanel() {
  renderEditItems('mask');
  MASK_CURVE?.sync();
  const mask = selectedMask();
  const createOpen = S.maskCreateOpen;
  $('maskReset').disabled = !photoReadyForEditing() || !S.masks.length;
  $('maskCreateMenu').hidden = !createOpen;
  $('maskCreateToggle').setAttribute('aria-expanded', String(createOpen));
  $('maskSemanticCombineRow').hidden = !mask;
  document.querySelectorAll('.mask-tool-grid button').forEach((button) => {
    const smart = ['maskAddSubject', 'maskAddSky', 'maskAddObject', 'maskAddDepth',
      'maskAddPeople'].includes(button.id);
    const combining = smart && $('maskSemanticCombine').value !== 'new' && !!mask;
    button.disabled = S.masks.length >= MAX_MASKS && !combining;
  });
  $('maskControls').hidden = !mask;
  if (!mask) { syncOverlayCursorClass(); return; }
  $('maskSelectedName').textContent = mask.name;
  $('maskVisible').checked = mask.enabled !== false;
  $('maskInvert').checked = !!mask.invert;
  $('maskOpacity').value = mask.opacity;
  $('maskOpacityV').textContent = `${Math.round(mask.opacity * 100)}%`;
  $('maskLumaLow').value = mask.lumaLow;
  $('maskLumaHigh').value = mask.lumaHigh;
  $('maskLumaLowV').textContent = `${Math.round(mask.lumaLow * 100)}%`;
  $('maskLumaHighV').textContent = `${Math.round(mask.lumaHigh * 100)}%`;
  const depth = depthMaskComponent(mask);
  $('maskDepthControls').hidden = !depth;
  if (depth) {
    $('maskDepthLow').value = depth.depthLow;
    $('maskDepthHigh').value = depth.depthHigh;
    $('maskDepthLowV').textContent = `${Math.round(depth.depthLow * 100)}%`;
    $('maskDepthHighV').textContent = `${Math.round(depth.depthHigh * 100)}%`;
  }
  $('maskColorHue').value = mask.colorHue == null ? 0 : mask.colorHue;
  $('maskColorRange').value = mask.colorRange;
  $('maskColorAmount').value = mask.colorAmount;
  $('maskColorEnabled').checked = mask.colorHue != null;
  for (const id of ['maskColorHue', 'maskColorRange', 'maskColorAmount']) {
    $(id).disabled = mask.colorHue == null;
  }
  $('maskColorHueV').textContent = mask.colorHue == null ? '—' : `${Math.round(mask.colorHue)}°`;
  $('maskColorRangeV').textContent = `${Math.round(mask.colorRange)}°`;
  $('maskColorAmountV').textContent = `${Math.round(mask.colorAmount * 100)}%`;
  $('maskBrushSize').value = S.brushSize;
  $('maskBrushFeather').value = S.brushFeather;
  $('maskBrushFlow').value = S.brushFlow;
  $('maskBrushDensity').value = S.brushDensity;
  $('maskBrushAutoMask').checked = S.brushAutoMask;
  $('maskBrushTolerance').value = S.brushTolerance;
  $('maskBrushSizeV').textContent = `${Math.round(S.brushSize * 100)}%`;
  $('maskBrushFeatherV').textContent = `${Math.round(S.brushFeather * 100)}%`;
  $('maskBrushFlowV').textContent = `${Math.round(S.brushFlow * 100)}%`;
  $('maskBrushDensityV').textContent = `${Math.round(S.brushDensity * 100)}%`;
  $('maskBrushToleranceV').textContent = `${Math.round(S.brushTolerance * 100)}%`;
  const brushing = mask.type === 'brush' || !!S.maskRefineMode;
  document.querySelectorAll('.mask-brush-control').forEach((row) => { row.hidden = !brushing; });
  $('maskBrushToleranceRow').hidden = !brushing || !S.brushAutoMask;
  $('maskShapeControls').hidden = mask.type !== 'radial';
  if (mask.type === 'radial') for (const [id, key] of [
    ['maskRadiusX', 'radiusX'], ['maskRadiusY', 'radiusY'], ['maskAngle', 'angle'], ['maskShapeFeather', 'feather']]) {
    $(id).value = mask[key];
    $(id + 'V').textContent = key === 'angle' ? `${Math.round(mask[key])}°` : `${Math.round(mask[key] * 100)}%`;
  }
  $('maskRefineAdd').setAttribute('aria-pressed', String(S.maskRefineMode === 'add'));
  $('maskRefineSubtract').setAttribute('aria-pressed', String(S.maskRefineMode === 'subtract'));
  $('maskRefineIntersect').setAttribute('aria-pressed', String(S.maskRefineMode === 'intersect'));
  $('maskEditShape').hidden = mask.type === 'brush' ||
    !['brush', 'linear', 'radial'].includes(mask.type);
  $('maskEditShape').setAttribute('aria-pressed', String(!S.maskRefineMode));
  document.querySelectorAll('[data-local]').forEach((input) => {
    const key = input.dataset.local;
    input.value = mask.grade[key] ?? 0;
    document.querySelector(`[data-localv="${key}"]`).textContent = fmtG(mask.grade[key] ?? 0);
  });
  if (S.maskRefineMode === 'subtract') $('maskInstruction').textContent = tr("Paint over areas to subtract from this mask.");
  else if (S.maskRefineMode === 'intersect') $('maskInstruction').textContent = tr("Paint the only area this mask should retain.");
  else if (S.maskRefineMode === 'add') $('maskInstruction').textContent = tr("Paint over areas to add to this mask. Hold Option to subtract.");
  else if (mask.type === 'linear') $('maskInstruction').textContent = tr('Drag either dot to adjust the gradient. Drag the line to move it. Drag elsewhere to redraw it.');
  else if (!['brush', 'linear', 'radial'].includes(mask.type)) {
    $('maskInstruction').textContent = tr('{tool} selected on device. Use Add, Subtract, or Intersect to refine it.', {tool: localToolLabel(mask.type)});
  } else $('maskInstruction').textContent = tr('Drag on the photo to edit the {tool} mask.', {tool: localToolLabel(mask.type)});
  syncOverlayCursorClass();
}

function syncHealPanel() {
  renderEditItems('heal');
  $('healReset').disabled = !photoReadyForEditing() || !S.heals.length;
  const spot = selectedHeal();
  for (const mode of ['remove', 'heal', 'clone']) {
    $(`healTool${mode[0].toUpperCase()}${mode.slice(1)}`).setAttribute(
      'aria-pressed', String(S.healToolMode === mode));
  }
  $('healControls').hidden = !spot;
  if (spot) Object.assign(S.healBrush, { radius: spot.radius, feather: spot.feather, opacity: spot.opacity });
  const brush = spot || S.healBrush;
  for (const [id, key] of [['healRadius', 'radius'], ['healFeather', 'feather'], ['healOpacity', 'opacity']]) {
    $(id).value = brush[key];
    $(id + 'V').textContent = `${Math.round(brush[key] * 100)}%`;
  }
  if (!spot) {
    $('healInstruction').textContent = tr("Click or drag over a distraction. Heal and Clone choose a source automatically.");
    syncOverlayCursorClass(); return;
  }
  $('healSelectedName').textContent = tr('{tool} Correction', {tool: localToolLabel(spot.mode)});
  $('healVisible').checked = spot.enabled !== false;
  $('healRadius').value = spot.radius;
  $('healFeather').value = spot.feather;
  $('healOpacity').value = spot.opacity;
  $('healRadiusV').textContent = `${Math.round(spot.radius * 100)}%`;
  $('healFeatherV').textContent = `${Math.round(spot.feather * 100)}%`;
  $('healOpacityV').textContent = `${Math.round(spot.opacity * 100)}%`;
  $('healRefresh').hidden = spot.mode === 'remove';
  $('healSourceHint').hidden = spot.mode === 'remove';
  if (!S.editGesture) $('healInstruction').textContent = spot.mode === 'remove' ? tr("Click or drag to add another removal. Drag a circle to reposition it.") : tr("Click or drag to add another correction. Drag the target or source circle to refine it.");
  syncOverlayCursorClass();
}

function syncOpticsPanel() {
  S.optics = normalizeOptics(S.optics);
  syncCropPanel();
  const match = _lensProfileCache.get(cur()?.name);
  const override = S.optics.profileOverride;
  const overrideKey = override ? JSON.stringify(override) : '';
  if (match) {
    S.lensProfile = override ? (match.candidates || []).find((profile) =>
      ['cameraMaker', 'cameraModel', 'lensMaker', 'lensModel'].every((key) => profile[key] === override[key])) || null : match.profile;
    const select = $('lensProfileOverride');
    select.replaceChildren(new Option(tr("Automatic from photo metadata"), ''));
    for (const candidate of match.candidates || []) {
      const identity = Object.fromEntries(['cameraMaker', 'cameraModel', 'lensMaker', 'lensModel'].map((key) => [key, candidate[key]]));
      select.add(new Option(`${candidate.lensMaker} ${candidate.lensModel}`, JSON.stringify(identity)));
    }
    if (override && !S.lensProfile) select.add(new Option(tr("Saved profile unavailable"), overrideKey));
    select.value = overrideKey;
    select.disabled = !(match.candidates || []).length && !override;
  }
  const profile = S.lensProfile;
  $('lensProfileCard').classList.toggle('matched', !!profile);
  $('lensProfileCard').classList.toggle('unavailable', !profile);
  $('lensProfileName').textContent = profile ? `${profile.lensMaker || ''} ${profile.lensModel || 'Matched lens'}`.trim() : tr("No exact lens profile found");
  $('lensProfileDetail').textContent = profile ? `${profile.cameraMaker || ''} ${profile.cameraModel || ''} · ${profile.focal || '—'} mm${profile.aliasedCamera ? ' · compatible camera profile' : ''}`.trim() : (match?.reason || tr("Manual distortion and perspective controls remain available."));
  $('lensProfileReason').textContent = override ? profile ? tr("Profile selected manually. Verify the correction against the original.") : tr("Saved profile is unavailable for this photo; choose another profile.") : (match?.reason || '');
  $('lensProfileEnabled').checked = !!S.optics.profileEnabled;
  $('lensProfileEnabled').disabled = !profile;
  $('lensProfileDistortion').checked = !!S.optics.profileDistortion;
  $('lensProfileVignette').checked = !!S.optics.profileVignette;
  $('lensFlipHorizontal').setAttribute('aria-pressed', String(!!S.optics.flipHorizontal));
  $('lensFlipVertical').setAttribute('aria-pressed', String(!!S.optics.flipVertical));
  $('lensProfileDistortion').disabled = !profile || !S.optics.profileEnabled || !profile.hasDistortion;
  $('lensProfileVignette').disabled = !profile || !S.optics.profileEnabled || !profile.hasVignette;
  document.querySelectorAll('[data-optics]').forEach((input) => {
    const key = input.dataset.optics;
    input.value = S.optics[key];
    document.querySelector(`[data-opticsv="${key}"]`).textContent = (+S.optics[key]).toFixed(2);
  });
}

function addMask(type) {
  if (!cur()) return;
  if (S.masks.length >= MAX_MASKS) return toast(tr("Up to sixteen local masks can be active"));
  pushUndo();
  const number = S.masks.length + 1;
  const mask = {
    id: editId('mask'), name: tr('Mask {number}', {number}),
    type, enabled: true, invert: false, opacity: 1, lumaLow: 0, lumaHigh: 1,
    colorHue: null, colorRange: 30, colorAmount: 1,
    grade: { ...LOCAL_GRADE_DEFAULTS }, addStrokes: [], subtractStrokes: [],
    intersectStrokes: [],
  };
  if (type === 'brush') mask.strokes = [];
  else if (type === 'linear') { mask.start = [0.25, 0.5]; mask.end = [0.75, 0.5]; }
  else { mask.center = [0.5, 0.5]; mask.radius = 0.25; mask.feather = 0.65; }
  const normalized = normalizeMasks([mask])[0];
  S.masks.push(normalized); S.selectedMaskId = normalized.id; S.maskTextureDirty = true;
  S.maskCreateOpen = false; S.maskRefineMode = type === 'brush' ? 'add' : null;
  syncMaskPanel(); drawGrade(); saveState();
}

$('maskCreateToggle').onclick = () => {
  S.maskCreateOpen = !S.maskCreateOpen;
  syncMaskPanel();
};
$('maskAddBrush').onclick = () => addMask('brush');
$('maskAddLinear').onclick = () => addMask('linear');
$('maskAddRadial').onclick = () => addMask('radial');
async function createSemanticMask(kind, point = null) {
  if (!cur()) return;
  const combine = $('maskSemanticCombine').value;
  const target = combine === 'new' ? null : selectedMask();
  if (!target && S.masks.length >= MAX_MASKS) return toast(tr("Up to sixteen local masks can be active"));
  if (target && maskComponents(target).length >= MAX_MASK_COMPONENTS) {
    return toast(tr("This mask has reached its component limit"));
  }
  /* On-device segmentation takes seconds and its answer carries no photo
   * identity, so without this the mask can be cut from one photograph and
   * saved onto whichever one is open when the model returns. */
  const requestedName = cur().name;
  const stillHere = () => cur()?.name === requestedName;
  $('maskInstruction').textContent = tr('Selecting {tool} on device…', {tool: localToolLabel(kind)});
  let result;
  try {
    result = await api('/api/mask/semantic', {
      name: requestedName, kind, point, params: S.params,
    });
  } catch (error) {
    // Leaving the panel on "Selecting…" forever gives the user nothing to act
    // on, so report the failure even though the request never answered.
    const message = tr("That selection could not be completed. Try again.");
    if (stillHere()) $('maskInstruction').textContent = message;
    return toast(message);
  }
  if (!stillHere()) return;
  if (result.error) {
    $('maskInstruction').textContent = result.error;
    return toast(result.error);
  }
  if (Number.isFinite(+result.faces) && result.faces > 0) {
    $('peopleFaceCount').textContent = trn("{count} face found", "{count} faces found", result.faces, {resultFaces: result.faces});
  }
  pushUndo();
  const component = {
    id: editId('component'), type: kind,
    combine: target ? combine : 'add', invert: false,
    bitmap: result.bitmap, provider: result.provider,
  };
  if (kind === 'depth') {
    component.depthLow = 0.55;
    component.depthHigh = 1;
  }
  let createdMask = target;
  if (target) {
    target.components = maskComponents(target);
    target.components.push(component);
    S.selectedMaskId = target.id;
  } else {
    const number = S.masks.length + 1;
    const mask = normalizeMasks([{
      id: editId('mask'), name: tr('{tool} {number}', {tool: localToolLabel(kind), number}),
      type: kind, bitmap: result.bitmap, provider: result.provider,
      depthLow: component.depthLow, depthHigh: component.depthHigh,
      components: [component], enabled: true, invert: false, opacity: 1,
      lumaLow: 0, lumaHigh: 1, colorHue: null, colorRange: 30,
      colorAmount: 1, grade: { ...LOCAL_GRADE_DEFAULTS },
    }])[0];
    S.masks.push(mask); S.selectedMaskId = mask.id;
    createdMask = mask;
  }
  S.editGesture = null; S.maskCreateOpen = false; S.maskRefineMode = null;
  S.maskTextureDirty = true; syncMaskPanel(); drawGrade(); saveState();
  toast(tr('{tool} mask ready', {tool: localToolLabel(kind)}));
  return createdMask;
}
$('maskAddSubject').onclick = () => createSemanticMask('subject');
$('maskAddSky').onclick = () => createSemanticMask('sky');
$('maskAddDepth').onclick = () => createSemanticMask('depth');
$('maskAddObject').onclick = () => {
  if (!cur()) return;
  S.editGesture = { type: 'semantic-object' };
  $('maskInstruction').textContent = tr("Click the object to select.");
};
$('maskAddPeople').onclick = () => {
  $('peopleMaskMenu').hidden = !$('peopleMaskMenu').hidden;
};
document.querySelectorAll('[data-person-part]').forEach((button) => {
  button.onclick = () => createSemanticMask(button.dataset.personPart);
});
$('maskSoftenSkin').onclick = async () => {
  const mask = await createSemanticMask('face-skin');
  if (!mask) return;
  mask.name = tr('Soften skin');
  mask.grade.texture = -0.4;
  mask.grade.clarity = -0.2;
  syncMaskPanel(); drawGrade(); saveState();
};
$('maskSemanticCombine').onchange = syncMaskPanel;
$('maskReset').onclick = () => {
  if (!S.masks.length) return;
  pushUndo(); S.masks = []; S.selectedMaskId = null; S.maskTextureDirty = true;
  S.maskCreateOpen = true; S.maskRefineMode = null;
  syncMaskPanel(); drawGrade(); saveState();
};
function deleteMask(id = S.selectedMaskId) {
  if (!S.masks.some((mask) => mask.id === id)) return;
  pushUndo(); S.masks = S.masks.filter((mask) => mask.id !== id);
  S.selectedMaskId = S.masks[0]?.id || null; S.maskTextureDirty = true;
  S.maskCreateOpen = !S.masks.length;
  S.maskRefineMode = selectedMask()?.type === 'brush' ? 'add' : null;
  syncMaskPanel(); drawGrade(); saveState();
}
$('maskDelete').onclick = () => deleteMask();
$('maskRename').onclick = async () => {
  const mask = selectedMask(); if (!mask) return;
  const name = await askName(tr("Rename mask"), mask.name);
  if (!name || name === mask.name) return;
  pushUndo(); mask.name = name.slice(0, 60); syncMaskPanel(); saveState();
};
$('maskRefineAdd').onclick = () => { S.maskRefineMode = 'add'; syncMaskPanel(); drawEditOverlay(); };
$('maskRefineSubtract').onclick = () => { S.maskRefineMode = 'subtract'; syncMaskPanel(); drawEditOverlay(); };
$('maskRefineIntersect').onclick = () => { S.maskRefineMode = 'intersect'; syncMaskPanel(); drawEditOverlay(); };
$('maskEditShape').onclick = () => { S.maskRefineMode = null; syncMaskPanel(); drawEditOverlay(); };
$('maskShowOverlay').onchange = drawEditOverlay;
for (const id of ['maskBrushSize', 'maskBrushFeather', 'maskBrushFlow', 'maskBrushDensity', 'maskBrushTolerance']) {
  $(id).addEventListener('input', () => {
    if (id === 'maskBrushSize') S.brushSize = +$(id).value;
    else if (id === 'maskBrushFeather') S.brushFeather = +$(id).value;
    else if (id === 'maskBrushFlow') S.brushFlow = +$(id).value;
    else if (id === 'maskBrushDensity') S.brushDensity = +$(id).value;
    else S.brushTolerance = +$(id).value;
    const output = $(id + 'V');
    output.textContent = `${Math.round(+$(id).value * 100)}%`;
    drawEditOverlay();
  });
}
$('maskBrushAutoMask').onchange = () => {
  S.brushAutoMask = $('maskBrushAutoMask').checked;
  syncMaskPanel();
};
MASK_CURVE = installMaskCurve({canvas: $('maskCurve'), reset: $('maskCurveReset'),
  channel: $('maskCurveChannel'), getMask: selectedMask, pushUndo, dropUndo,
  changed: () => drawGrade(), save: () => saveState()});
for (const [id, key] of [['maskRadiusX', 'radiusX'], ['maskRadiusY', 'radiusY'],
  ['maskAngle', 'angle'], ['maskShapeFeather', 'feather']]) {
  $(id).addEventListener('pointerdown', pushUndo);
  $(id).addEventListener('input', () => {
    const mask = selectedMask(); if (mask?.type !== 'radial') return;
    mask[key] = +$(id).value;
    S.maskTextureDirty = true; syncMaskPanel(); drawGrade();
  });
  $(id).addEventListener('change', () => saveState());
}
$('maskVisible').onchange = () => {
  const mask = selectedMask(); if (!mask) return;
  pushUndo(); mask.enabled = $('maskVisible').checked; syncMaskPanel(); drawGrade(); saveState();
};
$('maskInvert').onchange = () => {
  const mask = selectedMask(); if (!mask) return;
  pushUndo(); mask.invert = $('maskInvert').checked; S.maskTextureDirty = true;
  syncMaskPanel(); drawGrade(); saveState();
};

for (const id of ['maskOpacity', 'maskLumaLow', 'maskLumaHigh']) {
  $(id).addEventListener('pointerdown', pushUndo);
  $(id).addEventListener('input', () => {
    const mask = selectedMask(); if (!mask) return;
    const key = { maskOpacity: 'opacity', maskLumaLow: 'lumaLow', maskLumaHigh: 'lumaHigh' }[id];
    mask[key] = +$(id).value;
    if (mask.lumaLow > mask.lumaHigh) {
      if (key === 'lumaLow') mask.lumaHigh = mask.lumaLow; else mask.lumaLow = mask.lumaHigh;
    }
    syncMaskPanel(); drawGrade();
  });
  $(id).addEventListener('change', () => saveState());
}
for (const id of ['maskDepthLow', 'maskDepthHigh']) {
  $(id).addEventListener('pointerdown', pushUndo);
  $(id).addEventListener('input', () => {
    const mask = selectedMask();
    if (!mask) return;
    const depth = depthMaskComponent(mask);
    if (!depth) return;
    const key = id === 'maskDepthLow' ? 'depthLow' : 'depthHigh';
    depth[key] = +$(id).value;
    if (depth.depthLow > depth.depthHigh) {
      if (key === 'depthLow') depth.depthHigh = depth.depthLow;
      else depth.depthLow = depth.depthHigh;
    }
    if (mask.type === 'depth') {
      mask.depthLow = depth.depthLow;
      mask.depthHigh = depth.depthHigh;
      if (mask.components?.[0]) {
        mask.components[0].depthLow = depth.depthLow;
        mask.components[0].depthHigh = depth.depthHigh;
      }
    }
    S.maskTextureDirty = true;
    syncMaskPanel(); drawGrade(); drawEditOverlay();
  });
  $(id).addEventListener('change', () => saveState());
}
$('maskColorEnabled').onchange = () => {
  const mask = selectedMask(); if (!mask) return;
  pushUndo();
  mask.colorHue = $('maskColorEnabled').checked
    ? (mask.colorHue == null ? 0 : mask.colorHue) : null;
  syncMaskPanel(); drawGrade(); drawEditOverlay(); saveState();
};
for (const id of ['maskColorHue', 'maskColorRange', 'maskColorAmount']) {
  $(id).addEventListener('pointerdown', pushUndo);
  $(id).addEventListener('input', () => {
    const mask = selectedMask(); if (!mask) return;
    const key = { maskColorHue: 'colorHue', maskColorRange: 'colorRange',
      maskColorAmount: 'colorAmount' }[id];
    mask[key] = +$(id).value;
    syncMaskPanel(); drawGrade(); drawEditOverlay();
  });
  $(id).addEventListener('change', () => saveState());
}
$('maskColorSample').onclick = () => {
  if (!selectedMask()) return;
  S.maskColorPick = !S.maskColorPick;
  if (S.maskColorPick) {
    S.wbPick = false; S.pointColorPick = false; setCompareActive(false);
    $('wbBtn').classList.remove('on'); syncPointColor();
    toast(tr("Click a color, or Shift-drag to average an area"));
  }
  $('maskColorSample').classList.toggle('on', S.maskColorPick);
  syncCompareControl();
};
function sampleMaskColorAt(u, v) {
  const mask = selectedMask(); if (!mask) return false;
  if (!S.gl) {
    toast(tr("Color sampling: waiting for image preview…"));
    return false;
  }
  refreshWebGLSamplingSurface();
  const px = S.gl.samplePixel(u, v);
  if (!px) return false;
  const [red, green, blue] = [px[0] / 255, px[1] / 255, px[2] / 255];
  if (Math.max(red, green, blue) - Math.min(red, green, blue) < 0.015) {
    toast(tr("Choose a more colorful area")); return true;
  }
  pushUndo(); mask.colorHue = rgbHue(red, green, blue);
  S.maskColorPick = false; $('maskColorSample').classList.remove('on');
  syncMaskPanel(); syncCompareControl(); drawGrade(); drawEditOverlay(); saveState();
  toast(tr("Mask color range sampled"));
  return true;
}
function sampleMaskColorArea(start, end) {
  const mask = selectedMask();
  if (!mask || !S.gl) return false;
  refreshWebGLSamplingSurface();
  const left = Math.min(start[0], end[0]), right = Math.max(start[0], end[0]);
  const top = Math.min(start[1], end[1]), bottom = Math.max(start[1], end[1]);
  if ((right - left) * (bottom - top) < 0.00005) {
    return sampleMaskColorAt(end[0], end[1]);
  }
  let x = 0, y = 0, weight = 0;
  for (let row = 0; row < 9; row++) {
    for (let column = 0; column < 9; column++) {
      const u = left + (right - left) * (column + 0.5) / 9;
      const v = top + (bottom - top) * (row + 0.5) / 9;
      const px = S.gl.samplePixel(u, v);
      if (!px) continue;
      const red = px[0] / 255, green = px[1] / 255, blue = px[2] / 255;
      const saturation = Math.max(red, green, blue) - Math.min(red, green, blue);
      if (saturation < 0.015) continue;
      const angle = rgbHue(red, green, blue) * Math.PI / 180;
      x += Math.cos(angle) * saturation;
      y += Math.sin(angle) * saturation;
      weight += saturation;
    }
  }
  if (weight < 0.015) {
    toast(tr("Choose a more colorful area"));
    return true;
  }
  pushUndo();
  mask.colorHue = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  S.maskColorPick = false; $('maskColorSample').classList.remove('on');
  syncMaskPanel(); syncCompareControl(); drawGrade(); drawEditOverlay(); saveState();
  toast(tr("Mask color range sampled from area"));
  return true;
}
document.querySelectorAll('[data-local]').forEach((input) => {
  input.addEventListener('pointerdown', pushUndo);
  input.addEventListener('input', () => {
    const mask = selectedMask(); if (!mask) return;
    mask.grade[input.dataset.local] = +input.value;
    document.querySelector(`[data-localv="${input.dataset.local}"]`).textContent = fmtG(+input.value);
    drawGrade();
  });
  input.addEventListener('change', () => saveState());
  const resetLocal = () => {
    const mask = selectedMask(); if (!mask) return;
    pushUndo();
    mask.grade[input.dataset.local] = 0;
    input.value = '0';
    const out = document.querySelector(`[data-localv="${input.dataset.local}"]`);
    if (out) out.textContent = fmtG(0);
    drawGrade();
    drawEditOverlay();
    saveState();
  };
  input.addEventListener('dblclick', resetLocal);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetLocal);
});

function automaticHealSource(point, index = S.heals.length) {
  const angle = (index % 8) * Math.PI / 4;
  const distance = 0.13;
  return [clamp(point[0] + Math.cos(angle) * distance, 0, 1),
    clamp(point[1] + Math.sin(angle) * distance, 0, 1)];
}

function refreshBaseEdits(debounced = false) {
  syncPreviewBackend();
  if (nativePreviewActive() && !nativeBaseRequiresBake() && !S.baseEditsBaked) {
    previewFrameScheduler.request({ edits: true, overlay: true });
    return;
  }
  if (debounced) renderPhysicalPreview(); else renderFilm(0);
}

function setHealToolMode(mode) {
  if (!['remove', 'heal', 'clone'].includes(mode)) return;
  const spot = selectedHeal();
  S.healToolMode = mode;
  if (spot && spot.mode !== mode) {
    pushUndo();
    spot.mode = mode;
    if (mode === 'remove') spot.source = [...spot.target];
    else if (spot.source[0] === spot.target[0] && spot.source[1] === spot.target[1]) {
      spot.source = automaticHealSource(spot.target);
    }
    saveState(); refreshBaseEdits();
  }
  syncHealPanel(); drawEditOverlay();
}

for (const mode of ['remove', 'heal', 'clone']) {
  $(`healTool${mode[0].toUpperCase()}${mode.slice(1)}`).onclick = () => setHealToolMode(mode);
}
$('healReposition').onclick = () => {
  const spot = selectedHeal(); if (!spot) return;
  S.editGesture = { type: 'heal-place-target', id: S.selectedHealId };
  $('healInstruction').textContent = tr("Click the new target position.");
  syncOverlayCursorClass();
};
function deleteHeal(id = S.selectedHealId) {
  if (!S.heals.some((spot) => spot.id === id)) return;
  pushUndo(); S.heals = S.heals.filter((spot) => spot.id !== id);
  S.selectedHealId = S.heals[0]?.id || null; syncHealPanel(); drawEditOverlay();
  saveState(); refreshBaseEdits();
}
$('healDelete').onclick = () => deleteHeal();
$('healReset').onclick = () => {
  if (!S.heals.length) return;
  pushUndo(); S.heals = []; S.selectedHealId = null; syncHealPanel(); drawEditOverlay();
  saveState(); refreshBaseEdits();
};
$('healVisible').onchange = () => {
  const spot = selectedHeal(); if (!spot) return;
  pushUndo(); spot.enabled = $('healVisible').checked; syncHealPanel(); saveState(); refreshBaseEdits();
};
$('healRefresh').onclick = () => {
  const spot = selectedHeal(); if (!spot || spot.mode === 'remove') return;
  pushUndo();
  const dx = spot.source[0] - spot.target[0], dy = spot.source[1] - spot.target[1];
  const distance = Math.max(0.08, Math.hypot(dx, dy));
  const angle = Math.atan2(dy, dx) + Math.PI / 3;
  spot.source = [clamp(spot.target[0] + Math.cos(angle) * distance, 0, 1),
    clamp(spot.target[1] + Math.sin(angle) * distance, 0, 1)];
  syncHealPanel(); drawEditOverlay(); saveState(); refreshBaseEdits();
};
$('healVisualize').onchange = () => {
  $('healVisualizeRow').hidden = !$('healVisualize').checked;
  refreshSpotVisualization();
};
$('healVisualizeThreshold').addEventListener('input', () => {
  $('healVisualizeThresholdV').textContent = `${Math.round(+$('healVisualizeThreshold').value * 100)}%`;
  refreshSpotVisualization();
});
for (const id of ['healRadius', 'healFeather', 'healOpacity']) {
  const rememberUndo = () => { if (selectedHeal()) pushUndo(); };
  $(id).addEventListener('pointerdown', rememberUndo);
  $(id).addEventListener('keydown', (event) => {
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) rememberUndo();
  });
  $(id).addEventListener('input', () => {
    const spot = selectedHeal();
    const key = { healRadius: 'radius', healFeather: 'feather', healOpacity: 'opacity' }[id];
    S.healBrush[key] = +$(id).value;
    if (spot) spot[key] = S.healBrush[key];
    syncHealPanel(); drawEditOverlay();
    if (spot) refreshBaseEdits(true);
  });
  $(id).addEventListener('change', () => { if (selectedHeal()) saveState(); });
}

$('lensProfileOverride').onchange = () => {
  if (!cur()) return;
  pushUndo();
  S.optics.profileOverride = $('lensProfileOverride').value ? JSON.parse($('lensProfileOverride').value) : null;
  syncOpticsPanel();
  S.optics.profileEnabled = !!S.lensProfile;
  syncOpticsPanel(); saveState(); refreshBaseEdits();
};
for (const id of ['lensProfileEnabled', 'lensProfileDistortion', 'lensProfileVignette']) {
  $(id).onchange = () => {
    pushUndo();
    const key = { lensProfileEnabled: 'profileEnabled', lensProfileDistortion: 'profileDistortion', lensProfileVignette: 'profileVignette' }[id];
    S.optics[key] = $(id).checked; syncOpticsPanel(); saveState(); refreshBaseEdits();
  };
}
for (const id of ['lensFlipHorizontal', 'lensFlipVertical']) {
  $(id).onclick = () => {
    pushUndo();
    const key = id === 'lensFlipHorizontal' ? 'flipHorizontal' : 'flipVertical';
    S.optics[key] = !S.optics[key];
    syncOpticsPanel(); saveState(); refreshBaseEdits();
  };
}
document.querySelectorAll('[data-optics]').forEach((input) => {
  input.addEventListener('pointerdown', pushUndo);
  input.addEventListener('input', () => {
    S.optics[input.dataset.optics] = +input.value;
    document.querySelector(`[data-opticsv="${input.dataset.optics}"]`).textContent = (+input.value).toFixed(2);
    syncCropPanel();
    refreshBaseEdits(true);
  });
  input.addEventListener('change', () => saveState());
});
$('lensReset').onclick = (event) => {
  event.stopPropagation();
  pushUndo();
  for (const key of ['profileOverride', 'profileEnabled', 'profileDistortion', 'profileVignette', 'distortion', 'vignette']) {
    S.optics[key] = OPTICS_DEFAULTS[key];
  }
  delete S.grade.chromaticAberrationRedCyan;
  delete S.grade.chromaticAberrationBlueYellow;
  syncOpticsPanel(); syncGrade(); drawGrade(); saveState(); refreshBaseEdits();
};

function overlayPoint(event, rect = $('cv').getBoundingClientRect()) {
  return [clamp((event.clientX - rect.left) / rect.width, 0, 1),
    clamp((event.clientY - rect.top) / rect.height, 0, 1)];
}

function overlayDistance(a, b, rect = $('cv').getBoundingClientRect()) {
  return Math.hypot((a[0] - b[0]) * rect.width, (a[1] - b[1]) * rect.height);
}

function healHandleAt(point, rect) {
  return S.localPinsVisible ? findHealHandle(S.heals, S.selectedHealId, point, rect) : null;
}

function maskPointCount() {
  const count = strokes => (strokes || []).reduce((sum, stroke) => sum + (stroke.points?.length || 0), 0);
  return S.masks.reduce((sum, mask) => sum + maskComponents(mask).reduce(
    (total, component) => total + count(component.strokes), 0)
    + count(mask.addStrokes) + count(mask.subtractStrokes) + count(mask.intersectStrokes), 0);
}

$('editOverlay').addEventListener('pointerdown', (event) => {
  if (!cur() || event.button !== 0) return;
  event.stopPropagation();
  event.preventDefault();
  const rect = $('cv').getBoundingClientRect();
  const point = overlayPoint(event, rect);
  S.overlayAltKey = event.altKey;
  S.overlayHoverPoint = point;
  S.overlayHoverClientPoint = [event.clientX, event.clientY];
  if (S.activePane === 'maskPane') {
    if (S.maskColorPick) {
      if (event.shiftKey) {
        $('editOverlay').setPointerCapture(event.pointerId);
        S.editGesture = { type: 'mask-color-sample', pointerId: event.pointerId,
          start: point, end: point, rect };
        return;
      }
      sampleMaskColorAt(point[0], point[1]);
      return;
    }
    if (S.editGesture?.type === 'semantic-object') {
      createSemanticMask('object', point);
      return;
    }
    const mask = selectedMask(); if (!mask) return;
    const isSemantic = !['brush', 'linear', 'radial'].includes(mask.type);
    const refineMode = event.altKey ? 'subtract' :
      (S.maskRefineMode || (mask.type === 'brush' ? 'add' : null));
    if (!refineMode && isSemantic) return;
    if (refineMode && maskPointCount() >= MAX_TOTAL_MASK_POINTS) {
      return toast(tr("This photo has reached the 20,000-point mask limit"));
    }
    pushUndo(); $('editOverlay').setPointerCapture(event.pointerId);
    if (refineMode) {
      const strokes = refineMode === 'subtract' ? mask.subtractStrokes :
        refineMode === 'intersect' ? mask.intersectStrokes :
          (mask.type === 'brush' ? mask.strokes : mask.addStrokes);
      if (strokes.length >= 64) {
        $('editOverlay').releasePointerCapture(event.pointerId);
        return toast(tr('This brush has reached its 64-stroke limit. Create another mask to keep painting.'));
      }
      const stroke = { size: S.brushSize, feather: S.brushFeather, flow: S.brushFlow,
        density: S.brushDensity, buildUp: true, points: [point] };
      if (S.brushAutoMask) {
        stroke.edgeMask = captureBrushEdgeMask(point);
        if (!stroke.edgeMask) {
          $('editOverlay').releasePointerCapture(event.pointerId);
          return toast(tr('Auto Mask is waiting for the photo preview.'));
        }
      }
      strokes.push(stroke);
      S.editGesture = { type: 'brush', pointerId: event.pointerId, stroke, rect };
    } else if (mask.type === 'linear') {
      const handle = S.localPinsVisible ? linearHandleAt(mask, point, rect) : null;
      S.editGesture = { type: 'linear', handle, origin: point,
        start: [...mask.start], end: [...mask.end], pointerId: event.pointerId, rect };
      if (!handle) { mask.start = point; mask.end = point; }
    } else {
      const handle = S.localPinsVisible ? radialHandleAt(mask, point, rect) : null;
      if (!handle) {
        mask.center = point; mask.radius = mask.radiusX = mask.radiusY = 0.01; mask.angle = 0;
      }
      S.editGesture = { type: 'radial', handle, center: [...mask.center],
        pointerId: event.pointerId, start: point, rect };
    }
    S.maskTextureDirty = true; drawGrade();
  } else if (S.activePane === 'healPane') {
    if (S.editGesture?.type === 'heal-place-target' && S.editGesture.pointerId === undefined) {
      const spot = S.heals.find((item) => item.id === S.editGesture.id);
      if (!spot) { S.editGesture = null; syncOverlayCursorClass(); return; }
      pushUndo();
      const offset = [spot.source[0] - spot.target[0], spot.source[1] - spot.target[1]];
      spot.target = point;
      spot.source = spot.mode === 'remove' ? [...point] :
        [clamp(point[0] + offset[0], 0, 1), clamp(point[1] + offset[1], 0, 1)];
      S.editGesture = null;
      syncHealPanel(); drawEditOverlay(); saveState(); refreshBaseEdits();
      return;
    }
    const hit = healHandleAt(point, rect);
    if (!hit && S.heals.length >= MAX_HEALS) {
      return toast(tr("Up to 50 corrections can be active"));
    }
    pushUndo();
    $('editOverlay').setPointerCapture(event.pointerId);
    if (hit) {
      S.selectedHealId = hit.spot.id; S.healToolMode = hit.spot.mode;
      S.editGesture = { type: `heal-move-${hit.handle}`, pointerId: event.pointerId,
        spot: hit.spot, rect };
    } else {
      const spot = {
        id: editId('heal'), mode: S.healToolMode, enabled: true, target: point,
        source: S.healToolMode === 'remove' ? [...point] : automaticHealSource(point),
        ...S.healBrush,
      };
      S.heals.push(spot); S.selectedHealId = spot.id;
      S.editGesture = { type: 'heal-create', pointerId: event.pointerId,
        spot, start: point, rect };
    }
    syncHealPanel(); drawEditOverlay();
  }
});
$('editOverlay').addEventListener('pointermove', (event) => {
  const gesture = S.editGesture;
  const rect = gesture?.rect || $('cv').getBoundingClientRect();
  const point = overlayPoint(event, rect);
  S.overlayAltKey = event.altKey;
  S.overlayHoverPoint = point;
  S.overlayHoverClientPoint = [event.clientX, event.clientY];
  if (!gesture || gesture.pointerId !== event.pointerId) { drawEditOverlay(); return; }
  if (S.activePane === 'maskPane') {
    if (gesture.type === 'mask-color-sample') {
      gesture.end = point;
      drawEditOverlay();
      return;
    }
    const mask = selectedMask(); if (!mask) return;
    if (gesture.type === 'brush') {
      const previous = gesture.stroke.points.at(-1);
      if (Math.hypot(previous[0] - point[0], previous[1] - point[1]) > 0.002) {
        const total = maskPointCount();
        if (gesture.stroke.points.length >= 512) {
          if (!gesture.pointLimitShown) {
            gesture.pointLimitShown = true;
            toast(tr('This stroke has reached its point limit. Release and paint another stroke to continue.'));
          }
        } else if (total >= MAX_TOTAL_MASK_POINTS) {
          if (!gesture.pointLimitShown) {
            gesture.pointLimitShown = true;
            toast(tr("This photo has reached the 20,000-point mask limit"));
          }
        } else gesture.stroke.points.push(point);
      }
    } else if (gesture.type === 'linear') editLinear(mask, gesture, point);
    else if (gesture.type === 'radial') {
      editRadial(mask, gesture, point, rect, event.shiftKey);
    }
    S.maskTextureDirty = true; drawGrade();
  } else if (S.activePane === 'healPane') {
    if (gesture.type === 'heal-move-source') gesture.spot.source = point;
    else if (gesture.type === 'heal-move-target') {
      gesture.spot.target = point;
      if (gesture.spot.mode === 'remove') gesture.spot.source = [...point];
    } else if (gesture.type === 'heal-create') {
      const radius = Math.hypot((point[0] - gesture.start[0]) * rect.width,
        (point[1] - gesture.start[1]) * rect.height) / Math.min(rect.width, rect.height);
      if (radius > 0.008) gesture.spot.radius = clamp(radius, 0.005, 0.25);
    }
    drawEditOverlay();
    if (nativePreviewActive()) previewFrameScheduler.request({ edits: true });
  }
});
function finishEditGesture(event) {
  if (!S.editGesture || S.editGesture.pointerId !== event.pointerId) return;
  if (S.editGesture.type === 'mask-color-sample') {
    const gesture = S.editGesture;
    S.editGesture = null;
    if ($('editOverlay').hasPointerCapture(event.pointerId)) {
      $('editOverlay').releasePointerCapture(event.pointerId);
    }
    if (event.type === 'pointerup') {
      sampleMaskColorArea(gesture.start, overlayPoint(event, gesture.rect));
    }
    drawEditOverlay();
    return;
  }
  const wasMask = S.activePane === 'maskPane';
  S.editGesture = null;
  if ($('editOverlay').hasPointerCapture(event.pointerId)) $('editOverlay').releasePointerCapture(event.pointerId);
  if (wasMask) {
    syncMaskPanel();
    // Replace the provisional 256px channel with the full packed texture once
    // the pointer is up and bridge traffic is no longer on the hot path.
    S.maskTextureDirty = true;
    drawGrade();
  } else { syncHealPanel(); refreshBaseEdits(); }
  saveState(); drawEditOverlay();
}
$('editOverlay').addEventListener('pointerup', finishEditGesture);
$('editOverlay').addEventListener('pointercancel', finishEditGesture);
$('editOverlay').addEventListener('lostpointercapture', finishEditGesture);
$('editOverlay').addEventListener('pointerleave', () => {
  if (S.editGesture?.pointerId !== undefined) return;
  S.overlayHoverPoint = null; S.overlayHoverClientPoint = null; drawEditOverlay();
});

for (const type of ['keydown', 'keyup']) {
  document.addEventListener(type, event => {
    if (S.overlayAltKey === event.altKey) return;
    S.overlayAltKey = event.altKey;
    if (S.overlayHoverClientPoint) drawEditOverlay();
  });
}
window.addEventListener('blur', () => {
  S.overlayAltKey = false;
  S.overlayHoverPoint = null; S.overlayHoverClientPoint = null;
  drawEditOverlay();
});

/* ------------------------------------------------------------ film render */
const previewProgress = createPreviewProgress((progress) => {
  S.previewProgress = progress;
  syncPreviewDetailStatus();
});

function previewGeometryKey(name, rotate = 0) {
  const quarterTurns = ((Math.round((+rotate || 0) / 90) % 4) + 4) % 4;
  return name ? `${name}:${quarterTurns % 2}` : null;
}

function shouldPreservePresentationGeometry(phase, previousKey, nextKey) {
  return phase === 'interactive' && !!previousKey && previousKey === nextKey;
}

function syncPreviewDetailStatus() {
  const image = cur(), detail = S.previewDetail?.name === image?.name ? S.previewDetail : {};
  const detailLabel = previewDetailLabel({ ...detail, state: S.renderState,
    source: Math.max(+image?.width || 0, +image?.height || 0), actual: S.zoomMode === '100' });
  const progress = S.previewProgress;
  const label = progress?.visible ? progress.label : '';
  $('previewDetailLabel').textContent = image ? label : '';
  const working = Boolean(progress?.visible && progress?.active);
  $('previewProgressCount').hidden = !working;
  $('previewProgressCount').textContent = `${progress?.completed || 0} / 5`;
  $('previewProgressTrack').hidden = !working;
  $('previewProgressTrack').setAttribute('aria-valuenow', String(progress?.completed || 0));
  $('previewProgressTrack').setAttribute('aria-valuetext', `${progress?.completed || 0} of 5 stages complete`);
  $('previewProgressFill').style.width = `${(progress?.completed || 0) * 20}%`;
  $('previewDetailStatus').hidden = !image || !label;
  $('previewDetailStatus').classList.toggle('working', Boolean(progress?.visible && progress?.active));
  $('zoom1').title = detailLabel || tr('View actual pixels (100%) to assess sharpness and noise');
}
function setRenderPresentation(state, name = cur()?.name, message = '') {
  if (name && cur()?.name !== name) return;
  if (state === 'error' || state === 'ready') {
    recordPhotoDisplayState(S.images.find(image => image.name === name), state === 'error', 'preview');
  }
  S.renderState = state;
  S.renderName = name || null;
  if (state === 'pending') {
    previewProgress.start(tr('Loading preview…'));
    $('zoomwrap').setAttribute('aria-busy', 'true');
  } else if (state === 'empty') {
    previewProgress.finish();
    $('zoomwrap').setAttribute('aria-busy', 'false');
  }
  syncPreviewDetailStatus();
  if (state === 'ready') {
    S.hasPresentedImage = true;
    S.presentedPhotoName = name;
    S.presentedGeometryKey = previewGeometryKey(name, S.params?.rotate);
  }
  const wrap = $('zoomwrap');
  wrap.classList.toggle('photo-pending', state === 'pending');
  wrap.classList.toggle('photo-error', state === 'error');
  const isEmpty = (state === 'pending' || state === 'empty') && !S.hasPresentedImage;
  wrap.classList.toggle('photo-empty', isEmpty);
  if (message) wrap.dataset.renderMessage = message;
  else delete wrap.dataset.renderMessage;
  scheduleNativeViewportLayout();
  window.dispatchEvent(new CustomEvent('lighttable-render-state', {
    detail: { state: S.renderState, name: S.renderName, message },
  }));
}

let automaticPreviewTimer = null;
let automaticPreviewRequest = null;
let renderTimer = null;
let settleRenderTimer = null;
let lastInteractiveRenderAt = -Infinity;
let lastContinuousInputAt = -Infinity;
const INTERACTIVE_PREVIEW_WIDTH = 1100;
// Learn from real server round trips, excluding presentation-cache hits and
// settled/refinement renders. One-frame minimum lets fast film parameters keep
// pace with the display; slower ones naturally stop flooding the render queue.
function adaptiveInteractiveInterval(previous, roundTripMs) {
  if (!Number.isFinite(roundTripMs) || roundTripMs <= 0) return previous;
  const sample = Math.max(1000 / 60, Math.min(250, roundTripMs));
  return previous == null ? sample : previous * 0.7 + sample * 0.3;
}
let interactiveRenderIntervalMs = null;
let interactiveRenderPhoto = null;
const FULL_RESOLUTION_SETTLE_MS = 160;
function markContinuousInput() {
  lastContinuousInputAt = performance.now();
}
const PERF = {
  schema: 1,
  renders: [],
  reset() { this.renders.length = 0; },
  snapshot() { return this.renders.map((entry) => ({ ...entry })); },
  prepare(imageName, width, engine = 'rs') {
    $('engine').value = engine;
    $('pw').value = String(width);
    const index = S.images.findIndex((image) => image.name === imageName);
    go(index >= 0 ? index : 0);
  },
};
PERF.grades = GRADE_PERF;
window.__lightTablePerf = PERF;

function waitForBenchmarkSettled(
  width, presentation, timeoutMs = 180000, imageName = null,
) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('lighttable-rendered', onRendered);
      reject(new Error(`Timed out waiting for ${width}px ${presentation} preview`));
    }, timeoutMs);
    const onRendered = (event) => {
      const detail = event.detail || {};
      if (detail.requestedWidth !== width || detail.width !== width ||
          detail.refining || (imageName && detail.image !== imageName)) return;
      clearTimeout(timer);
      window.removeEventListener('lighttable-rendered', onRendered);
      if (detail.presentation !== presentation) {
        reject(new Error(
          `Expected ${presentation} preview, received ${detail.presentation}`));
        return;
      }
      resolve(detail);
    };
    window.addEventListener('lighttable-rendered', onRendered);
  });
}

async function runNativeBenchmark(width, iterations) {
  try {
    if (window.__LIGHTTABLE_NATIVE_JOURNEY_LAYER__ === 'visual-review') {
      const deadline = performance.now() + 45000;
      while (!window.__LIGHTTABLE_RECORDING_READY__) {
        if (performance.now() > deadline) throw new Error('Window recording did not become ready');
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    const presentation = nativePreviewActive() ? 'native-metal' : 'webgl';
    postNative('nativeBenchmarkProgress', { stage: 'started', width, iterations });
    $('engine').value = 'rs';
    $('pw').value = String(width);
    $('filmProfileToggle').setAttribute('aria-checked', 'true');
    S.params.profile_enabled = true;
    const initial = waitForBenchmarkSettled(width, presentation);
    renderFilm(0);
    await initial;
    postNative('nativeBenchmarkProgress', { stage: 'warmed', width, iterations });
    const interactions = window.__LIGHTTABLE_INTERACTION_BENCHMARK__
      ? await runContinuousGradeBenchmark() : null;
    const journeyLayer = window.__LIGHTTABLE_NATIVE_JOURNEY_LAYER__
      || (window.__LIGHTTABLE_NATIVE_SMOKE_JOURNEY__ ? 'pr' : '');
    const journey = journeyLayer
      ? await runNativeProductJourney(width, journeyLayer) : null;
    PERF.reset();
    for (let index = 0; index < iterations; index++) {
      const settled = waitForBenchmarkSettled(width, presentation);
      const slider = $('print_exposure');
      slider.value = String(iterations === 1
        ? 0 : 0.333 + index * (1.777 / Math.max(1, iterations - 1)));
      slider.dispatchEvent(new Event('input', { bubbles: true }));
      slider.dispatchEvent(new Event('change', { bubbles: true }));
      await settled;
      postNative('nativeBenchmarkProgress', {
        stage: 'iteration', width, iteration: index + 1, iterations,
      });
    }
    if (journey && S.params.profile_enabled) {
      const screenshotReady = waitForBenchmarkSettled(
        width, presentation, 180000, cur()?.name || null);
      $('filmProfileToggle').setAttribute('aria-checked', 'false');
      S.params.profile_enabled = false;
      renderFilm(0);
      await screenshotReady;
    }
    postNative('nativeBenchmarkComplete', {
      schema: 1,
      requestedWidth: width,
      iterations,
      image: cur()?.name || null,
      userAgent: navigator.userAgent,
      journey, interactions,
      renders: PERF.snapshot(),
    });
  } catch (error) {
    postNative('nativeBenchmarkComplete', {
      schema: 1, requestedWidth: width, iterations,
      error: String(error?.message || error), renders: PERF.snapshot(),
    });
  }
}

async function runContinuousGradeBenchmark(suffix = "") {
  const savedGrade = cloneValue(S.grade);
  const savedMasks = S.masks;
  const results = [];
  for (const mode of ['plain', 'detail', 'mask']) {
    const detailed = mode !== 'plain';
    S.masks = mode === 'mask' ? normalizeMasks([{id: 'benchmark-radial',
      type: 'radial', grade: {exposure: 0.3}}]) : savedMasks;
    S.maskTextureDirty = true;
    S.grade = { ...GRADE_DEFAULTS, ...(detailed ? {
      clarity: 0.2, sharpness: 0.3, luminanceNoise: 0.15,
      curveL: Array.from({length: 256}, (_, i) => Math.pow(i / 255, 0.95)),
    } : {}) };
    syncGrade(); drawGrade();
    await afterVisiblePaint();
    GRADE_PERF.start((detailed ? 'exposure-with-detail-and-curve' : 'exposure') +
      (mode === 'mask' ? '-and-mask' : '') + suffix);
    const slider = document.querySelector('[data-g="exposure"]');
    for (let i = 0; i < 120; i++) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      slider.value = String(Math.sin(i / 12) * 0.8);
      slider.dispatchEvent(new Event('input', { bubbles: true }));
    }
    const deadline = performance.now() + 2500;
    const last = GRADE_PERF.inputs.at(-1)?.id;
    while (!GRADE_PERF.samples.some((sample) => sample.id === last)
      && performance.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const result = GRADE_PERF.snapshot();
    if (!result.samples.some((sample) => sample.id === last)) {
      throw new Error('Continuous grade benchmark did not present its final input: ' + JSON.stringify({inputs: result.inputs, presented: result.presented, last, latest: result.samples.at(-1), seq:S.seq}));
    }
    results.push(result);
    GRADE_PERF.stop();
  }
  S.grade = savedGrade; S.masks = savedMasks; S.maskTextureDirty = true;
  syncGrade(); drawGrade();
  return { schema: 1, metric: 'input-to-gpu-completion-acknowledgement; drawable presentation recorded when available', scenarios: results };
}

const JOURNEY_RENDER_FIELDS = [
  'cached', 'engine', 'backend', 'queueMs', 'requestMs', 'serverMs',
  'residentMs', 'gpuMs', 'imageDecodeMs', 'textureUploadMs',
  'paintAfterUploadMs', 'presentation', 'nativeFetchMs', 'nativeGpuMs',
  'nativeTotalMs', 'totalMs', 'textureCacheHit', 'presentationCacheHit', 'serverQueueMs',
  'generation', 'viewport', 'nativeSharedMemory',
];

function compactJourneyRender(render) {
  return Object.fromEntries(JOURNEY_RENDER_FIELDS
    .filter((key) => render?.[key] !== undefined)
    .map((key) => [key, render[key]]));
}

function waitForNativeViewportFrame(imageName, region, afterGeneration, previousRegion = null) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('lighttable-rendered', onRender);
      reject(new Error(`Timed out waiting for native ${region ? 'viewport' : 'full-frame'} presentation`));
    }, 180000);
    const onRender = ({ detail }) => {
      if (detail.image !== imageName || detail.generation <= afterGeneration ||
          detail.generation !== S.seq || detail.presentation !== 'native-metal' || detail.refining ||
          Boolean(detail.viewport) !== region) return;
      if (previousRegion && detail.viewport.x === previousRegion.x &&
          detail.viewport.y === previousRegion.y) return;
      clearTimeout(timer);
      window.removeEventListener('lighttable-rendered', onRender);
      resolve(detail);
    };
    window.addEventListener('lighttable-rendered', onRender);
  });
}

async function waitForJourneyFrame() {
  await afterVisiblePaint();
}

function waitForRenderState(imageName, state, timeoutMs = 180000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('lighttable-render-state', onState);
      reject(new Error(tr("Timed out waiting for {imageName} to become {state}", {imageName: imageName, state: state})));
    }, timeoutMs);
    const onState = (event) => {
      const detail = event.detail || {};
      if (detail.name !== imageName || detail.state !== state) return;
      clearTimeout(timer);
      window.removeEventListener('lighttable-render-state', onState);
      resolve(detail);
    };
    window.addEventListener('lighttable-render-state', onState);
  });
}

async function waitForJourneyExport(timeoutMs = 180000) {
  const startedAt = performance.now();
  while (performance.now() - startedAt < timeoutMs) {
    const status = await getJSON('/api/export/status');
    if (!status.running && status.total > 0 && status.done >= status.total) {
      clearInterval(exportTimer);
      if (status.errors?.length) {
        throw new Error(`Journey export failed: ${status.errors.join('; ')}`);
      }
      return status;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('Timed out waiting for the journey export');
}

async function runNativeProductJourney(width, layer) {
  if (layer === 'visual-review') {
    const { runVisualJourney } = await import('/web/visual-journey.js');
    return runVisualJourney({ S, $, cur, visible, executeUICommand, uiStateReport,
      nativePreviewActive, postNative, pushUndo, drawGrade, saveState,
      waitForJourneyFrame, editSaveQueue });
  }
  if (layer === 'raw-curated' || layer === 'raw-full') {
    return runNativeRawJourney(width, layer);
  }
  return runNativeSmokeJourney(width, layer);
}

// Exercise the real native journal and save queue with an injected transport
// failure. Fixtures and native journal storage belong to the isolated journey.
async function runEditRecoveryJourney() {
  const name = cur().name;
  const original = await getJSON(`/api/state?name=${encodeURIComponent(name)}`);
  const originalGrade = {...GRADE_DEFAULTS, ...(original.grade || {})};
  // Change only the tested edit. A state response also contains read-only
  // provenance and unset film parameters that the server normalizes on write.
  const pending = {state: {name, grade: {...originalGrade, exposure: 0.321}}};
  const originalFetch = window.fetch;
  let failedClose = false;
  try {
    window.fetch = (input, options) => {
      if (String(input).startsWith('/api/state') && options?.method === 'POST') {
        return Promise.reject(new Error('Journey: render server interrupted'));
      }
      return originalFetch(input, options);
    };
    editSaveQueue.enqueue(name, pending, {immediate: true});
    failedClose = !(await window.lightTablePrepareToClose());
    if (!failedClose || editSaveQueue.getStatus().state !== 'error') {
      throw new Error('A failed edit save did not block close');
    }
    const data = await getJSON('/api/images');
    const reopened = createEditRecovery({scope: data.catalog?.path || `folder:${data.folder}`,
      nativeRequest: nativeJournalRequest});
    const records = await reopened.list();
    const recovered = records.find(record => record.name === name);
    if (!recovered || !recoveryPayloadMatches(pending, recovered.payload.state)) {
      throw new Error('Native recovery did not retain the failed edit');
    }
  } finally { window.fetch = originalFetch; }
  await editSaveQueue.retry(name);
  const saved = await getJSON(`/api/state?name=${encodeURIComponent(name)}`);
  if (!recoveryPayloadMatches(pending, saved)) throw new Error('Recovered edit did not reach the catalog');
  if ((await editRecovery.list()).some(record => record.name === name)) {
    throw new Error('Acknowledged recovery was not removed');
  }
  editSaveQueue.enqueue(name, {state: {name, grade: originalGrade}});
  if (!(await window.lightTablePrepareToClose())) throw new Error('Close did not flush the final edit');
  window.lightTableCancelClose();
  return {failedSaveBlockedClose: failedClose, nativeDraftRecovered: true,
    retrySaved: true, finalCloseFlushed: true};
}

async function runNativeSmokeJourney(width, layer = 'pr') {
  const startImage = cur()?.name || null;
  const next = visible().find((image) => image.name !== startImage);
  if (!startImage || !next) {
    throw new Error('Native smoke journey requires two visible photos');
  }
  if (!nativePreviewActive()) {
    throw new Error('Native smoke journey did not start in Metal');
  }

  const steps = [];
  const record = async (name, action) => {
    const startedAt = performance.now();
    const detail = await action();
    const entry = { name, durationMs: performance.now() - startedAt };
    if (detail?.image) entry.image = detail.image;
    if (detail?.presentation) entry.render = compactJourneyRender(detail);
    steps.push(entry);
    postNative('nativeBenchmarkProgress', { stage: 'journey-step', ...entry });
    return detail;
  };
  const renderStep = (name, imageName, presentation, action) => record(name, async () => {
    const settled = waitForBenchmarkSettled(
      width, presentation, 180000, imageName);
    await action();
    return settled;
  });

  const editRecoveryProof = await record('edit-recovery', runEditRecoveryJourney);

  await renderStep('navigate-photo', next.name, 'native-metal', () =>
    executeUICommand('goto', { name: next.name }));

  await renderStep('rapid-navigation', startImage, 'native-metal', async () => {
    void executeUICommand('goto', { name: startImage });
    void executeUICommand('goto', { name: next.name });
    await executeUICommand('goto', { name: startImage });
  });

  const fallback = await renderStep(
    'lens-profile-metal', startImage, 'native-metal', () => {
      S.optics.profileEnabled = true;
      syncOpticsPanel();
      refreshBaseEdits();
    });

  const native = await renderStep(
    'return-to-metal', startImage, 'native-metal', () => {
      S.optics.profileEnabled = false;
      syncOpticsPanel();
      refreshBaseEdits();
    });

  let correctedInteractions = null;
  if (window.__LIGHTTABLE_INTERACTION_BENCHMARK__) {
    const savedHeals = S.heals;
    await renderStep('seventeen-heals-metal', startImage, 'native-metal', () => {
      S.heals = Array.from({length: 17}, (_, i) => ({ mode: 'clone',
        target: [0.2 + i * 0.03, 0.6], source: [0.2 + i * 0.03, 0.3], radius: 0.02 }));
      refreshBaseEdits();
    });
    if (!S.baseEditsBaked) throw new Error('17 heals were not baked into the native base');
    correctedInteractions = await runContinuousGradeBenchmark('-with-17-heals');
    await renderStep('clear-heals-metal', startImage, 'native-metal', () => {
      S.heals = savedHeals; refreshBaseEdits();
    });
  }

  await renderStep('post-fallback-navigation', next.name, 'native-metal', () =>
    executeUICommand('goto', { name: next.name }));

  const samplingPickers = await record('sampling-pickers', async () => {
    $('pointColorSample').click();
    await waitForJourneyFrame();
    const pointColor = S.pointColorPick && nativePreviewActive() &&
      !document.documentElement.classList.contains('webgl-preview-fallback');
    $('pointColorSample').click();

    const previousMasks = S.masks;
    const previousMaskId = S.selectedMaskId;
    S.masks = normalizeMasks([{ id: 'journey-mask', type: 'radial' }]);
    S.selectedMaskId = S.masks[0].id;
    syncMaskPanel();
    $('maskColorSample').click();
    await waitForJourneyFrame();
    const maskColor = S.maskColorPick && nativePreviewActive() &&
      !document.documentElement.classList.contains('webgl-preview-fallback');
    $('maskColorSample').click();
    S.masks = previousMasks;
    S.selectedMaskId = previousMaskId;
    syncMaskPanel();
    if (!pointColor || !maskColor) {
      throw new Error('A color sampling picker hid the native photo');
    }
    return { pointColor, maskColor };
  });

  await renderStep('film-off', next.name, 'native-metal', () => {
    if (S.params.profile_enabled !== false) return executeUICommand('filmToggle');
    renderFilm(0);
  });
  await renderStep('film-on', next.name, 'native-metal', () => {
    if (S.params.profile_enabled === false) return executeUICommand('filmToggle');
    renderFilm(0);
  });
  if (S.params.profile_enabled === false) throw new Error('Film did not activate before slider/viewport proof');
  await renderStep('slider-adjustment', next.name, 'native-metal', () =>
    executeUICommand('slider', { key: 'print_exposure', value: 0.05 }));

  await record('compare-on', async () => {
    await executeUICommand('compare');
    await waitForJourneyFrame();
    const state = uiStateReport();
    if (!state.compare.active) throw new Error('Compare did not become active');
    return state;
  });
  await record('compare-off', async () => {
    await executeUICommand('compare');
    await waitForJourneyFrame();
    const state = uiStateReport();
    if (state.compare.active) throw new Error('Compare did not turn off');
    return state;
  });
  const actualRender = await record('zoom-actual', async () => {
    await executeUICommand('zoomIn');
    await waitForJourneyFrame();
    const presented = waitForNativeViewportFrame(next.name, true, S.seq);
    for (let attempt = 0; attempt < 3; attempt++) {
      const state = await executeUICommand('zoomActual');
      if (state.zoomMode === '100') {
        const result = await presented;
        if (result.viewport.width * result.viewport.height >=
            result.viewport.fullWidth * result.viewport.fullHeight) {
          throw new Error('Actual-size proof rendered the entire source');
        }
        return result;
      }
      await waitForJourneyFrame();
    }
    throw new Error('Actual-size zoom did not activate');
  });
  await record('viewport-pan', async () => {
    const presented = waitForNativeViewportFrame(next.name, true, S.seq, actualRender.viewport);
    const wrap = $('zoomwrap'), bounds = wrap.getBoundingClientRect();
    const origin = { clientX: bounds.left + bounds.width / 2, clientY: bounds.top + bounds.height / 2 };
    wrap.dispatchEvent(new PointerEvent('pointerdown', { ...origin, button: 0, pointerId: 997, bubbles: true }));
    wrap.dispatchEvent(new PointerEvent('pointermove', { clientX: origin.clientX + 320,
      clientY: origin.clientY + 160, button: 0, pointerId: 997, bubbles: true }));
    wrap.dispatchEvent(new PointerEvent('pointerup', { clientX: origin.clientX + 320,
      clientY: origin.clientY + 160, button: 0, pointerId: 997, bubbles: true }));
    return presented;
  });
  await record('viewport-film-slider', async () => {
    const presented = waitForNativeViewportFrame(next.name, true, S.seq);
    const previous = +S.params.print_exposure;
    await executeUICommand('slider', { key: 'print_exposure', value: previous === 1.37 ? 1.73 : 1.37 });
    const result = await presented;
    if (+S.params.print_exposure === previous || result.presentationCacheHit) {
      throw new Error('Viewport film slider did not produce a distinct film render');
    }
    if (result.engine !== 'rs') throw new Error('Viewport film slider did not use the resident film engine');
    return result;
  });
  await record('zoom-fit', async () => {
    const presented = waitForNativeViewportFrame(next.name, false, S.seq);
    await executeUICommand('zoomFit');
    const result = await presented;
    if (S.zoomMode !== 'fit' || S.nativeViewport) throw new Error('Fit zoom did not restore a full-frame surface');
    return result;
  });

  const destination = window.__LIGHTTABLE_NATIVE_JOURNEY_EXPORT_DIR__;
  if (!destination) throw new Error('Native journey export directory is missing');
  let exportInteractions = null;
  const exportStatus = await record('export-photo', async () => {
    const queued = await runExport({
      which: 'selected', names: [next.name], format: 'jpeg', quality: 85,
      longEdge: width, outputSpace: 'srgb', destination,
      filenameTemplate: '{filename}_journey', collision: 'overwrite',
    });
    if (!queued?.queued) throw new Error('Journey export did not queue a photo');
    if (window.__LIGHTTABLE_INTERACTION_BENCHMARK__) {
      exportInteractions = await runContinuousGradeBenchmark('-during-export');
    }
    return waitForJourneyExport();
  });

  const visibilityControls = await record('visibility-controls-on', async () => {
    $('clipBtn').click();
    $('wbBtn').click();
    await waitForJourneyFrame();
    const state = uiStateReport();
    const webglFallback = document.documentElement.classList.contains(
      'webgl-preview-fallback');
    if (!S.clip || !S.wbPick || !nativePreviewActive() || webglFallback) {
      throw new Error('Clipping or white-balance sampling hid the native photo');
    }
    return { ...state, clipping: S.clip, whiteBalance: S.wbPick, webglFallback };
  });

  const finalState = uiStateReport();
  if (finalState.current !== next.name || finalState.render.state !== 'ready' ||
      finalState.render.name !== next.name ||
      finalState.render.backend !== 'native-metal') {
    throw new Error('Journey ended without the selected photo ready in Metal');
  }
  return {
    schema: 2,
    layer,
    startImage,
    navigatedImage: cur()?.name || null,
    correctedPresentation: fallback.presentation,
    returnPresentation: native.presentation,
    samplingPickers,
    correctedInteractions,
    exportInteractions,
    visibilityControls,
    editRecovery: editRecoveryProof,
    steps,
    export: {
      total: exportStatus.total,
      done: exportStatus.done,
      destination,
    },
    finalState,
  };
}

async function runNativeRawJourney(width, layer) {
  const configured = Array.isArray(window.__LIGHTTABLE_NATIVE_JOURNEY_IMAGES__)
    ? window.__LIGHTTABLE_NATIVE_JOURNEY_IMAGES__ : [];
  const names = configured.filter((name) =>
    S.images.some((image) => image.name === name));
  if (!names.length || names.length !== configured.length) {
    throw new Error(`RAW journey could not find every fixture (${names.length}/${configured.length})`);
  }
  const corrupt = window.__LIGHTTABLE_NATIVE_JOURNEY_CORRUPT__ || null;
  const valid = names.filter((name) => name !== corrupt);
  if (!valid.length) throw new Error('RAW journey requires at least one valid photo');
  const steps = [];
  for (const name of valid) {
    const startedAt = performance.now();
    const settled = waitForBenchmarkSettled(width, 'native-metal', 180000, name);
    await executeUICommand('goto', { name });
    const render = await settled;
    steps.push({
      name: `open-${name.split('.').pop().toLowerCase()}`,
      image: name,
      durationMs: performance.now() - startedAt,
      render: compactJourneyRender(render),
    });
  }
  // Exercise obsolete work and reversal, not just sequential cold opens.
  const burstStartedAt = performance.now();
  const burstNames = Array.from({length: 30}, (_, i) =>
    valid[(i < 15 ? i : 29 - i) % valid.length]);
  for (const name of burstNames.slice(0, -1)) {
    void executeUICommand('goto', { name });
    await new Promise((resolve) => setTimeout(resolve, 12));
  }
  const burstFinal = burstNames.at(-1);
  const burstSettled = waitForBenchmarkSettled(width, 'native-metal', 180000, burstFinal);
  await executeUICommand('goto', { name: burstFinal });
  const burstRender = await burstSettled;
  steps.push({name: 'burst-30-with-reversal', image: burstFinal,
    durationMs: performance.now() - burstStartedAt,
    render: compactJourneyRender(burstRender)});
  let recovery = null;
  if (corrupt) {
    const failedAt = performance.now();
    const failed = waitForRenderState(corrupt, 'error');
    await executeUICommand('goto', { name: corrupt });
    await failed;
    const failureMs = performance.now() - failedAt;
    const recoveredAt = performance.now();
    const recovered = waitForBenchmarkSettled(
      width, 'native-metal', 180000, valid[0]);
    await executeUICommand('goto', { name: valid[0] });
    const render = await recovered;
    recovery = {
      corrupt,
      failureMs,
      recoveredImage: valid[0],
      recoveryMs: performance.now() - recoveredAt,
      render: compactJourneyRender(render),
    };
  }
  const finalState = uiStateReport();
  if (finalState.render.state !== 'ready' ||
      finalState.render.backend !== 'native-metal') {
    throw new Error('RAW journey did not recover to a ready Metal presentation');
  }
  return { schema: 2, layer, images: valid, steps, recovery, finalState };
}

function scheduleProgressiveRender(scheduledAt, firstDelay = 0) {
  clearTimeout(renderTimer);
  const requestedWidth = requestedPreviewWidth();
  // Opening can use an accurate cached surface before requesting sharp detail.
  // Zooming an already visible photo still keeps its existing sharp pixels.
  const opening = S.presentedPhotoName !== cur()?.name || S.renderState !== 'ready';
  const width = opening || viewportRegionEnabled() ? requestedWidth
    : Math.min(requestedWidth, INTERACTIVE_PREVIEW_WIDTH);
  renderTimer = setTimeout(() => {
    lastInteractiveRenderAt = performance.now();
    doRender(scheduledAt, {
      width,
      requestedWidth,
      phase: opening ? 'navigation' : width === requestedWidth ? 'settled' : 'interactive',
    });
  }, firstDelay);
  clearTimeout(settleRenderTimer);
}

function renderFilm(debounce = 0) {
  markContinuousInput();
  scheduleProgressiveRender(performance.now(), debounce);
}

function renderPhysicalPreview() {
  markContinuousInput();
  const scheduledAt = performance.now();
  const delay = Math.max(0,
    (interactiveRenderIntervalMs ?? 1000 / 60) - (scheduledAt - lastInteractiveRenderAt));
  scheduleProgressiveRender(scheduledAt, delay);
}

async function doRender(scheduledAt = performance.now(), options = {}) {
  clearTimeout(prefetchTimer);
  // A pending helper is left alone. Its own generation guard drops a
  // superseded fetch, while the early return below happens before the
  // generation is bumped, so cancelling here stranded the sampling surface.
  const im = cur();
  if (!im) return;
  // Zoom/pan state can change before its animation-frame layout is applied.
  // Measure the current view before choosing the native source-pixel region.
  viewFrameScheduler.flush();
  readControls();
  const my = ++S.seq;
  const requestedWidth = options.requestedWidth || requestedPreviewWidth();
  let w = options.width || requestedWidth;
  if ($('pw').value === 'auto') automaticPreviewRequest = { name: im.name, width: requestedWidth };
  let phase = (options.phase || 'settled');
  if (window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__) {
    postNative('nativeBenchmarkProgress', {
      stage: 'render-start', generation: my, width: w,
      requestedWidth, phase,
    });
  }
  if (interactiveRenderPhoto !== im.name) {
    interactiveRenderPhoto = im.name;
    interactiveRenderIntervalMs = null;
  }
  const requestStartedAt = performance.now();
  const viewport = phase !== 'navigation' && w === requestedWidth ? requestedViewportRegion() : null;
  const hasAccuratePixels = S.renderState === 'ready' && S.presentedPhotoName === im.name &&
    S.previewDetail?.name === im.name && S.previewDetail.refining === false;
  $('rstat').textContent = tr('rendering…');
  $('rstat').className = 'busy';
  $('zoomwrap').setAttribute('aria-busy', 'true');
  if (!options.background) previewProgress.start(
    S.params.profile_enabled ? tr('Applying film…') : tr('Loading preview…'), my);
  try {
    const request = {
      name: im.name, params: { ...S.params }, w, engine: $('engine').value,
      optics: S.optics, heals: S.heals,
      ...gradeBakeRequest(S.grade, S.masks),
      client: CLIENT_ID, generation: my, priority: 'interactive',
      allow_draft: false,
      native: nativePreviewActive(),
      ...(viewport ? { viewport } : {}),
    };
    if (phase === 'navigation') {
      const cached = options.skipPresentationCache ? null : presentationCache.findPreview(im, request);
      w = cached?.width || Math.min(requestedWidth, INTERACTIVE_PREVIEW_WIDTH);
      request.w = w;
      phase = w < requestedWidth ? 'interactive' : 'settled';
    }
    const measureInteractiveRoundTrip = (viewport || w <= INTERACTIVE_PREVIEW_WIDTH) &&
      requestStartedAt - lastContinuousInputAt < FULL_RESOLUTION_SETTLE_MS;
    const requestedGradeKey = gradeBakeKey(request);
    clearTimeout(viewportRegionTimer);
    lastViewportRenderKey = JSON.stringify([im.name, viewport]);
    const presentationKey = renderRequestKey(im, request);
    const remembered = options.skipPresentationCache ? null : presentationCache.get(presentationKey);
    const m = remembered ? { ...remembered, cached: true }
      : await api('/api/render', request);
    m.previewGradeKey = requestedGradeKey;
    presentationCache.set(presentationKey, m);
    const responseAt = performance.now();
    if (measureInteractiveRoundTrip && !remembered && !m.cached && !m.error &&
        !m.cancelled && interactiveRenderPhoto === im.name) {
      interactiveRenderIntervalMs = adaptiveInteractiveInterval(
        interactiveRenderIntervalMs, responseAt - requestStartedAt);
    }
    if (window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__) {
      postNative('nativeBenchmarkProgress', {
        stage: 'render-response', generation: my, currentGeneration: S.seq,
        width: w, requestedWidth, phase, cancelled: Boolean(m.cancelled),
      });
    }
    if (my !== S.seq) return;
    if (requestedGradeKey !== gradeBakeKey(gradeBakeRequest(S.grade, S.masks))) {
      renderPhysicalPreview();
      return;
    }
    if (m.cancelled) {
      automaticPreviewRequest = null;
      previewProgress.finish();
      $('zoomwrap').setAttribute('aria-busy', 'false');
      $('rstat').textContent = '';
      $('rstat').className = '';
      return;
    }
    if (m.error) {
      automaticPreviewRequest = null;
      if (!options.background) previewProgress.finish({ error: tr('Could not render preview') });
      $('zoomwrap').setAttribute('aria-busy', 'false');
      $('rstat').textContent = tr('error: {mError}', {mError: m.error});
      $('rstat').className = '';
      if (S.renderState === 'pending' && S.renderName === im.name) {
        setRenderPresentation('error', im.name,
          previewFailureMessage(tr('Could not render this photo'), m.error));
      }
      return;
    }
    previewProgress.advance(4, my);
    if (Number.isFinite(m.match) && m.match > 0) S.matchFactor = m.match;
    if (Object.prototype.hasOwnProperty.call(m, 'lens_profile')) {
      S.lensProfile = m.lens_profile;
      syncOpticsPanel();
    }
    // The 1100px interaction result is a texture swap for the same oriented
    // photo, not a document resize. Keep the settled presentation frame while
    // it is visible, but allow rotations and photo navigation to change it.
    const nextGeometryKey = previewGeometryKey(im.name, S.params.rotate);
    const preservePresentationGeometry = shouldPreservePresentationGeometry(
      phase, S.presentedGeometryKey, nextGeometryKey);
    // A draft (embedded-camera) render only stands in while nothing accurate
    // is on screen. When this photo is already presented accurately, keep
    // those pixels and let the refinement replace them: swapping in a draft
    // flashes a different rendering on every zoom, crop, or panel change.
    const keepAccuratePixels = Boolean(m.refining) && hasAccuratePixels;
    let imageTiming = null;
    if (!keepAccuratePixels) {
      imageTiming = await setBaseImage(m, my, {
        preserveCanvasSize: preservePresentationGeometry,
      });
      if (my !== S.seq) return;
      if (imageTiming.failed && remembered) {
        presentationCache.delete(presentationKey);
        return doRender(scheduledAt, { ...options, skipPresentationCache: true });
      }
      if (imageTiming.failed) {
        automaticPreviewRequest = null;
        if (!options.background) previewProgress.finish({ error: tr('Could not display preview') });
        $('zoomwrap').setAttribute('aria-busy', 'false');
        $('rstat').textContent = imageTiming.error || tr('preview unavailable');
        $('rstat').className = '';
        setRenderPresentation('error', im.name,
          previewFailureMessage(tr('Could not display this photo'), imageTiming.error));
        return;
      }
      S.baseEditsBaked = Boolean(m.baseEditsBaked);
      S.previewDetail = { name: im.name, refining: Boolean(m.refining), requested: requestedWidth,
        renderedWidth: w,
        native: m.native || null,
        delivered: Math.max(+(m.native?.width || S.baseImg?.naturalWidth || w),
          +(m.native?.height || S.baseImg?.naturalHeight || 0)) };
      setRenderPresentation('ready', im.name);
      drawGrade();
    }
    const paintedAt = keepAccuratePixels ? performance.now()
      : (imageTiming.presentedAt || await afterVisiblePaint());
    if (my !== S.seq) return;
    // A usable preview is on screen. Higher resolution and RAW refinement are
    // background work and must not keep or restart the overlay.
    previewProgress.finish();
    imageTiming ||= {
      decodeMs: 0, uploadMs: 0, uploadedAt: paintedAt, presentation: 'draft-skipped',
    };
    const timing = {
      image: im.name,
      generation: my,
      width: w,
      requestedWidth,
      phase,
      cached: Boolean(m.cached),
      refining: Boolean(m.refining),
      engine: m.engine,
      backend: m.backend || null,
      viewport: m.native?.viewport || null,
      nativeSharedMemory: imageTiming.nativeSharedMemory || false,
      queueMs: requestStartedAt - scheduledAt,
      requestMs: responseAt - requestStartedAt,
      serverMs: !m.cached && Number.isFinite(+m.ms) ? +m.ms : null,
      residentMs: !m.cached && Number.isFinite(+m.resident_ms) ? +m.resident_ms : null,
      gpuMs: !m.cached && Number.isFinite(+m.gpu_ms) ? +m.gpu_ms : null,
      imageDecodeMs: imageTiming.decodeMs,
      textureUploadMs: imageTiming.uploadMs,
      paintAfterUploadMs: imageTiming.presentation === 'native-metal'
        ? (imageTiming.nativeGpuMs || 0) : paintedAt - imageTiming.uploadedAt,
      presentation: imageTiming.presentation || 'webgl',
      nativeFetchMs: imageTiming.nativeFetchMs ?? null,
      nativeGpuMs: imageTiming.nativeGpuMs ?? null,
      nativeTotalMs: imageTiming.nativeTotalMs ?? null,
      textureCacheHit: Boolean(imageTiming.textureCacheHit),
      presentationCacheHit: Boolean(remembered),
      serverQueueMs: +m.queue_ms || 0,
      totalMs: paintedAt - scheduledAt,
    };
    PERF.renders.push(timing);
    if (PERF.renders.length > 500) PERF.renders.splice(0, PERF.renders.length - 500);
    window.dispatchEvent(new CustomEvent('lighttable-rendered', { detail: timing }));
    syncBrowserOriginal(requestedWidth);
    const status = m.profile_enabled === false
      ? tr('Profile off · source')
      : (m.cached ? tr('Cached') : tr('{milliseconds} ms', { milliseconds: m.ms })) +
         ' · ' + (m.engine === 'rs' ? 'rust/gpu' : 'python');
    $('rstat').textContent = [status, m.refining ? tr('Refining RAW…') : ''].filter(Boolean).join(' · ');
    $('rstat').className = '';
    if (m.refining) {
      // Start accurate RAW work immediately after the useful first frame.
      // Rendering a large embedded-camera draft first delays demosaic and
      // creates a second temporary film result that will soon be replaced.
      const refinementRequest = { name: im.name, params: { ...request.params },
        w: requestedWidth, client: CLIENT_ID, generation: my };
      const ready = await waitForRawRefinement({
        request: () => api('/api/refine', refinementRequest),
        isCurrent: () => my === S.seq && cur()?.name === im.name,
      });
      if (ready) return doRender(scheduledAt, {
        width: requestedWidth, requestedWidth, phase: 'refinement', background: true,
      });
    } else if (phase === 'interactive' && w < requestedWidth) {
      const renderWhenIdle = () => {
        const idleFor = performance.now() - lastContinuousInputAt;
        if (idleFor < FULL_RESOLUTION_SETTLE_MS) {
          settleRenderTimer = setTimeout(
            renderWhenIdle, FULL_RESOLUTION_SETTLE_MS - idleFor);
          return;
        }
        if (my === S.seq && cur() && cur().name === im.name) {
          doRender(scheduledAt, {
            width: requestedWidth,
            requestedWidth,
            phase: 'settled', background: true,
          });
        }
      };
      settleRenderTimer = setTimeout(
        renderWhenIdle, FULL_RESOLUTION_SETTLE_MS);
    } else $('zoomwrap').setAttribute('aria-busy', 'false');
    prefetch(m.refining || phase === 'interactive');
  } catch (e) {
    const failedAt = performance.now();
    if (my === S.seq) automaticPreviewRequest = null;
    const failure = {
      image: im?.name || null,
      width: w,
      requestedWidth,
      phase,
      presentation: 'request-failed',
      totalMs: failedAt - scheduledAt,
      error: String(e?.message || e),
    };
    PERF.renders.push(failure);
    window.dispatchEvent(new CustomEvent('lighttable-rendered', { detail: failure }));
    if (window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__) {
      postNative('nativeBenchmarkProgress', {
        stage: 'render-error', generation: my, width: w,
        requestedWidth, phase, error: failure.error,
      });
    }
    if (my === S.seq) {
      if (!options.background) previewProgress.finish({ error: tr('Could not finish preview') });
      $('zoomwrap').setAttribute('aria-busy', 'false');
      $('rstat').textContent = failure.error || tr('Could not finish preview');
      $('rstat').className = '';
      if (S.renderState === 'pending' && S.renderName === im.name) {
        setRenderPresentation('error', im.name,
          previewFailureMessage(tr('Could not finish this preview'), failure.error));
      }
    }
  }
}

function nativePreviewCanDraw(state) {
  return state === 'pending' || state === 'ready';
}

function nativeViewportPayload() {
  const canvas = $('cv').getBoundingClientRect();
  const wrap = $('zoomwrap').getBoundingClientRect();
  const frame = $('cmp').getBoundingClientRect();
  // The native Metal view sits below WebKit, so CSS overflow cannot clip it.
  // Intersect with the visible photo frame explicitly; after crop is
  // committed the source canvas is intentionally larger than that frame.
  const left = Math.max(wrap.left, frame.left);
  const top = Math.max(wrap.top, frame.top);
  const right = Math.min(wrap.right, frame.right);
  const bottom = Math.min(wrap.bottom, frame.bottom);
  const clip = {
    left, top,
    width: Math.max(0, right - left),
    height: Math.max(0, bottom - top),
  };
  const rect = (value) => ({
    x: value.left, y: value.top, width: value.width, height: value.height,
  });
  return {
    canvas: rect(canvas), clip: rect(clip),
    visible: NATIVE_PREVIEW &&
      !useWebGLPreview(nativePreviewActive(), S.presentedBackend) &&
      S.viewMode === 'detail' &&
      nativePreviewCanDraw(S.renderState) && canvas.width > 0 && canvas.height > 0,
  };
}

let lastNativeViewportKey = '';
let nativeHelperTimer = null;
function nativePreviewActive() {
  return NATIVE_PREVIEW;
}

function useWebGLPreview(nativeActive, presentedBackend) {
  return !nativeActive && presentedBackend === 'webgl';
}

let nativePreviewWasActive = null;
function nativePreviewNeedsRefresh(wasActive, isActive, presentedBackend) {
  return wasActive === false && isActive && presentedBackend !== 'native-metal';
}

function syncPreviewBackend() {
  if (!NATIVE_PREVIEW) return;
  const native = nativePreviewActive();
  document.documentElement.classList.toggle(
    'webgl-preview-fallback', useWebGLPreview(native, S.presentedBackend));
  if (native !== nativePreviewWasActive) {
    const previousNative = nativePreviewWasActive;
    nativePreviewWasActive = native;
    syncBrowserOriginal();
    if (native) {
      postNative('nativeCompare', { position: previewSourceX(renderedComparePosition()) });
    } else {
      $('cmp').style.setProperty('--clip',
        (100 - previewSourceX(renderedComparePosition()) * 100) + '%');
    }
    if (nativePreviewNeedsRefresh(
      previousNative, native, S.presentedBackend) && cur()) {
      setRenderPresentation('pending', cur().name);
      renderFilm(0);
    }
    scheduleNativeViewportLayout();
  }
}

function flushNativeViewportLayout() {
  const payload = nativeViewportPayload();
  const key = JSON.stringify(payload);
  if (key !== lastNativeViewportKey) {
    lastNativeViewportKey = key;
    postNative('nativeViewportLayout', payload);
  }
}
const nativeLayoutScheduler = createFrameScheduler(flushNativeViewportLayout);
function scheduleNativeViewportLayout() {
  if (!NATIVE_PREVIEW) return;
  if (zoomMotion.active) {
    // Keep Metal on this animation frame rather than one RAF behind overlays.
    nativeLayoutScheduler.cancel();
    flushNativeViewportLayout();
  } else nativeLayoutScheduler.request();
}

function scheduleNativeHelper(url, generation) {
  if (!url) return;
  clearTimeout(nativeHelperTimer);
  const whenIdle = () => {
    if (generation !== S.seq) return;
    const remaining = 180 - (performance.now() - lastContinuousInputAt);
    if (remaining > 0) {
      nativeHelperTimer = setTimeout(whenIdle, remaining);
      return;
    }
    // Sampling/scopes must not interrupt a drag or resize the visible anchor.
    setWebGLBaseImage(url, {
      preserveCanvasSize: true, forceWebGLDraw: true, generation,
    }).catch(() => {});
  };
  nativeHelperTimer = setTimeout(whenIdle, 180);
}

function setNativeBaseImage(render, generation, { preserveCanvasSize = false } = {}) {
  for (const [olderGeneration, pending] of nativePreviewPending) {
    if (olderGeneration < generation) {
      nativePreviewPending.delete(olderGeneration);
      const now = performance.now();
      pending.resolve({
        decodeMs: 0, uploadMs: 0, uploadedAt: now, presentedAt: now,
        presentation: 'native-cancelled', cancelled: true,
      });
    }
  }
  const surface = render.native || (render.img ? {
    url: render.img, format: 'image', width: 0, height: 0,
    rowBytes: 0, headerBytes: 0,
  } : null);
  if (!surface) return Promise.resolve({
    decodeMs: 0, uploadMs: 0, uploadedAt: performance.now(),
    presentedAt: performance.now(), presentation: 'native-missing',
    failed: true, error: tr('Native preview data is missing'),
  });

  S.nativeViewport = surface.viewport || null;
  if (surface.viewport) S.viewportSourceGeometry = { key: viewportSourceGeometryKey(),
    width: surface.viewport.fullWidth, height: surface.viewport.fullHeight };
  const canvasWidth = surface.viewport?.fullWidth || surface.width;
  const canvasHeight = surface.viewport?.fullHeight || surface.height;
  if (canvasWidth > 0 && canvasHeight > 0 && !preserveCanvasSize) {
    if ($('cv').width !== canvasWidth || $('cv').height !== canvasHeight) {
      // Keep the old drawable at its existing size until nativePreview installs
      // the new texture and viewport together. Layout messages can otherwise
      // stretch the previous orientation while this surface is still loading.
      postNative('nativeNavigate', { generation });
      S.maskTextureDirty = true;
      $('cv').width = canvasWidth;
      $('cv').height = canvasHeight;
    }
    applyCropVisual();
    cropFrameScheduler.flush();
    if (S.maskTextureDirty) drawGrade();
  }
  scheduleNativeViewportLayout();

  // Install the mask state with the source swap, including baked-to-live
  // transitions, so the first drawable cannot reuse the previous mask atlas.
  if (S.maskTextureDirty || !packedMaskData) {
    packedMaskData = buildMaskTexture();
    S.maskTextureDirty = false;
  }

  // Histogram, WB sampling, and reference matching retain a 256px WebGL
  // helper, generated only after interaction settles. A response without a
  // helper still presents a JPEG surface, which seeds sampling just as well.
  if (!surface.viewport) scheduleNativeHelper(render.helper || render.img, generation);

  return new Promise((resolve) => {
    nativePreviewPending.set(generation, { resolve });
    if (window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__) {
      postNative('nativeBenchmarkProgress', {
        stage: 'native-request', generation,
        width: surface.width, height: surface.height,
      });
    }
    postNative('nativePreview', {
      generation, surface, grade: render.gradeEditsBaked ? GRADE_DEFAULTS : S.grade,
      masks: nativeMaskPayload(packedMaskData),
      ...nativeEditsPayload(Boolean(render.baseEditsBaked)),
      original: {
        url: originalPreviewURL(), format: 'image', width: 0, height: 0,
        rowBytes: 0, headerBytes: 0,
      },
      ...nativeViewportPayload(),
    });
  });
}

function originalPreviewURL(requestedWidth = requestedPreviewWidth()) {
  const im = cur();
  if (!im) return '';
  // Compare must resolve the same detail as the edited preview, including 1:1.
  const width = requestedWidth;
  return `/api/orig?name=${encodeURIComponent(im.name)}` +
    `&w=${width}&rot=${S.params.rotate || 0}&quality=full&v=2` +
    `&key=${encodeURIComponent(im.fileKey || im.mtime || '')}`;
}

let browserOriginal = null;
let browserOriginalTextureURL = null;
function installBrowserOriginal() {
  if ((!S.compareActive && !S.holdBefore) || nativePreviewActive() ||
      !S.gl || !browserOriginal?.image?.complete ||
      !browserOriginal.image.naturalWidth ||
      browserOriginal.url !== S.originalImageName ||
      browserOriginalTextureURL === browserOriginal.url) return;
  S.gl.setOriginalImage(browserOriginal.image);
  browserOriginalTextureURL = browserOriginal.url;
  S.gl.drawCompare(previewSourceX(renderedComparePosition()));
}

function syncBrowserOriginal(requestedWidth = requestedPreviewWidth()) {
  $('orig').removeAttribute('src');
  const url = originalPreviewURL(requestedWidth);
  if (!url || nativePreviewActive() || (!S.compareActive && !S.holdBefore)) return;
  if (S.originalImageName === url && browserOriginal?.url === url) {
    installBrowserOriginal();
    return;
  }
  S.originalImageName = url;
  browserOriginalTextureURL = null;
  S.gl?.clearOriginalImage();
  const image = new Image();
  browserOriginal = { url, image };
  image.onload = () => installBrowserOriginal();
  image.onerror = () => {
    if (browserOriginal?.url === url) browserOriginal = null;
  };
  image.src = url;
}

function rememberPresentedRender(state, identity, backend, timing) {
  if (timing?.failed) {
    state.presentedRenderKey = null;
    state.presentedBackend = null;
    return;
  }
  state.presentedRenderKey = identity || null;
  state.presentedBackend = backend;
}

async function setBaseImage(render, generation, { preserveCanvasSize = false } = {}) {
  const previousGradeState = [S.presentedGradeKey, S.gradeEditsBaked];
  S.presentedGradeKey = render.previewGradeKey ?? null;
  S.gradeEditsBaked = Boolean(render.gradeEditsBaked);
  S.maskTextureDirty = true;
  const native = nativePreviewActive();
  const backend = native ? 'native-metal' : 'webgl';
  const identity = render.key || render.native?.url || render.img;
  if (S.renderState !== 'pending' && identity && identity === S.presentedRenderKey &&
      backend === S.presentedBackend) {
    drawGrade();
    const now = performance.now();
    return { decodeMs: 0, uploadMs: 0, uploadedAt: now, presentedAt: now,
      presentation: backend, deduplicated: true };
  }
  S.previewLoadGeneration = generation;
  let timing;
  try {
    timing = native
      ? await setNativeBaseImage(render, generation, { preserveCanvasSize })
      : await setWebGLBaseImage(render.img, { generation, preserveCanvasSize, cacheKey: identity });
    if (generation === S.seq && timing.failed) {
      [S.presentedGradeKey, S.gradeEditsBaked] = previousGradeState;
    }
  } finally {
    if (S.previewLoadGeneration === generation) S.previewLoadGeneration = null;
  }
  if (generation === S.seq && !timing.cancelled) rememberPresentedRender(S, identity, backend, timing);
  return timing;
}

function setWebGLBaseImage(dataUri, {
  preserveCanvasSize = false, forceWebGLDraw = false, generation = null, cacheKey = null,
} = {}) {
  return new Promise((res) => {
    const startedAt = performance.now();
    const cached = S.gl?.cachedImage(cacheKey);
    const img = cached || new Image();
    const present = async () => {
      // A helper requested while idle may finish after another drag began.
      while (forceWebGLDraw && nativePreviewActive() && generation === S.seq &&
             performance.now() - lastContinuousInputAt < 180) {
        await new Promise((resolve) => setTimeout(resolve, 180));
      }
      if (generation !== null && generation !== S.seq) {
        const cancelledAt = performance.now();
        return res({ decodeMs: cancelledAt - startedAt, uploadMs: 0,
          uploadedAt: cancelledAt, cancelled: true });
      }
      S.baseImg = img;
      if (!S.gl) {
        try { S.gl = new GradeRenderer($('cv')); }
        catch (e) {
          toast(tr("WebGL unavailable: {eMessage}", {eMessage: e.message}));
          return res({ decodeMs: performance.now() - startedAt, uploadMs: 0,
            uploadedAt: performance.now(), failed: true,
            error: previewFailureMessage(tr('WebGL preview could not be initialized'), e) });
        }
        browserOriginalTextureURL = null;
        browserReferenceTextureURL = null;
      }
      const uploadStartedAt = performance.now();
      const { textureCacheHit } = S.gl.setImage(img, { resizeCanvas: !preserveCanvasSize, cacheKey });
      installBrowserOriginal();
      installBrowserReference();
      S.maskTextureDirty = true;
      const uploadedAt = performance.now();
      if (forceWebGLDraw) {
        previewFrameScheduler.request({ grade: true, forceWebGL: true });
      } else drawGrade();
      scheduleHistogram(true);
      applyCropVisual();
      // The upload changes the canvas orientation now; do not leave its CSS
      // frame at the old aspect until the next animation-frame callback.
      cropFrameScheduler.flush();
      res({ decodeMs: uploadStartedAt - startedAt,
        uploadMs: uploadedAt - uploadStartedAt, uploadedAt, textureCacheHit });
    };
    if (cached) { void present(); return; }
    img.onload = present;
    img.onerror = () => {
      const failedAt = performance.now();
      res({ decodeMs: failedAt - startedAt, uploadMs: 0, uploadedAt: failedAt,
        failed: true, error: tr('Preview image could not be loaded or decoded') });
    };
    img.src = dataUri;
  });
}

/* ------------------------------------------------------ reference matching */
let browserReferenceTextureURL = null;
let pendingNativeReferenceData = null;

function referenceCompositeSettings() {
  return {
    active: Boolean(S.referenceUrl),
    mode: $('referenceMode').value,
    amount: +$('referenceOpacity').value,
    scale: +$('referenceScale').value,
    x: +$('referenceX').value / 100,
    y: +$('referenceY').value / 100,
  };
}

function installBrowserReference() {
  const image = $('referenceImg');
  if (!S.gl || !S.referenceUrl || !image.complete || !image.naturalWidth ||
      browserReferenceTextureURL === S.referenceUrl) return;
  S.gl.setReferenceImage(image);
  browserReferenceTextureURL = S.referenceUrl;
}

function updateReferenceCompositeNow(drawBrowser = true) {
  const settings = referenceCompositeSettings();
  installBrowserReference();
  if (S.gl) {
    if (drawBrowser) S.gl.drawReference(settings);
    else S.gl.setReferenceSettings(settings);
  }
  const payload = { ...settings };
  if (pendingNativeReferenceData) {
    payload.data = pendingNativeReferenceData;
    pendingNativeReferenceData = null;
  }
  postNative('nativeReference', payload, true);
}

function updateReferenceOverlay() {
  const image = $('referenceImg');
  image.hidden = true;
  const opacity = +$('referenceOpacity').value;
  const scale = +$('referenceScale').value;
  const x = +$('referenceX').value;
  const y = +$('referenceY').value;
  $('referenceOpacityV').textContent = Math.round(opacity * 100) + '%';
  $('referenceScaleV').textContent = Math.round(scale * 100) + '%';
  $('referenceXV').textContent = x.toFixed(1) + '%';
  $('referenceYV').textContent = y.toFixed(1) + '%';
  markContinuousInput();
  previewFrameScheduler.request({ reference: true });
}

function srgbToLab(r, g, b) {
  const linear = [r, g, b].map((value) => {
    value /= 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  const x = (linear[0] * 0.4124564 + linear[1] * 0.3575761 + linear[2] * 0.1804375) / 0.95047;
  const y = linear[0] * 0.2126729 + linear[1] * 0.7151522 + linear[2] * 0.072175;
  const z = (linear[0] * 0.0193339 + linear[1] * 0.119192 + linear[2] * 0.9503041) / 1.08883;
  const f = (value) => value > 0.008856 ? Math.cbrt(value) : 7.787 * value + 16 / 116;
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
}

function measureReferenceStats() {
  const reference = $('referenceImg');
  if (!S.referenceUrl || !S.baseImg || !reference.complete || !S.gl) return null;
  // The visible WebGL canvas uses preserveDrawingBuffer:false for speed, so
  // copying it later can legally return black. GradeRenderer.sample() redraws
  // the current edit into its small readback framebuffer and returns the real
  // pixels without retaining or downloading the full-resolution canvas.
  refreshWebGLSamplingSurface();
  const sampled = S.gl.sample();
  if (!sampled) return null;
  const { px: current, w: width, h: height } = sampled;
  const referenceCanvas = document.createElement('canvas');
  referenceCanvas.width = width;
  referenceCanvas.height = height;
  const referenceContext = referenceCanvas.getContext('2d', { willReadFrequently: true });
  referenceContext.save();
  referenceContext.translate(
    width / 2 + width * (+$('referenceX').value) / 100,
    height / 2 + height * (+$('referenceY').value) / 100);
  const scale = +$('referenceScale').value;
  referenceContext.scale(scale, scale);
  referenceContext.drawImage(reference, -width / 2, -height / 2, width, height);
  referenceContext.restore();
  const target = referenceContext.getImageData(0, 0, width, height).data;
  const currentMean = [0, 0, 0];
  const targetMean = [0, 0, 0];
  let absolute = 0; let deltaE = 0; let count = 0;
  for (let pixel = 0; pixel < width * height; pixel += 4) {
    const targetIndex = pixel * 4;
    if (target[targetIndex + 3] === 0) continue;
    // readPixels is bottom-up; canvas ImageData is top-down.
    const x = pixel % width;
    const y = Math.floor(pixel / width);
    const currentIndex = ((height - 1 - y) * width + x) * 4;
    for (let channel = 0; channel < 3; channel++) {
      currentMean[channel] += current[currentIndex + channel];
      targetMean[channel] += target[targetIndex + channel];
      absolute += Math.abs(current[currentIndex + channel] - target[targetIndex + channel]);
    }
    const a = srgbToLab(current[currentIndex], current[currentIndex + 1], current[currentIndex + 2]);
    const b = srgbToLab(target[targetIndex], target[targetIndex + 1], target[targetIndex + 2]);
    deltaE += Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
    count++;
  }
  if (!count) return null;
  for (let channel = 0; channel < 3; channel++) {
    currentMean[channel] /= count; targetMean[channel] /= count;
  }
  const luma = (rgb) => (rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722) / 255;
  return {
    currentMean, targetMean,
    currentLuma: luma(currentMean), targetLuma: luma(targetMean),
    mae: absolute / (count * 3), deltaE: deltaE / count,
  };
}

function showReferenceMetrics(stats) {
  if (!stats) return $('referenceMetrics').textContent = tr("Reference could not be measured.");
  S.referenceStats = stats;
  const rgb = (values) => values.map((value) => Math.round(value)).join(' / ');
  $('referenceMetrics').textContent =
    tr("Mean RGB · edit {value} · reference {value2}\nLuminance · edit {value3} · reference {value4}\nMean absolute error · {value5} / 255\nApprox. mean ΔE76 · {value6}", {value: rgb(stats.currentMean), value2: rgb(stats.targetMean), value3: stats.currentLuma.toFixed(3), value4: stats.targetLuma.toFixed(3), value5: stats.mae.toFixed(1), value6: stats.deltaE.toFixed(1)});
}

$('referenceFile').addEventListener('change', () => {
  const file = $('referenceFile').files[0];
  if (!file) return;
  if (S.referenceUrl) URL.revokeObjectURL(S.referenceUrl);
  S.referenceUrl = URL.createObjectURL(file);
  browserReferenceTextureURL = null;
  const reader = new FileReader();
  reader.onload = () => {
    pendingNativeReferenceData = String(reader.result || '').split(',').pop() || null;
    updateReferenceOverlay();
  };
  reader.readAsDataURL(file);
  $('referenceImg').onload = () => {
    installBrowserReference();
    updateReferenceOverlay();
    showReferenceMetrics(measureReferenceStats());
  };
  $('referenceImg').src = S.referenceUrl;
});
const REF_DEFAULTS = { referenceOpacity: '0.5', referenceScale: '1', referenceX: '0', referenceY: '0' };
['referenceOpacity', 'referenceScale', 'referenceX', 'referenceY'].forEach((id) => {
  const input = $(id);
  input.addEventListener('input', updateReferenceOverlay);
  const resetRef = () => {
    input.value = REF_DEFAULTS[id];
    updateReferenceOverlay();
  };
  input.addEventListener('dblclick', resetRef);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetRef);
});
$('referenceMode').addEventListener('change', updateReferenceOverlay);
$('measureReference').onclick = () => showReferenceMetrics(measureReferenceStats());
$('clearReference').onclick = () => {
  if (S.referenceUrl) URL.revokeObjectURL(S.referenceUrl);
  S.referenceUrl = null; S.referenceStats = null;
  pendingNativeReferenceData = null;
  browserReferenceTextureURL = null;
  S.gl?.clearReferenceImage();
  postNative('nativeReference', { active: false, clear: true }, true);
  $('referenceFile').value = ''; $('referenceImg').removeAttribute('src');
  $('referenceImg').hidden = true;
  $('referenceMetrics').textContent = tr("Load a scan, align it, then measure.");
};
$('startReferenceMatch').onclick = () => {
  const stats = measureReferenceStats();
  if (!stats) return toast(tr("Load and align a reference first"));
  pushUndo();
  const exposureDelta = clamp(Math.log2(
    Math.max(stats.targetLuma, 1e-4) / Math.max(stats.currentLuma, 1e-4)), -2, 2);
  S.params.auto_exposure = false;
  S.params.exposure_ev = clamp((+S.params.exposure_ev || 0) + exposureDelta, -3, 3);
  const raw = isRawInput();
  const currentWarmth = stats.currentMean[0] / Math.max(stats.currentMean[2], 1);
  const targetWarmth = stats.targetMean[0] / Math.max(stats.targetMean[2], 1);
  const currentMagenta = (stats.currentMean[0] + stats.currentMean[2]) /
    Math.max(2 * stats.currentMean[1], 1);
  const targetMagenta = (stats.targetMean[0] + stats.targetMean[2]) /
    Math.max(2 * stats.targetMean[1], 1);
  if (raw) {
    S.params.wb_mode = 'custom';
    S.params.wb_temperature = clamp((+S.params.wb_temperature || 5500) *
      Math.exp(Math.log(targetWarmth / currentWarmth) * 0.35), 2000, 50000);
    S.params.wb_tint = clamp((+S.params.wb_tint || 0) +
      Math.log(targetMagenta / currentMagenta) * 0.35, -1, 1);
  } else {
    S.params.print_y_filter_shift = clamp((+S.params.print_y_filter_shift || 0) +
      (stats.targetMean[2] - stats.currentMean[2]) / 16, -20, 20);
    S.params.print_m_filter_shift = clamp((+S.params.print_m_filter_shift || 0) +
      (stats.currentMean[1] - stats.targetMean[1]) / 16, -20, 20);
  }
  syncControls(); saveState(); renderFilm(0);
  toast(tr("Physical starting match applied"));
};

function isStateLoaded(im) {
  return !S.catalogEnabled || Boolean(im?.stateLoaded) || im?.hasEdits === false;
}

const _pendingStateFetches = new Map();
function prefetchState(im) {
  if (!im || isStateLoaded(im)) return Promise.resolve(im);
  if (_pendingStateFetches.has(im.name)) return _pendingStateFetches.get(im.name);
  const duringLoad = {};
  im.stateLoadEdits = duringLoad;
  const p = fetch(`/api/state?name=${encodeURIComponent(im.name)}`)
    .then((r) => r.json())
    .then((state) => {
      _pendingStateFetches.delete(im.name);
      if (!state.error) {
        Object.assign(im, normalizeLibraryImage({ ...im, ...state, ...duringLoad }, true));
        im.stateLoaded = true;
      }
      return im;
    })
    .catch(() => {
      _pendingStateFetches.delete(im.name);
      return im;
    });
  p.finally(() => { if (im.stateLoadEdits === duringLoad) delete im.stateLoadEdits; });
  _pendingStateFetches.set(im.name, p);
  return p;
}

let prefetchTimer = null;
let lastNavigationDirection = 1;

async function prefetchImage(target, epoch, generation, detail = false) {
  const current = () => epoch === navigationGeneration && generation === S.seq;
  if (!target || !current()) return;
  try {
    await prefetchState(target);
    if (!current()) return;
    const params = normalizeFilmParams(target.params);
    const width = requestedPreviewWidth(target, params, target.crop || null);
    const request = {
      name: target.name,
      params,
      optics: target.optics || OPTICS_DEFAULTS,
      heals: target.heals || [],
      ...gradeBakeRequest(target.grade || GRADE_DEFAULTS, target.masks || []),
      w: detail ? width : Math.min(width, INTERACTIVE_PREVIEW_WIDTH),
      engine: $('engine').value,
      client: CLIENT_ID, generation, priority: 'prefetch',
      allow_draft: false,
      native: nativePreviewActive(),
    };
    const key = renderRequestKey(target, request);
    const result = presentationCache.get(key) || await api('/api/render', request);
    presentationCache.set(key, result);
    if (current() && result.native && !result.cancelled) {
      postNative('nativePreload', { surface: result.native, epoch });
    }
  } catch (_) { /* A speculative miss must not interrupt navigation. */ }
}

function prefetch(refining = false) {
  clearTimeout(prefetchTimer);
  if (refining) return;
  const ordered = visible();
  const visibleIndex = ordered.indexOf(cur());
  if (visibleIndex < 0) return;
  const primary = ordered[visibleIndex + lastNavigationDirection];
  const secondary = ordered[visibleIndex - lastNavigationDirection];
  if (!primary && !secondary) return;

  const epoch = navigationGeneration, generation = S.seq;
  prefetchTimer = setTimeout(async () => {
    // Warm both useful first frames before larger work. Each step rechecks the
    // photo and render generation so navigation, zoom, or edits stop the batch.
    for (const detail of [false, true]) {
      for (const target of [primary, secondary]) {
        await prefetchImage(target, epoch, generation, detail);
      }
    }
  }, 80);
}

/* ----------------------------------------------------------------- state */

/* Persistent history is fed from the same commit point as the state save, but
 * only when the edit actually changed: a rating or a flag is not an edit step,
 * and recording one would bury the real edits in noise. */
let _lastHistorySnapshot = '';
const PANE_STEP_LABELS = {
  editPane: tr("Light and colour"), filmPane: tr("Film"), maskPane: tr("Masking"),
  healPane: tr("Remove"), lensPane: tr("Lens and geometry"), cropPane: tr("Crop"),
  presetsPane: tr("Preset"), versionsPane: tr("Version"), matchPane: tr("Reference match"),
};

function editHistorySnapshot() {
  return JSON.stringify({ params: S.params, grade: S.grade, crop: S.crop,
    masks: serializableMasks(), heals: S.heals, optics: S.optics, preset: S.preset || null });
}

let editRecovery = null;
let editRecoveryReady = false;
let editRecoveryIssue = null;
const deferredEditRecovery = new Map();
let deferredRecoveryRefreshIssue = null;
const journalRequests = new Map();
window.addEventListener('lighttable-edit-journal', ({detail}) => {
  const request = journalRequests.get(detail.id);
  if (!request) return;
  journalRequests.delete(detail.id); clearTimeout(request.timer);
  if (detail.error) request.reject(new Error(detail.error));
  else request.resolve(detail.result);
});
function nativeJournalRequest(body) {
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      journalRequests.delete(id); reject(new Error(tr("Edit recovery did not respond")));
    }, 10000);
    journalRequests.set(id, {resolve, reject, timer});
    if (!sendNative('editJournal', {...body, id})) {
      journalRequests.delete(id); clearTimeout(timer);
      reject(new Error(tr("Desktop edit recovery is unavailable")));
    }
  });
}
function renderEditSaveStatus(status) {
    const recoveryError = editRecoveryIssue || status.recoveryError;
    const label = $('editSaveStatus');
    label.textContent = status.state === 'error' ? tr("Edits not saved") : status.state === 'saving' ? tr("Saving…") : recoveryError ? tr("Saved · recovery needs attention") : tr("Saved");
    label.dataset.state = status.state;
    label.title = status.state === 'error' ? tr("Retry to save your changes. Local recovery is retained when available.") : recoveryError ? recoveryError.message : '';
    $('retryEditSave').hidden = status.state !== 'error' && !recoveryError;
}
function updateEditRecoveryHealth(error) {
  editRecoveryIssue = error;
  renderEditSaveStatus(editSaveQueue.getStatus());
}
const editSaveQueue = createEditSaveQueue({
  journal: {
    put(name, token, payload) {
      if (!editRecoveryReady) throw new Error(tr("Edit recovery is not ready"));
      return editRecovery.put(name, token, payload);
    },
    remove(name, token) { return editRecovery?.remove(name, token); },
  },
  async send(name, payload) {
    const result = await api('/api/state', {...payload.state, ...(payload.expectedRecoverySourceKey
      ? {expectedRecoverySourceKey: payload.expectedRecoverySourceKey} : {})});
    if (!result?.ok || result.error) throw new Error(((result?.error || tr("Could not save edits"))));
    const image = S.images.find((item) => item.name === name);
    if (image) invalidateEditedThumbnail(image);
    if (payload.history) {
      HISTORY?.record(name, payload.history.label, payload.history.state);
      if (await HISTORY?.flush(name) === false) throw new Error(tr("Could not save edit history"));
    }
  },
  onStatus: () => updateEditSaveStatus(),

});
let historySaveStatus = { pendingNames: [], failedNames: [], error: null };
function updateEditSaveStatus() {
  const status = editSaveQueue.getStatus();
  const state = status.state === 'error' || historySaveStatus.error ? 'error'
    : status.state === 'saving' || historySaveStatus.pendingNames.length ? 'saving' : 'saved';
  renderEditSaveStatus({...status, state});
}

async function flushEditSaves() {
  try {
    await editSaveQueue.flush();
    if (await HISTORY?.flush() === false) throw new Error(tr("Could not save edit history"));
    return true;
  }
  catch { toast(tr("Edits could not be saved. Use Retry save before continuing.")); return false; }

}

const closeBarrier = createCloseBarrier({
  capture: () => saveState(), flush: flushEditSaves,
  setBlocked: blocked => { document.body.inert = blocked; },
});
window.lightTablePrepareToClose = () => closeBarrier.prepare();
window.lightTableCancelClose = () => closeBarrier.cancel();
const DESKTOP_UPDATES = installDesktopUpdates({
  prepare: () => closeBarrier.prepare(), cancel: () => closeBarrier.cancel(), onError: toast,
});

async function refreshDeferredEditRecovery() {
  let refreshFailed = false;
  for (const [name, sourceKey] of deferredEditRecovery) {
    if (editSaveQueue.getPending(name)) continue;
    const image = S.images.find(item => item.name === name);
    if (!image || await reconcilePeerSave(image, sourceKey)) deferredEditRecovery.delete(name);
    else if (!editSaveQueue.getPending(name)) refreshFailed = true;
  }
  if (refreshFailed) {
    deferredRecoveryRefreshIssue = new Error(tr('Recovered edits are saved, but their display could not refresh. Retry to reload them.'));
    updateEditRecoveryHealth(deferredRecoveryRefreshIssue);
  } else if (!deferredEditRecovery.size && deferredRecoveryRefreshIssue) {
    if (editRecoveryIssue === deferredRecoveryRefreshIssue) updateEditRecoveryHealth(null);
    deferredRecoveryRefreshIssue = null;
  }
}

$('retryEditSave').onclick = async () => {
  try {
    if (!editRecoveryReady && editRecovery) {
      try { await editRecovery.list(); editRecoveryReady = true; }
      catch (error) { updateEditRecoveryHealth(error); }
    }
    if (await (HISTORY?.retry ? HISTORY.retry() : HISTORY?.flush()) === false) {
      throw new Error(tr("Could not save photo history"));
    }
    await editSaveQueue.retry();
    await refreshDeferredEditRecovery();
    toast(editRecoveryIssue ? tr('Edits saved; local recovery still needs attention') : tr('Edits saved'));
  }
  catch { toast(tr("Still unable to save. Your changes are kept in this window.")); }

};
let _controlDirty = false;
function markControlDirty() {
  _controlDirty = true;
  globalThis._controlDirty = true;
}
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('input', (e) => {
    if (e.target?.closest?.('#panel')) markControlDirty();
  }, true);
}

window.addEventListener('beforeunload', (event) => {
  if (globalThis._controlDirty && typeof saveState === 'function') saveState();
  if (!editSaveQueue.getStatus().pendingNames.length && !HISTORY?.hasPending) return;
  void editSaveQueue.flush().then(() => HISTORY?.flush()).catch(() => {});
  event.preventDefault();
  event.returnValue = '';
});

function saveState(immediate = false) {
  _controlDirty = false;
  globalThis._controlDirty = false;
  if (window.__LIGHTTABLE_BENCHMARK__ && window.__LIGHTTABLE_NATIVE_JOURNEY_LAYER__ !== 'visual-review') return Promise.resolve(true);
  const im = cur();
  if (!im || S.editingName !== im.name) return immediate ? flushEditSaves() : Promise.resolve(true);
  readControls();
  const previousPreset = S.preset;
  S.preset = reconcilePresetAdjustment(S.preset, presetEditState(S));
  if (previousPreset?.id !== S.preset?.id || (previousPreset && !S.preset)) {
    PRESET_BROWSER?.select();
  }
  const edits = JSON.parse(editHistorySnapshot());
  const wasEdited = photoHasEdits(im);
  Object.assign(im, cloneValue(edits));
  const current = JSON.stringify(edits);
  const pending = editSaveQueue.getPending(im.name);
  const history = current !== _lastHistorySnapshot
    ? { label: PANE_STEP_LABELS[S.activePane] || 'Edit', state: edits }
    : pending?.history;
  _lastHistorySnapshot = current;
  const state = { name: im.name, status: im.status, rating: im.rating,
    label: cleanLabel(im.label), ...edits,
    keywords: im.keywords || [], versions: im.versions || [] };
  if (im.stateLoadEdits) Object.assign(im.stateLoadEdits, cloneValue(state));
  editSaveQueue.enqueue(im.name, { state, history, sourceKey: im.recoverySourceKey || null }, { immediate });
  if (wasEdited !== photoHasEdits(im)) {
    invalidateVisibleCache();
    _stripKey = _gridKey = '';
    refreshLists();
  }
  return immediate ? flushEditSaves() : Promise.resolve(true);
}

/* ------------------------------------------------------------- filmstrip */
function photoMatchesQuery(im, query) {
  const exif = _exifCache.get(im.sourceName || im.name);
  return matchesPhotoQuery(im, query, exif);
}

function photoMatchesRules(im, rules = {}) {
  const exif = _exifCache.get(im.sourceName || im.name);
  return matchesPhotoRules(im, rules, exif);
}

function activeCollection() {
  return (S.library.collections || []).find((item) => item.id === S.activeCollection) || null;
}

/* Preferences can arrive before the library payload. Keep the saved
 * collection until the collections list exists to validate it against,
 * instead of discarding it because the list still looks empty. */
function applyPendingActiveCollection() {
  if (S.pendingActiveCollection == null) return;
  const pending = S.pendingActiveCollection;
  S.pendingActiveCollection = null;
  S.activeCollection = (S.library.collections || [])
    .some((item) => item.id === pending) ? pending : '';
}

/* The catalog spans every source that has been opened; the window shows one.
 * These three keep that boundary in one place: which source is open, the
 * query fragment that keeps a catalog request inside it, and the folder id
 * the server expects for a folder-scoped query. */
function catalogSourceId(payload) {
  const id = payload?.catalog?.primarySource;
  return Number.isInteger(id) ? id : null;
}

function sourceScopeSpec() {
  if (!S.catalogEnabled || S.primarySourceId == null) return {};
  return { sourceId: S.primarySourceId };
}

function activeFolderId() {
  const id = (S.folderIds || {})[S.activeFolder || ''];
  return Number.isInteger(id) ? id : null;
}

function collectionScope() {
  const collection = activeCollection();
  if (!collection) return S.images.filter(inFolderScope);
  if (collection.type === 'smart') {
    return S.images.filter((image) => photoMatchesRules(image, collection.rules));
  }
  const members = new Set(collection.members || []);
  return S.images.filter((image) => members.has(image.name));
}

let _cachedVisibleKey = '';
let _visibleEpoch = 0;
let _cachedVisibleList = null;
let _cachedVisibleImages = null, _cachedVisibleLibrary = null;
function invalidateVisibleCache() {
  _visibleEpoch++;
  _cachedVisibleKey = '';
  _cachedVisibleList = null;
}

const pairOverrides = new Map();
let pairIndexImages = null, pairIndexLength = -1, pairIndex = new Map();
function photoPairs() {
  if (pairIndexImages !== S.images || pairIndexLength !== S.images.length) {
    pairIndex = indexPairs(S.images); pairIndexImages = S.images; pairIndexLength = S.images.length;
  }
  return pairIndex;
}
function linkedMetadataTargets(targets) {
  return pairedTargets(targets, photoPairs(), APP_PREFS.pairRawJPEG !== false && APP_PREFS.linkPairedMetadata === true);
}
function syncPairControls() {
  const image = cur(), members = APP_PREFS.pairRawJPEG === false ? [] : photoPairs().get(image?.name) || [];
  const companion = members.find(member => member.raw !== image?.raw);
  const button = $('switchPair');
  button.hidden = !companion;
  if (companion) {
    button.textContent = image.raw ? tr("RAW → JPEG") : tr("JPEG → RAW");
    button.title = tr("Open {value}. Each file keeps its own edits.", {value: displayName(companion)});
  }
  $('pairedMetadataNote').hidden = !companion;
  $('pairedMetadataNote').textContent = APP_PREFS.linkPairedMetadata === true ? tr("RAW + JPEG · Ratings, flags, labels and keywords are linked. Edits stay separate.") : tr("RAW + JPEG · Ratings and edits stay separate. Link metadata in Settings → Library.");
}
$('switchPair').onclick = async () => {
  const image = cur();
  const companion = photoPairs().get(image?.name)?.find(member => member.raw !== image.raw);
  if (!companion) return;
  pairOverrides.set(pairKey(companion), companion.name);
  invalidateVisibleCache();
  // The requested member is explicit. A RAW-only/JPEG-only filter should not
  // immediately navigate away from it; keep other library filters intact.
  if (['raw', 'processed'].includes($('kindFilter').value)) $('kindFilter').value = 'all';
  LIBRARY_FILTERS.setTypes([]);
  if (S.msel.delete(image.name)) S.msel.add(companion.name);
  await go(S.images.indexOf(companion));
  refreshLists(); savePrefs();
};

function visible() {
  const f = $('filter')?.value || 'all';
  const rf = $('ratingFilter')?.value || 'all';
  const kind = $('kindFilter')?.value || 'all';
  const labelFilter = $('labelFilter') ? $('labelFilter').value : 'all';
  const editState = $('editFilter')?.value || 'all';
  const fileTypes = LIBRARY_FILTERS.types();
  const metadata = LIBRARY_FILTERS.metadata();
  const hideUndisplayable = LIBRARY_FILTERS.hideUndisplayable();
  const search = $('search')?.value || '';
  const s = $('sort')?.value || 'capture';
  const stacksKey = (S.library.stacks || []).map((stack) => `${stack.id}:${stack.collapsed}`).join(',');
  const pairMode = pairViewPreference(APP_PREFS);
  const cullKey = `${S.cull.review}|${CULL_SELECT.filter((k) => S.cull.on[k]).join(',')}`
    + `|${CULL_REJECT.filter((k) => S.cull.on[k]).join(',')}|${S.cull.revision}`;
  const cacheKey = `${S.libraryRevision || 0}|${S.activeFolder}|${S.includeSubfolders}|${S.activeCollection}|${f}|${rf}|${kind}|${labelFilter}|${editState}|${fileTypes.join(",")}|${JSON.stringify(metadata)}|${hideUndisplayable}|${PHOTO_DISPLAY_STATUS.revision}|${search}|${s}|${stacksKey}|${pairMode}|${cullKey}|${S.images.length}`;
  if (_cachedVisibleList && _cachedVisibleKey === cacheKey &&
      _cachedVisibleImages === S.images && _cachedVisibleLibrary === S.library) {
    return _cachedVisibleList;
  }
  let list = collectionScope()
    .filter((im) => im.kind !== 'video')
    .filter((im) => !hideUndisplayable || !PHOTO_DISPLAY_STATUS.cannotDisplay(im))
    .filter(matchesCullReview)
    .filter((im) => {
      if (f === 'all') return true;
      if (f === 'rated') return (im.rating || 0) >= 1;
      if (f === 'unrated') return !im.rating || +im.rating === 0;
      if (f === 'edited') return photoHasEdits(im);
      if (f === 'unedited') return !photoHasEdits(im);
      if (f === 'virtual') return !!im.virtual;
      if (f === 'pending') return !im.status || im.status === 'pending';
      return im.status === f;
    });
  list = list.filter((im) => (labelFilter === 'all'
      || (labelFilter === 'any' ? cleanLabel(im.label) !== 'none'
          : cleanLabel(im.label) === labelFilter)));
  list = list.filter((im) => {
    const r = +im.rating || 0;
    if (rf === 'unrated') {
      if (r > 0) return false;
    } else if (rf !== 'all' && rf !== '0' && r < +rf) {
      return false;
    }
    const matchesKind = (kind === 'all' || (kind === 'raw' && im.raw) ||
      (kind === 'processed' && !im.raw && !im.virtual) ||
      (kind === 'virtual' && im.virtual));
    return matchesKind && matchesLibraryFilters(im, fileTypes, editState, metadata) && photoMatchesQuery(im, search);
  });
  list = collapsePairs(list, pairMode, pairOverrides);
  for (const stack of S.library.stacks || []) {
    if (!stack.collapsed || S.cull.review !== 'all') continue;
    const visibleMembers = stack.members.filter((name) => list.some((im) => im.name === name));
    if (visibleMembers.length > 1) {
      const cover = visibleMembers[0];
      const hidden = new Set(visibleMembers.slice(1));
      list = list.filter((im) => im.name === cover || !hidden.has(im.name));
    }
  }
  if (s === 'name') {
    list = [...list].sort((a, b) =>
      String(a.sourceName || a.name || '').localeCompare(
        String(b.sourceName || b.name || ''), undefined, { numeric: true, sensitivity: 'base' }
      ));
  } else if (s === 'rating') {
    list = [...list].sort((a, b) => (b.rating || 0) - (a.rating || 0));
  } else if (s === 'status') {
    list = [...list].sort((a, b) => String(a.status || '').localeCompare(String(b.status || '')));
  } else if (s === 'date' || s === 'capture') {
    list = [...list].sort((a, b) =>
      captureSortValue(a) - captureSortValue(b));
  } else if (s === 'label') {
    list = [...list].sort((a, b) =>
      LABELS.indexOf(cleanLabel(a.label)) - LABELS.indexOf(cleanLabel(b.label)));
  }
  _visibleEpoch++;
  _cachedVisibleKey = cacheKey;
  _cachedVisibleList = list;
  _cachedVisibleImages = S.images;
  _cachedVisibleLibrary = S.library;
  return list;
}

function inFolderScope(im) {
  // Folder paths are relative to their own source, so the open source is the
  // outer boundary of the view. Without this an empty selection means the root
  // of the open source, not every source the catalog has ever seen.
  if (S.primarySourceId != null && im.sourceId != null &&
      im.sourceId !== S.primarySourceId) return false;
  const dir = im.folder || '';
  const selected = S.activeFolder || '';
  if (!selected) return S.includeSubfolders || !dir;
  return dir === selected || (S.includeSubfolders && dir.startsWith(selected + '/'));
}

function starStr(n) { return '<span class="rated">' + '★'.repeat(n) + '</span>' + '★'.repeat(5 - n); }

let _stripKey = '', _gridKey = '', _gridMembershipKey = '';
const _stripEls = new Map(), _gridEls = new Map();
const STRIP_OVERSCAN = 24;
let _gridLayout = null, _gridLayoutKey = '';
let _gridList = [];
const _gridIndexByName = new Map();
let selectionAnchorName = null;

function stripItemPitch() {
  const thumb = $('strip')?.querySelector('.thumb');
  if (thumb?.getBoundingClientRect().width) {
    return thumb.getBoundingClientRect().width + 6;
  }
  const height = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--filmstrip-h')) || 116;
  return clamp(height - 22, 64, 238) + 6;
}

function listKey(list) {
  const len = list.length;
  const sample = len === 0 ? '' : `${list[0]?.name}:${list[len - 1]?.name}:${list[len >> 1]?.name}`;
  return [S.libraryRevision || 0, $('filter')?.value, $('ratingFilter')?.value, $('kindFilter')?.value,
    _cachedVisibleKey, $('sort')?.value, $('search')?.value, S.activeCollection,
    S.activeFolder, S.includeSubfolders, len, sample].join('|');
}

function thumbnailURL(im, edge = 240) {
  return `/api/thumb?name=${encodeURIComponent(im.name)}&key=${encodeURIComponent(im.fileKey || im.mtime || '')}&w=${edge}`;
}

let displayStatusFrame = 0, displayFilterChanged = false;
function recordPhotoDisplayState(im, failed, channel = 'thumbnail') {
  displayFilterChanged = PHOTO_DISPLAY_STATUS.record(im, failed, channel) || displayFilterChanged;
  if (displayStatusFrame) return;
  displayStatusFrame = requestAnimationFrame(() => {
    displayStatusFrame = 0;
    const changed = displayFilterChanged;
    displayFilterChanged = false;
    if (changed && LIBRARY_FILTERS.hideUndisplayable()) refreshFilteredView();
    else syncUndisplayableLink();
  });
}

function syncUndisplayableLink() {
  const library = $('library');
  const link = $('hideUndisplayableLink');
  link.hidden = true;
  if (!library.classList.contains('show') || LIBRARY_FILTERS.hideUndisplayable()) return;
  const viewport = library.getBoundingClientRect();
  const top = Math.max(viewport.top, library.querySelector('.library-head').getBoundingClientRect().bottom);
  for (const element of _gridEls.values()) {
    if (element.querySelector('img')?.dataset.thumbnailError !== '1') continue;
    const rect = element.getBoundingClientRect();
    if (rect.bottom > top && rect.top < viewport.bottom && rect.right > viewport.left && rect.left < viewport.right) {
      link.hidden = false;
      break;
    }
  }
}

const EDITED_THUMB_CONCURRENCY = 2;
const EDITED_THUMB_RETRY_LIMIT = 36;
const _editedThumbnailCache = new Map();
const _editedThumbnailQueue = [];
const _queuedEditedThumbnails = new Set();
let _activeEditedThumbnails = 0;
let _editedThumbnailEpoch = 0;

function editedThumbnailEdge() {
  return S.viewMode === 'detail' ? 320 : 1024;
}

function editedThumbnailIdentity(im) {
  return `${_editedThumbnailEpoch}|${im.name}|${im.fileKey || im.mtime || ''}|${im.thumbnailRevision || 0}|${editedThumbnailEdge()}`;
}

function currentThumbnailIdentity(name) {
  const im = S.images.find((image) => image.name === name);
  return im ? editedThumbnailIdentity(im) : null;
}

function paintEditedThumbnail(name, identity, url) {
  for (const image of document.querySelectorAll('img[data-thumbnail-name]')) {
    if (image.dataset.thumbnailName !== name ||
        image.dataset.thumbnailIdentity !== identity) continue;
    if (image.getAttribute('src') !== url) image.setAttribute('src', url);
    image.dataset.thumbnailKind = 'edited';
  }
}

function cacheEditedThumbnail(name, identity, blob) {
  const previous = _editedThumbnailCache.get(name);
  if (previous?.url) URL.revokeObjectURL(previous.url);
  const url = URL.createObjectURL(blob);
  _editedThumbnailCache.delete(name);
  _editedThumbnailCache.set(name, { identity, url });
  if (_editedThumbnailCache.size > 64) {
    const oldest = _editedThumbnailCache.keys().next().value;
    URL.revokeObjectURL(_editedThumbnailCache.get(oldest).url);
    _editedThumbnailCache.delete(oldest);
  }
  paintEditedThumbnail(name, identity, url);
}

function pumpEditedThumbnailQueue() {
  while (_activeEditedThumbnails < EDITED_THUMB_CONCURRENCY &&
         _editedThumbnailQueue.length) {
    const task = _editedThumbnailQueue.shift();
    if (currentThumbnailIdentity(task.name) !== task.identity ||
        !isThumbnailVisible(task.name)) {
      _queuedEditedThumbnails.delete(task.identity);
      continue;
    }
    _activeEditedThumbnails += 1;
    let retry = false;
    fetch(`/api/thumb/rendered?name=${encodeURIComponent(task.name)}&w=${editedThumbnailEdge()}`, {
      cache: 'no-store',
    }).then(async (response) => {
      if (response.status === 202) {
        retry = true;
        return;
      }
      if (!response.ok) throw new Error(tr("thumbnail {responseStatus}", {responseStatus: response.status}));
      const blob = await response.blob();
      if (currentThumbnailIdentity(task.name) === task.identity) {
        cacheEditedThumbnail(task.name, task.identity, blob);
      }
    }).catch(() => {
      // The source thumbnail remains usable when a background rendition fails.
    }).finally(() => {
      _activeEditedThumbnails -= 1;
      _queuedEditedThumbnails.delete(task.identity);
      pumpEditedThumbnailQueue();
      if (retry && task.attempt < EDITED_THUMB_RETRY_LIMIT &&
          currentThumbnailIdentity(task.name) === task.identity) {
        const delay = Math.min(2000, 350 + task.attempt * 100);
        setTimeout(() => {
          const im = S.images.find((image) => image.name === task.name);
          if (im && editedThumbnailIdentity(im) === task.identity) {
            queueEditedThumbnail(im, task.attempt + 1);
          }
        }, delay);
      }
    });
  }
}

function isThumbnailVisible(name) {
  return !_editedThumbnailObserver || [...document.querySelectorAll('img[data-thumbnail-name]')].some(
    image => image.dataset.thumbnailName === name && image.dataset.thumbnailVisible === '1');
}

function queueEditedThumbnail(im, attempt = 0) {
  if (im?.availability === 'cloud-only') return;
  if (!im || im.kind === 'video' || (window.__LIGHTTABLE_BENCHMARK__ && window.__LIGHTTABLE_NATIVE_JOURNEY_LAYER__ !== 'visual-review')) return;
  if (!isThumbnailVisible(im.name)) return;
  const identity = editedThumbnailIdentity(im);
  const cached = _editedThumbnailCache.get(im.name);
  if (cached?.identity === identity) {
    paintEditedThumbnail(im.name, identity, cached.url);
    return;
  }
  if (_queuedEditedThumbnails.has(identity)) return;
  _queuedEditedThumbnails.add(identity);
  const task = { name: im.name, identity, attempt };
  if (im === cur()) _editedThumbnailQueue.unshift(task);
  else _editedThumbnailQueue.push(task);
  pumpEditedThumbnailQueue();
}

const _editedThumbnailObserver = typeof IntersectionObserver === 'undefined'
  ? null
  : new IntersectionObserver((entries) => {
      for (const entry of entries) {
        entry.target.dataset.thumbnailVisible = entry.isIntersecting ? '1' : '0';
        if (!entry.isIntersecting) continue;
        const im = S.images.find(
          (image) => image.name === entry.target.dataset.thumbnailName);
        if (im) {
          // Reused grid/strip nodes can enter a different view without a DOM
          // rebuild. Match their identity to that view before painting its tier.
          entry.target.dataset.thumbnailIdentity = editedThumbnailIdentity(im);
          queueEditedThumbnail(im);
        }
      }
    }, { rootMargin: '160px' });

function invalidateEditedThumbnail(im) {
  if (!im) return;
  im.thumbnailRevision = (im.thumbnailRevision || 0) + 1;
  const identity = editedThumbnailIdentity(im);
  for (const image of document.querySelectorAll('img[data-thumbnail-name]')) {
    if (image.dataset.thumbnailName === im.name) {
      image.dataset.thumbnailIdentity = identity;
    }
  }
  queueEditedThumbnail(im);
}

function clearEditedThumbnails() {
  _editedThumbnailEpoch += 1;
  for (const image of document.querySelectorAll('img[data-thumbnail-source]')) {
    image.setAttribute('src', image.dataset.thumbnailSource);
    image.dataset.thumbnailKind = 'source';
  }
  for (const cached of _editedThumbnailCache.values()) {
    if (cached.url) URL.revokeObjectURL(cached.url);
  }
  _editedThumbnailCache.clear();
}

function syncThumbnailImage(element, im) {
  const image = element.querySelector('img');
  const source = thumbnailURL(im, element.classList.contains('cell') ? 1024 : 240);
  image.syncThumbnailError ??= bindThumbnailErrors(element, image,
    failed => recordPhotoDisplayState(imageForLibraryElement(element), failed));
  image.syncThumbnailError(source);
  const sourceChanged = image.dataset.thumbnailSource !== source;
  image.dataset.thumbnailName = im.name;
  image.dataset.thumbnailIdentity = editedThumbnailIdentity(im);
  if (sourceChanged || !image.getAttribute('src')) {
    image.dataset.thumbnailSource = source;
    image.dataset.thumbnailKind = 'source';
    image.setAttribute('src', source);
  }
  const cached = _editedThumbnailCache.get(im.name);
  if (cached?.identity === image.dataset.thumbnailIdentity) {
    paintEditedThumbnail(im.name, cached.identity, cached.url);
  }
  if (_editedThumbnailObserver) {
    _editedThumbnailObserver.observe(image);
    if (image.dataset.thumbnailVisible === '1') queueEditedThumbnail(im);
  } else {
    queueEditedThumbnail(im);
  }
}

function imageForLibraryElement(element) {
  return S.images.find((image) => image.name === element.dataset.name) || null;
}

/* Reorder and remove only the nodes that changed. In particular, never empty
 * a thumbnail host before repopulating it: moving an existing <img> preserves
 * its decoded pixels and prevents scope/filter changes from flashing blank. */
function reconcileChildren(host, ordered) {
  let cursor = host.firstElementChild;
  for (const element of ordered) {
    if (element === cursor) {
      cursor = cursor.nextElementSibling;
    } else {
      host.insertBefore(element, cursor);
    }
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    const thumbnail = cursor.querySelector?.('img[data-thumbnail-name]');
    if (thumbnail && _editedThumbnailObserver) {
      _editedThumbnailObserver.unobserve(thumbnail);
    }
    cursor.remove();
    cursor = next;
  }
}

/* One place that paints a cell's colour label, used by both lists. */
function paintLabelDot(element, im) {
  const dot = element.querySelector('.label-dot');
  if (!dot) return;
  const label = cleanLabel(im.label);
  dot.hidden = label === 'none';
  if (label !== 'none') {
    dot.style.background = LABEL_COLORS[label];
    dot.title = LABEL_TITLES[label];
  }
}

function stackForImage(name) {
  return (S.library.stacks || []).find((stack) => stack.members.includes(name)) || null;
}

function createStripItem(im) {
  const element = document.createElement('div');
  element.innerHTML = `<img loading="lazy" decoding="async">
    <span class="dot"></span>
    <span class="label-dot" hidden></span><span class="pair-badge" hidden></span>`;
  element.onclick = (event) => {
    const image = imageForLibraryElement(element);
    if (image) selectPhotoFromPointer(image, event);
  };
  syncStripItem(element, im);
  return element;
}

function syncPairBadge(element, im) {
  const badge = element.querySelector('.pair-badge');
  badge.hidden = APP_PREFS.pairRawJPEG === false || !photoPairs().has(im.name);
  badge.textContent = im.raw ? tr("RAW + J") : tr("J + RAW");
  badge.title = tr("RAW + JPEG capture. Switch file in the top bar; edits stay separate.");
}
function syncStripItem(element, im) {
  element.dataset.name = im.name;
  syncPairBadge(element, im);
  syncThumbnailImage(element, im);
}

function renderStrip(fromScroll = false) {
  const list = visible();
  const host = $('strip');
  const itemPitch = stripItemPitch();
  const viewportCount = Math.max(1,
    Math.ceil((host.clientWidth || 1200) / itemPitch));
  let start = Math.max(0,
    Math.floor(host.scrollLeft / itemPitch) - STRIP_OVERSCAN);
  let end = Math.min(list.length, start + viewportCount + STRIP_OVERSCAN * 2);
  const c = cur();
  const activeIndex = c ? list.findIndex((image) => image.name === c.name) : -1;
  if (!fromScroll && activeIndex >= 0 && (activeIndex < start || activeIndex >= end)) {
    start = Math.max(0, activeIndex - STRIP_OVERSCAN);
    end = Math.min(list.length, start + viewportCount + STRIP_OVERSCAN * 2);
  }
  const key = `${listKey(list)}|${start}:${end}`;
  if (key !== _stripKey) {
    _stripKey = key;
    const before = host.querySelector(':scope > .strip-spacer:first-child')
      || document.createElement('span');
    before.className = 'strip-spacer';
    before.style.flexBasis = `${start * itemPitch}px`;
    const ordered = [before];
    const nextElements = new Map();
    list.slice(start, end).forEach((im) => {
      const element = _stripEls.get(im.name) || createStripItem(im);
      syncStripItem(element, im);
      nextElements.set(im.name, element);
      ordered.push(element);
    });
    const after = host.querySelector(':scope > .strip-spacer:last-child:not(:first-child)')
      || document.createElement('span');
    after.className = 'strip-spacer';
    after.style.flexBasis = `${Math.max(0, list.length - end) * itemPitch}px`;
    ordered.push(after);
    reconcileChildren(host, ordered);
    _stripEls.clear();
    nextElements.forEach((element, name) => _stripEls.set(name, element));
  }
  list.slice(start, end).forEach((im) => {
    const d = _stripEls.get(im.name);
    if (d) d.className = 'thumb ' + im.status + (im === c ? ' cur' : '')
      + (S.msel.has(im.name) ? ' msel' : '');
    if (d) paintLabelDot(d, im);
  });
  if (!fromScroll && activeIndex >= 0) {
    const left = activeIndex * itemPitch;
    const right = left + itemPitch;
    if (left < host.scrollLeft) host.scrollLeft = left;
    else if (right > host.scrollLeft + host.clientWidth) {
      host.scrollLeft = right - host.clientWidth;
    }
  }
}

function recordGridThumbnailGeometry(element) {
  const image = element.querySelector('img');
  const im = _gridList[_gridIndexByName.get(element.dataset.name)];
  if (!im || (im.width && im.height) || image.dataset.thumbnailKind !== 'source' ||
      !image.naturalWidth || !image.naturalHeight) return;
  const ratio = image.naturalHeight / image.naturalWidth;
  if (im.thumbnailAspectRatio === ratio) return;
  // Folder mode and incomplete catalog rows may lack source metadata. Keep a
  // thumbnail ratio separate from true source pixels (used for 1:1/export).
  im.thumbnailAspectRatio = ratio;
  if (S.viewMode === 'photo') layoutPhotoGrid();
}

function createGridCell(im) {
  const d = document.createElement('div');
  d.innerHTML = `<img loading="lazy" decoding="async">
    <span class="idx"></span>
    <span class="label-dot" hidden></span>
    <button class="stack-badge" type="button" hidden></button><span class="pair-badge" hidden></span>
    <div class="meta"><span class="dot"></span>
    <span class="nm"></span>
    <span class="stars"></span></div>`;
  d.querySelector('img').addEventListener('load', () => recordGridThumbnailGeometry(d));
  d.onclick = (event) => {
    const image = imageForLibraryElement(d);
    if (image) selectPhotoFromPointer(image, event);
  };
  d.oncontextmenu = async (event) => {
    event.preventDefault();
    const image = imageForLibraryElement(d);
    if (image && image.name !== cur()?.name && !S.msel.has(image.name)) {
      await selectPhotoFromPointer(image, event);
    }
    openActionMenu('libraryMenu', null, event);
  };
  d.ondblclick = () => {
    const image = imageForLibraryElement(d);
    if (image) {
      go(S.images.indexOf(image));
      setViewMode('detail');
    }
  };
  d.querySelector('.stack-badge').onclick = async (event) => {
    event.stopPropagation();
    const image = imageForLibraryElement(d);
    const stack = image && stackForImage(image.name);
    if (stack) await runLibraryAction({ action: 'toggle_stack', id: stack.id });
  };
  d.addEventListener('dragstart', (e) => {
    const image = imageForLibraryElement(d);
    if (!image) return e.preventDefault();
    if (!S.msel.has(image.name)) {
      S.msel.clear();
      S.msel.add(image.name);
      renderGrid();
    }
    const names = [...S.msel];
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('application/x-lighttable-photos', JSON.stringify(names));
    e.dataTransfer.setData('text/plain', names.join('\n'));
  });
  syncGridCell(d, im);
  return d;
}

function syncGridCell(element, im) {
  element.dataset.name = im.name;
  syncPairBadge(element, im);
  element.draggable = !im.virtual;
  element.style.setProperty('--photo-aspect-ratio', im.width && im.height
    ? `${im.width} / ${im.height}` : 'auto');
  syncThumbnailImage(element, im);
  const name = element.querySelector('.nm');
  const nextName = im.availability === 'cloud-only'
    ? tr('{name} · Cloud only', {name: displayName(im)}) : displayName(im);
  element.title = im.availability === 'cloud-only' ? tr("Make this photo available offline in its cloud storage app, then retry.") : displayName(im);
  if (name.textContent !== nextName) name.textContent = nextName;
  recordGridThumbnailGeometry(element);
}

function gridScrollAnchor() {
  const library = $('library');
  if (!library.classList.contains('show')) return null;
  const top = library.getBoundingClientRect().top + library.querySelector('.library-head').offsetHeight;
  let first = null;
  for (const element of _gridEls.values()) {
    const bounds = element.getBoundingClientRect();
    if (bounds.bottom > top && (!first || bounds.top < first.top)) {
      first = { name: element.dataset.name, top: bounds.top };
    }
  }
  return first;
}

function gridViewportTop() {
  // Read the container once; never derive every thumbnail's position from DOM.
  return $('library').getBoundingClientRect().top - $('grid').getBoundingClientRect().top;
}

function renderGrid() {
  const list = visible();
  const grid = $('grid'), library = $('library');
  const photo = S.viewMode === 'photo';
  const width = grid.clientWidth;
  if (!width || !library.classList.contains('show')) return;
  const membershipKey = listKey(list);
  const layoutKey = `${membershipKey}|${width}|${$('gridSize').value}|${photo}`;
  const layoutChanged = layoutKey !== _gridLayoutKey || list !== _gridList;
  if (layoutChanged) {
    const anchor = gridScrollAnchor();
    _gridLayoutKey = layoutKey;
    _gridMembershipKey = membershipKey;
    _gridList = list;
    _gridIndexByName.clear();
    list.forEach((im, index) => _gridIndexByName.set(im.name, index));
    _gridLayout = createGridLayout(list, { width, cell: +$('gridSize').value, photo });
    grid.style.height = `${_gridLayout.height}px`;
    if (anchor && _gridIndexByName.has(anchor.name)) {
      const position = _gridLayout.positions[_gridIndexByName.get(anchor.name)];
      library.scrollTop += grid.getBoundingClientRect().top + position.top - anchor.top;
    }
  }
  const positions = visibleGridPositions(_gridLayout, gridViewportTop(), library.clientHeight);
  const key = `${layoutKey}|${positions.map((p) => p.index).join(',')}`;
  if (layoutChanged || key !== _gridKey) {
    _gridKey = key;
    const ordered = [];
    const nextElements = new Map();
    positions.forEach((position) => {
      const im = list[position.index];
      const element = _gridEls.get(im.name) || createGridCell(im);
      syncGridCell(element, im);
      Object.assign(element.style, { left: `${position.left}px`, top: `${position.top}px`,
        width: `${position.width}px`, height: `${position.height}px` });
      nextElements.set(im.name, element);
      ordered.push(element);
    });
    reconcileChildren(grid, ordered);
    _gridEls.clear();
    nextElements.forEach((element, name) => _gridEls.set(name, element));
  }
  const c = cur();
  positions.forEach(({ index }) => {
    const im = list[index], d = _gridEls.get(im.name);
    if (!d) return;
    const stack = stackForImage(im.name);
    const isCover = stack && stack.members.find((name) => _gridIndexByName.has(name)) === im.name;
    const paintKey = [im.status, im === c, S.msel?.has(im.name), im.virtual,
      isCover, stack?.collapsed, stack?.members.length, im.rating, im.label, photo].join('|');
    if (d.dataset.paintKey === paintKey) return;
    d.dataset.paintKey = paintKey;
    d.className = 'cell ' + im.status + (im === c ? ' sel' : '')
      + (S.msel?.has(im.name) ? ' msel' : '')
      + (im.virtual ? ' virtual-copy' : '') + (isCover ? ' stack-cover' : '')
      + (photo ? ' photo-cell' : '');
    d.querySelector('img').classList.toggle('photo-grid-image', photo);
    d.querySelector('.meta').hidden = photo;
    d.querySelector('.idx').hidden = photo;
    const badge = d.querySelector('.stack-badge');
    badge.hidden = !isCover;
    if (isCover) {
      badge.textContent = stack.members.length;
      badge.title = stack.collapsed ? tr("Expand stack") : tr("Collapse stack");
    }
    const st = d.querySelector('.stars'), want = starStr(im.rating || 0);
    if (st.innerHTML !== want) st.innerHTML = want;
    d.querySelector('.idx').textContent = S.msel?.has(im.name) ? '✓' : '';
    paintLabelDot(d, im);
  });
  if (S.catalogEnabled && S.images.length < S.catalogTotal && positions.length) {
    const lastVisible = positions[positions.length - 1].index;
    if (lastVisible + LIBRARY_CHUNK_SIZE >= S.images.length) {
      loadRemainingCatalogRows(S.catalogTotal).catch(() => {});
    }
  }
  $('emptyState').classList.toggle('show', list.length === 0);
  applyGridStyle();
  syncUndisplayableLink();
}

let libraryScrollFrame = 0;
function scheduleGridLayout() {
  if (libraryScrollFrame) return;
  libraryScrollFrame = requestAnimationFrame(() => {
    libraryScrollFrame = 0;
    renderGrid();
  });
}
$('library').addEventListener('scroll', scheduleGridLayout, { passive: true });

let stripScrollFrame = 0;
$('strip').addEventListener('scroll', () => {
  if (stripScrollFrame) return;
  stripScrollFrame = requestAnimationFrame(() => {
    stripScrollFrame = 0;
    renderStrip(true);
  });
}, { passive: true });

function layoutPhotoGrid() {
  _gridLayoutKey = '';
  scheduleGridLayout();
}

function applyGridStyle() {
  const grid = $('grid');
  const photo = S.viewMode === 'photo';
  grid.classList.toggle('photo-grid', photo);
  grid.classList.toggle('square-grid', !photo);

}

if ('ResizeObserver' in window) {
  new ResizeObserver(scheduleGridLayout).observe($('library'));
}

const cachedLibrarySummary = createSummaryCache();
let _countsPaintKey = '';

function counts() {
  LIBRARY_FILTERS.sync();
  // Selection changes do not alter catalog counts or collection membership.
  const summaryKey = `${S.libraryRevision || 0}|${_visibleEpoch}|${S.activeFolder}|${S.includeSubfolders}|${S.activeCollection}|${S.images.length}`;
  const summary = cachedLibrarySummary(summaryKey, S.images, () => {
    const scope = collectionScope();
    let a = 0, s = 0, p = 0, rated = 0;
    for (const im of scope) {
      if (im.status === 'approved') a++;
      else if (im.status === 'skipped') s++;
      else p++;
      if ((im.rating || 0) > 0) rated++;
    }
    return { scope, a, s, p, rated };
  });
  const { scope, a, s, p, rated } = summary;
  const selected = activeCollection();
  $('addToCollection').disabled = !selected || selected.type !== 'regular' || !transferTargets().length;
  const paintKey = `${summaryKey}|${_cachedVisibleKey}|${$('search').value}`;
  if (_countsPaintKey === paintKey) return;
  _countsPaintKey = paintKey;
  const shown = visible().length;
  $('counts').textContent = tr("{shown} of {scopeLength}", {shown: shown, scopeLength: scope.length});
  $('counts').title = tr('Photos shown / total photos in this folder or collection');
  $('sourceAllCount').textContent = scope.length;
  $('sourcePendingCount').textContent = p;
  $('sourceApprovedCount').textContent = a;
  $('sourceSkippedCount').textContent = s;
  $('sourceRatedCount').textContent = rated;
  $('filmstripCount').textContent = trn('{count} photo', '{count} photos', shown);
  const labels = { all: tr('All Photos'), pending: tr('Unflagged'), approved: tr('Picked'),
    skipped: tr('Rejected'), rated: tr('Rated'), unrated: tr('Unrated'), edited: tr('Edited'), unedited: tr('Unedited'),
    virtual: tr('Virtual Copies') };
  const folder = S.folders.find((item) => item.path === S.activeFolder);
  const folderLabel = (folder?.name || (S.rootFolder.split('/').filter(Boolean).pop() || tr("All Photos")));
  const collection = activeCollection();
  const scopeLabel = collection?.name || folderLabel;
  $('libraryTitle').textContent = $('filter').value === 'all'
    ? scopeLabel : `${labels[$('filter').value] || tr('All Photos')} — ${scopeLabel}`;
  const resultCount = shown === scope.length
    ? trn('{count} photo', '{count} photos', scope.length)
    : tr('{shown} of {total} photos', {shown, total: scope.length});
  $('searchSummary').textContent = $('search').value.trim()
    ? tr('{resultCount} · Results for “{query}”', {resultCount, query: $('search').value.trim()})
    : tr('{resultCount} · Local', {resultCount});
  document.querySelectorAll('[data-source]').forEach((b) => {
    b.classList.toggle('on', !collection && b.dataset.source === $('filter').value);
  });
  renderCollections();
}

function collectionImages(collection) {
  if (!collection) return [];
  if (collection.type === 'smart') {
    return S.images.filter((image) => photoMatchesRules(image, collection.rules));
  }
  const members = new Set(collection.members || []);
  return S.images.filter((image) => members.has(image.name));
}

function renderCollections() {
  const host = $('collectionList');
  if (!host) return;
  host.replaceChildren();
  for (const collection of S.library.collections || []) {
    const row = document.createElement('div');
    row.className = 'collection-row' +
      (collection.id === S.activeCollection ? ' on' : '');
    row.innerHTML = `<button class="collection-main" type="button"><span></span><b>${collectionImages(collection).length}</b></button><button class="collection-delete" type="button" title="${i18nHTML(tr("Delete collection"))}">×</button>`;
    row.querySelector('span').textContent =
      `${collection.type === 'smart' ? '✦ ' : ''}${collection.name}`;
    row.querySelector('.collection-delete').setAttribute(
      'aria-label', tr("Delete {collectionName}", {collectionName: collection.name}));
    row.querySelector('.collection-main').onclick = () => {
      S.activeCollection = S.activeCollection === collection.id ? '' : collection.id;
      refreshFilteredView(); savePrefs();
    };
    row.querySelector('.collection-delete').onclick = async () => {
      await runLibraryAction({ action: 'delete_collection', id: collection.id });
      if (S.activeCollection === collection.id) S.activeCollection = '';
      refreshLists(); savePrefs(); toast(tr("Collection deleted"));
    };
    host.appendChild(row);
  }
  $('collectionBlock').classList.toggle('is-empty', !host.children.length);
  const selected = activeCollection();
  $('addToCollection').hidden = !selected;
  $('addToCollection').disabled = !selected || selected.type !== 'regular' ||
    !transferTargets().length;
}

async function runLibraryAction(body) {
  const collectionActions = {
    create_collection: 'create', create_smart_collection: 'create_smart',
    delete_collection: 'delete', add_to_collection: 'add',
  };
  let path = '/api/library';
  let request = body;
  if (S.catalogEnabled && collectionActions[body.action]) {
    const rules = { ...(body.rules || {}) };
    if (rules.flag && rules.flag !== 'all') rules.status = rules.flag;
    delete rules.flag;
    request = {
      action: collectionActions[body.action], id: body.id, name: body.name,
      rules, imageIds: (body.members || []).map((name) => {
        const image = S.images.find((item) => item.name === name);
        return image?.catalogId || image?.id;
      }).filter((id) => Number.isInteger(+id)).map(Number),
    };
    path = '/api/catalog/collections';
  }
  const result = await api(path, request);
  if (result.error) { toast(result.error); return null; }
  if (result.library) S.library = result.library;
  _stripKey = _gridKey = ''; refreshLists();
  return result;
}

async function createVirtualCopy() {
  const source = cur(); if (!source) return;
  if (!await saveState(true)) return;
  const defaultName = tr("{value} — Copy", {value: displayName(source).replace(/\.[^.]+$/, '')});
  const displayNameValue = await askName(tr("Name virtual copy"), defaultName);
  if (!displayNameValue) return;
  const result = await runLibraryAction({
    action: 'create_virtual', name: source.name, displayName: displayNameValue,
  });
  if (!result?.copy) return;
  const copy = {
    ...source, ...result.copy, displayName: result.copy.displayName,
    folder: source.folder, mtime: source.mtime, ai: source.ai,
    keywords: Array.isArray(result.copy.keywords) ? result.copy.keywords : [],
    versions: Array.isArray(result.copy.versions) ? result.copy.versions : [],
    masks: normalizeMasks(result.copy.masks), heals: normalizeHeals(result.copy.heals),
    optics: normalizeOptics(result.copy.optics),
  };
  const index = S.images.indexOf(source) + 1;
  S.images.splice(index, 0, copy);
  _stripKey = _gridKey = ''; go(index); toast(tr("Virtual copy created"));
}

$('virtualCopyBtn').onclick = createVirtualCopy;
$('deleteVirtualBtn').onclick = async () => {
  const image = cur();
  if (!image?.virtual) return;
  const oldIndex = S.idx;
  const result = await runLibraryAction({ action: 'delete_virtual', name: image.name });
  if (!result) return;
  S.images = S.images.filter((item) => item.name !== image.name);
  S.msel.delete(image.name);
  _stripKey = _gridKey = '';
  go(Math.min(oldIndex, Math.max(0, S.images.length - 1)));
  toast(tr("Virtual copy deleted"));
};
$('stackBtn').onclick = async () => {
  const members = transferTargets().map((image) => image.name);
  if (members.length < 2) return toast(tr("Select at least two photos to stack"));
  const name = await askName(tr("Name stack"), tr('Photo stack'));
  if (!name) return;
  await runLibraryAction({ action: 'create_stack', name, members });
  S.msel.clear(); refreshLists(); toast(tr("Stack created"));
};
$('unstackBtn').onclick = async () => {
  const stack = transferTargets().map((image) => stackForImage(image.name)).find(Boolean);
  if (!stack) return toast(tr("Select a photo in a stack"));
  await runLibraryAction({ action: 'unstack', id: stack.id });
  toast(tr("Photos unstacked"));
};
$('matchExposureBtn').onclick = async () => {
  const current = cur();
  if (!current) return toast(tr("Select an active reference photo first"));
  const targets = transferTargets().map((image) => image.name);
  if (targets.length < 2) return toast(tr("Select at least two photos to match exposure"));
  const button = $('matchExposureBtn');
  button.disabled = true;
  try {
    const res = await fetch('/api/match-exposure', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reference: current.name, targets }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(((data.error || tr("Failed to match exposure"))));
    toast(tr("Matched exposure on {dataCount} photos", {dataCount: data.count}));
    await reloadLibrary();
  } catch (err) {
    toast(((err.message || tr("Error matching exposure"))));
  } finally {
    button.disabled = false;
  }
};
$('pregenPreviewsBtn').onclick = async () => {
  const selected = transferTargets();
  const targets = selected.map((image) => image.name);
  if (!targets.length) return toast(tr("Select photos to build previews"));
  const size = Math.max(INTERACTIVE_PREVIEW_WIDTH,
    ...selected.map((image) => sourceLongEdge(image)));
  const button = $('pregenPreviewsBtn');
  button.disabled = true;
  try {
    const res = await fetch('/api/cache/pregenerate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: targets, size }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(((data.error || tr("Failed to start preview generation"))));
    toast(tr("Building 1:1 previews for {dataQueued} photos…", {dataQueued: data.queued}));
    const poll = async () => {
      try {
        const sres = await fetch('/api/cache/pregenerate/status');
        const sdata = await sres.json();
        if (sdata.active) {
          toast(tr("1:1 Previews: {sdataCompleted} / {sdataTotal} done", {sdataCompleted: sdata.completed, sdataTotal: sdata.total}));
          setTimeout(poll, 1500);
        } else if (sdata.total > 0 && sdata.completed >= sdata.total) {
          toast(tr("1:1 Previews finished: {sdataCompleted} built", {sdataCompleted: sdata.completed}));
        }
      } catch {}
    };
    setTimeout(poll, 1500);
  } catch (err) {
    toast(((err.message || tr("Error starting preview generation"))));
  } finally {
    button.disabled = false;
  }
};
$('addCollection').onclick = async () => {
  const name = await askName(tr("New collection"));
  if (!name) return;
  const result = await runLibraryAction({
    action: 'create_collection', name,
    members: transferTargets().map((image) => image.name),
  });
  const created = result?.library?.collections?.find(
    (collection) => String(collection.id) === String(result.id));
  if (created) S.activeCollection = created.id;
  refreshLists(); toast(tr("Collection created"));
};
$('addSmartCollection').onclick = async () => {
  const name = await askName(tr("Save current filters"));
  if (!name) return;
  const result = await runLibraryAction({
    action: 'create_smart_collection', name,
    rules: { flag: ['pending', 'approved', 'skipped'].includes($('filter').value) ? $('filter').value : 'all',
      ratingMin: Math.max(+$('ratingFilter').value || 0,
        $('filter').value === 'rated' ? 1 : 0),
      kind: $('kindFilter').value, query: $('search').value,
      fileTypes: LIBRARY_FILTERS.types(),
      editState: ['edited', 'unedited', 'virtual'].includes($('filter').value)
        ? $('filter').value : $('editFilter').value,
      unrated: $('ratingFilter').value === 'unrated' || $('filter').value === 'unrated',
      label: $('labelFilter').value, ...LIBRARY_FILTERS.metadata() },
  });
  const created = result?.library?.collections?.find(
    (collection) => String(collection.id) === String(result.id));
  if (created) S.activeCollection = created.id;
  refreshLists(); toast(tr("Smart collection saved"));
};
$('addToCollection').onclick = async () => {
  const collection = activeCollection();
  if (!collection || collection.type !== 'regular') return;
  await runLibraryAction({ action: 'add_to_collection', id: collection.id,
    members: transferTargets().map((image) => image.name) });
  toast(tr("Added to collection"));
};

/* ------------------------------------------------------------ folders */
function fullFolderPath(sourcePath, relative) {
  return relative ? `${sourcePath.replace(/\/$/, '')}/${relative}` : sourcePath;
}

function activeSources() {
  if (S.sources.length) return S.sources;
  if (!S.rootFolder) return [];
  return [{ path: S.rootFolder, name: S.rootFolder.split('/').pop(),
    favorite: true, available: true }];
}

function selectFolder(relative) {
  S.activeCollection = '';
  S.activeFolder = relative || '';
  if (S.rootFolder) S.activeFolders[S.rootFolder] = S.activeFolder;
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  const first = visible()[0];
  if (first) {
    go(S.images.indexOf(first));
    refreshLists();
  } else {
    refreshLists();
  }
  setViewMode(S.gridViewMode);
  renderFolders();
  savePrefs();
}

function makeFolderRow(source, data, isRoot) {
  const relative = isRoot ? '' : data.path;
  const isCurrentSource = source.path === S.rootFolder;
  const row = document.createElement('div');
  row.className = 'folder-row' + (isRoot ? '' : ' nested')
    + (!source.available ? ' unavailable' : '')
    + (isCurrentSource && S.activeFolder === relative ? ' on' : '');
  const depth = data.depth ?? relative.split('/').filter(Boolean).length;
  row.style.setProperty('--depth', isRoot ? 0 : depth);
  row.title = fullFolderPath(source.path, relative);
  row.dataset.folder = relative;

  {
    const favorite = isRoot ? source.favorite
      : S.favoriteFolders.includes(fullFolderPath(source.path, relative));
    const star = document.createElement('button');
    star.className = 'folder-star' + (favorite ? ' on' : '');
    star.title = favorite ? tr("Remove from Favorites") : tr("Add as Favorite");
    star.setAttribute('aria-label', star.title);
    star.textContent = favorite ? '★' : '☆';
    star.onclick = (event) => {
      event.stopPropagation();
      if (isRoot) postNative('toggleFavorite', { path: source.path });
      else toggleFolderFavorite(fullFolderPath(source.path, relative));
    };
    row.appendChild(star);
  }

  const label = document.createElement('span');
  label.className = 'folder-label';
  label.textContent = isRoot ? source.name : data.name;
  row.appendChild(label);

  const count = document.createElement('span');
  count.className = 'folder-count';
  if (isCurrentSource) {
    const total = S.includeSubfolders ? data.totalCount : data.directCount;
    if (Number.isFinite(total)) count.textContent = String(total);
  }
  row.appendChild(count);

  const more = document.createElement('button');
  more.className = 'folder-more';
  more.textContent = '•••';
  more.title = tr("Manage {labelTextContent}", {labelTextContent: label.textContent});
  more.setAttribute('aria-label', more.title);
  more.onclick = (event) => {
    event.stopPropagation();
    openFolderMenu(event.currentTarget, { source, data, isRoot, relative });
  };
  row.appendChild(more);

  row.onclick = () => {
    if (!source.available) return toast(tr("That folder is unavailable"));
    if (!isCurrentSource) {
      S.activeFolders[source.path] = relative;
      savePrefs().then(() => postNative('selectSource', { path: source.path }));
    }
    else selectFolder(relative);
  };

  if (isCurrentSource) {
    row.addEventListener('dragover', (event) => {
      if (!Array.from(event.dataTransfer.types).includes('application/x-lighttable-photos')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', async (event) => {
      event.preventDefault();
      row.classList.remove('drop-target');
      try {
        const names = JSON.parse(event.dataTransfer.getData('application/x-lighttable-photos'));
        await movePhotos(names, relative);
      } catch (_) {
        toast(tr("Could not read the dragged photos"));
      }
    });
  }
  return row;
}

function renderFolders() {
  const host = $('folderTree');
  if (!host) return;
  const sources = activeSources();
  const fragment = document.createDocumentFragment();
  const rootData = S.folders.find((item) => item.path === '') || {
    path: '', name: S.rootFolder.split('/').pop(), depth: 0,
    directCount: 0, totalCount: S.images.length,
  };
  for (const source of sources) {
    if (S.folderMode === 'browse') {
      fragment.appendChild(makeFolderRow(source, rootData, true));
    } else if (source.favorite) {
      fragment.appendChild(makeFolderRow(source, rootData, true));
    }
    if (S.folderMode === 'browse' && source.path === S.rootFolder) {
      for (const data of S.folders.filter((item) => item.path)) {
        fragment.appendChild(makeFolderRow(source, data, false));
      }
    } else if (S.folderMode === 'favorites') {
      const prefix = source.path.replace(/\/$/, '') + '/';
      for (const full of S.favoriteFolders.filter((path) => path.startsWith(prefix))) {
        const relative = full.slice(prefix.length);
        if (!relative) continue;
        const data = source.path === S.rootFolder
          ? S.folders.find((item) => item.path === relative)
          : null;
        fragment.appendChild(makeFolderRow(source, data || {
          path: relative, name: relative.split('/').pop(), depth: 0,
          directCount: 0, totalCount: 0,
        }, false));
      }
    }
  }
  if (!fragment.childNodes.length) {
    const empty = document.createElement('div');
    empty.className = 'folder-empty';
    empty.textContent = tr("Favorite a folder to keep it close at hand.");
    fragment.appendChild(empty);
  }
  host.replaceChildren(fragment);
}

function toggleFolderFavorite(fullPath) {
  const index = S.favoriteFolders.indexOf(fullPath);
  if (index >= 0) S.favoriteFolders.splice(index, 1);
  else S.favoriteFolders.push(fullPath);
  S.favoriteFolders.sort((a, b) => a.localeCompare(b));
  renderFolders();
  savePrefs();
}

function menuButton(label, action, className = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.className = className;
  button.onclick = async () => {
    closeFolderMenu();
    await action();
  };
  return button;
}

function menuSeparator() {
  const line = document.createElement('div');
  line.className = 'menu-separator';
  return line;
}

function closeFolderMenu() {
  const menu = $('folderMenu');
  menu.classList.remove('on');
  menu.setAttribute('aria-hidden', 'true');
  document.querySelectorAll('.folder-row.menu-open').forEach((row) => row.classList.remove('menu-open'));
}

function closeActionMenus() {
  for (const id of ['localLibraryMenu', 'libraryMenu']) {
    $(id)?.classList.remove('on');
    $(id)?.setAttribute('aria-hidden', 'true');
  }
  $('localLibraryMenuBtn')?.setAttribute('aria-expanded', 'false');
  $('libraryMenuBtn')?.setAttribute('aria-expanded', 'false');
}

function openActionMenu(id, anchor = null, event = null) {
  const wasOpen = $(id).classList.contains('on');
  closeActionMenus();
  closeFolderMenu();
  if (anchor && wasOpen) return;
  updateTransferActions();
  const menu = $(id);
  menu.classList.add('on');
  menu.setAttribute('aria-hidden', 'false');
  const width = Math.max(210, menu.offsetWidth);
  const left = event ? event.clientX : anchor.getBoundingClientRect().right - width;
  const top = event ? event.clientY : anchor.getBoundingClientRect().bottom + 3;
  menu.style.left = `${Math.max(8, Math.min(left, innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(top, innerHeight - menu.offsetHeight - 8))}px`;
  const trigger = id === 'libraryMenu' ? $('libraryMenuBtn') : $('localLibraryMenuBtn');
  trigger?.setAttribute('aria-expanded', 'true');
}

$('localLibraryMenuBtn').onclick = (event) => {
  event.stopPropagation();
  openActionMenu('localLibraryMenu', event.currentTarget);
};
$('libraryMenuBtn').onclick = (event) => {
  event.stopPropagation();
  openActionMenu('libraryMenu', event.currentTarget);
};
for (const menu of [$('localLibraryMenu'), $('libraryMenu')]) {
  menu.addEventListener('click', (event) => {
    if (event.target.closest('button')) closeActionMenus();
  });
}

function openFolderMenu(anchor, context) {
  closeFolderMenu();
  const { source, data, isRoot, relative } = context;
  const menu = $('folderMenu');
  const fullPath = fullFolderPath(source.path, relative);
  const current = source.path === S.rootFolder;
  const items = [];
  const favorite = isRoot ? source.favorite : S.favoriteFolders.includes(fullPath);
  items.push(menuButton((favorite ? tr("Remove from Favorites") : tr("Add as Favorite")),
    () => isRoot
      ? postNative('toggleFavorite', { path: source.path })
      : toggleFolderFavorite(fullPath)));
  if (current) {
    items.push(menuButton(tr("Create Folder in {value}…", {value: isRoot ? source.name : data.name}),
      () => createFolder(relative, isRoot ? source.name : data.name)));
  }
  if (isRoot || current) {
    items.push(menuButton(tr("Rename Folder…"),
      () => renameFolder(source, data, isRoot, relative)));
  }
  const browserName = window.__LIGHTTABLE_PLATFORM__ === 'windows'
    ? tr('File Explorer') : window.__LIGHTTABLE_PLATFORM__ === 'linux' ? tr('File Manager') : 'Finder';
  items.push(menuButton(tr("Show in {browserName}", {browserName: browserName}),
    () => postNative('revealFolder', { path: fullPath })));
  if (current) {
    items.push(menuButton(tr("Synchronize Folder"), () => location.reload()));
  }
  if (isRoot && activeSources().length > 1) {
    items.push(menuSeparator());
    items.push(menuButton(tr("Remove from LightTable"), () => removeFolderSource(source.path), 'negative'));
  }
  menu.replaceChildren(...items);
  menu.classList.add('on');
  menu.setAttribute('aria-hidden', 'false');
  anchor.closest('.folder-row')?.classList.add('menu-open');
  const rect = anchor.getBoundingClientRect();
  const width = 220;
  menu.style.left = `${Math.min(rect.left, innerWidth - width - 8)}px`;
  menu.style.top = `${Math.min(rect.bottom + 3, innerHeight - menu.offsetHeight - 8)}px`;
}

async function removeFolderSource(path) {
  const prefix = path.replace(/\/$/, '') + '/';
  S.favoriteFolders = S.favoriteFolders.filter((favorite) => !favorite.startsWith(prefix));
  delete S.activeFolders[path];
  await savePrefs();
  postNative('removeSource', { path });
}

async function createFolder(parent, parentName) {
  const name = await askName(tr("Create Folder in {parentName}", {parentName: parentName}));
  if (!name) return;
  const result = await api('/api/folders', { action: 'create', parent, name });
  if (result.error) return toast(result.error);
  location.reload();
}

async function renameFolder(source, data, isRoot, relative) {
  const oldName = isRoot ? source.name : data.name;
  const name = await askName(tr("Rename Folder"), oldName);
  if (!name || name === oldName) return;
  if (isRoot) {
    postNative('renameRoot', { path: source.path, name });
    return;
  }
  const result = await api('/api/folders', { action: 'rename', path: relative, name });
  if (result.error) return toast(result.error);
  const oldFull = fullFolderPath(source.path, relative);
  const newFull = fullFolderPath(source.path, result.path);
  S.favoriteFolders = S.favoriteFolders.map((path) =>
    path === oldFull || path.startsWith(oldFull + '/')
      ? newFull + path.slice(oldFull.length) : path);
  if (S.activeFolder === relative) S.activeFolder = result.path;
  S.activeFolders[S.rootFolder] = S.activeFolder;
  await savePrefs();
  location.reload();
}

async function movePhotos(names, destination) {
  const unique = [...new Set((names || []).filter((name) => S.images.some((im) => im.name === name)))];
  if (!unique.length) return;
  if (unique.every((name) => (S.images.find((im) => im.name === name)?.folder || '') === destination)) {
    return toast(tr("Photos are already in that folder"));
  }
  if (!await saveState(true)) return;
  const result = await api('/api/photos/move', { names: unique, destination });
  if (result.error) return toast(result.error);
  S.msel.clear();
  location.reload();
}

document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('#folderMenu') && !event.target.closest('.folder-more')) closeFolderMenu();
  if (!event.target.closest('.action-menu') && !event.target.closest('#localLibraryMenuBtn, #libraryMenuBtn')) closeActionMenus();
  if (!event.target.closest('#scopeMenu, #scopeMenuButton')) {
    $('scopeMenu').classList.remove('on');
    $('scopeMenu').setAttribute('aria-hidden', 'true');
    $('scopeMenuButton').setAttribute('aria-expanded', 'false');
  }
});
window.addEventListener('resize', () => { closeFolderMenu(); closeActionMenus(); });

function refreshLists() {
  KEYWORD_BATCH?.sync();
  if (S.cull.review !== 'all' || document.querySelector('.ai-cull-disclosure')?.open) syncCullPanel();
  renderStrip();
  if ($('library').classList.contains('show')) renderGrid();
  counts();
  const stars = $('stars');
  const r = cur() ? (cur().rating || 0) : 0;
  if (!stars.children.length) {
    stars.replaceChildren(...[1, 2, 3, 4, 5].map((i) => {
      const star = document.createElement('span');
      star.className = 's';
      star.dataset.r = i;
      star.textContent = '★';
      return star;
    }));
  }
  for (const star of stars.children) {
    star.classList.toggle('on', +star.dataset.r <= r);
  }
  const im = cur();
  $('approveBtn').classList.toggle('on', im && im.status === 'approved');
  $('skipBtn').classList.toggle('on', im && im.status === 'skipped');
  $('unflagBtn')?.classList.toggle('on', im && (!im.status || im.status === 'pending'));
  renderLabelRow($('labelRow'), im ? im.label : 'none', setLabel);
  if (SURVEY && SURVEY.isOpen) SURVEY.render();
  syncCullBars();
  updateTransferActions();
}

/* The photo's index in the visible list as it stands right now, captured before
 * a mark changes what the filter admits. -1 when it is not currently listed. */
function markResumeIndex(im) {
  if (!im || (SURVEY && SURVEY.isOpen)) return -1;
  return visible().indexOf(im);
}

/* The photo to show after a mark. When the marked photo is still listed the
 * neighbour is simply the one after it. When the mark removed it from the
 * filter, everything below shifted up by one, so the photo now at its former
 * index is the one that came next. Falling back to the top of the library
 * instead would restart the cull on every keystroke. */
function photoAfterMark(list, im, resumeAt = -1) {
  const here = list.indexOf(im);
  if (here >= 0) return list[here + 1];
  /* Guard the index: the callers below are also bound directly to DOM events,
   * where the first argument is an Event rather than a position. */
  if (!Number.isInteger(resumeAt) || resumeAt < 0) return undefined;
  return list[resumeAt] || list[list.length - 1];
}

/* `resumeAt` is the current photo's index from before a mark changed what the
 * filter admits. Without it a filter change lands on the top of the new list,
 * which is what changing a filter should do. */
function refreshFilteredView(resumeAt = -1) {
  const leaving = cur();
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  const list = visible();
  if (list.length && !list.includes(cur())) {
    const resume = photoAfterMark(list, leaving, resumeAt) || list[0];
    go(S.images.indexOf(resume));
    // go() updates S.idx synchronously but may wait for catalog state before
    // showCurrentImage(). Paint the new filtered membership immediately.
    refreshLists();
  } else {
    refreshLists();
  }
}

function setViewMode(mode, persist = true) {
  if (!['photo', 'square', 'detail'].includes(mode)) return;
  stopZoomMotion({finish: true});
  if (mode !== S.viewMode) rememberPhotoPan();
  const gridMode = mode !== 'detail';
  LIBRARY_FILTERS.close();
  S.viewMode = mode;
  if (gridMode) S.gridViewMode = mode;
  $('library').classList.toggle('show', gridMode);
  $('editor').classList.toggle('hide', gridMode);
  $('filmstripShell').style.display = gridMode ? 'none' : '';
  $('appShell').classList.toggle('grid-mode', gridMode);
  $('appShell').classList.toggle('grid-info-open', gridMode && S.activePane === 'infoPane');
  document.querySelectorAll('[data-view]').forEach((button) => {
    const selected = button.dataset.view === mode;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  if (gridMode) {
    renderGrid();
  } else if (cur()) {
    restorePhotoPan();
    doRender();
  }
  syncCullBars();
  syncPairControls();
  scheduleNativeViewportLayout();
  scheduleNativeMenuState();
  if (persist) savePrefs();
}

const paneScrollPositions = new Map();
const PHOTO_TOOL_PANES = ['cropPane', 'maskPane', 'healPane'];
let lastAdjustmentPane = 'editPane';
let compareReturnPane = null;
let cropSession = null;
function beginCropSession() {
  if (cur() && (!cropSession || cropSession.name !== cur().name)) {
    cropSession = { name: cur().name, entry: cropGeometry(JSON.parse(snapshot())) };
  }
}
function cancelCropSession() {
  const session = cropSession; cropSession = null;
  if (session?.name === cur()?.name && S.editingName === session.name) {
    const current = JSON.parse(snapshot());
    const restored = restoreCropGeometry(current, session.entry);
    if (JSON.stringify(cropGeometry(current)) !== JSON.stringify(session.entry)) {
      pushUndo(); restore(JSON.stringify(restored));
    }
  }
  exitPhotoTool();
}
function exitPhotoTool() {
  if (S.compareActive) { setCompareActive(false); return; }
  switchPane(lastAdjustmentPane);
  document.querySelector(`.tool-btn[data-pane="${lastAdjustmentPane}"]`)?.focus();
}
function selectPhotoTool(id) {
  if (S.activePane === id && !S.compareActive) exitPhotoTool();
  else switchPane(id);
}
function switchPane(id, { fromCompare = false } = {}) {
  const requestedPane = id;
  const aliases = {
    lensPane: 'editPane', matchPane: 'filmPane', versionsPane: 'historyPane',
    metadataPane: 'infoPane', aiPane: 'infoPane', catalogPane: 'editPane',
    mergePane: 'editPane', exportPane: 'editPane',
  };
  id = aliases[id] || id;
  if (!$(id)) return;
  if (!fromCompare) setCompareActive(false, { restoreTool: false });
  if (['editPane', 'filmPane'].includes(id)) lastAdjustmentPane = id;
  document.querySelectorAll('[data-exit-tool]').forEach((button) => {
    button.textContent = lastAdjustmentPane === 'filmPane' ? tr("Back to Film") : tr("Back to Edit");
  });
  PRESET_BROWSER?.setActive(id === 'presetsPane');
  const panel = $('panel');
  const previousPane = S.activePane;
  if (previousPane === 'cropPane' && id !== 'cropPane' && !fromCompare) cropSession = null;
  if (previousPane && previousPane !== id) {
    paneScrollPositions.set(previousPane, panel.scrollTop);
  }
  S.activePane = id;
  if (previousPane === 'healPane' || id === 'healPane') refreshSpotVisualization();
  $('appShell').classList.toggle('grid-info-open', S.viewMode !== 'detail' && id === 'infoPane');
  if (S.viewMode !== 'detail') { _gridLayoutKey = ''; requestAnimationFrame(renderGrid); }
  let activeButton = null;
  document.querySelectorAll('.panel-pane').forEach((p) => p.classList.toggle('on', p.id === id));
  document.querySelectorAll('.tool-btn').forEach((button) => {
    const selected = button.dataset.pane === id;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
    if (selected) activeButton = button;
  });
  if (id === 'cropPane') { beginCropSession(); setCropMode(true); }
  else if (S.cropping) setCropMode(false);
  S.editGesture = null;
  $('editOverlay').classList.toggle('active', !!cur() && (id === 'maskPane' || id === 'healPane'));
  if (compareEditingBlocked()) setCompareActive(false);
  else syncCompareControl();
  if (id === 'maskPane') syncMaskPanel();
  if (id === 'healPane') syncHealPanel();
  if (id === 'editPane') syncOpticsPanel();
  if (id === 'infoPane') METADATA?.refresh(cur()?.name, true);
  if (id === 'historyPane') HISTORY?.refresh(cur()?.name, true);
  const foldedSection = $(requestedPane);
  if (foldedSection instanceof HTMLDetailsElement && requestedPane !== id) {
    foldedSection.open = true;
  }
  drawEditOverlay();
  requestAnimationFrame(() => {
    if (S.activePane !== id) return;
    panel.scrollTop = paneScrollPositions.get(id) || 0;
    activeButton?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
  scheduleNativeMenuState();
  savePrefs();
}

/* ---------------------------------------------------------------- crop */
function cropSourceSize() {
  const im = cur();
  let width = +(im?.width || 0);
  let height = +(im?.height || 0);
  if (width > 0 && height > 0) {
    // Catalog dimensions already include EXIF orientation. Apply the requested
    // rotation now: the canvas may still show the previous render or photo.
    if (Math.abs(Math.round((+S.params?.rotate || 0) / 90)) % 2) {
      [width, height] = [height, width];
    }
    return { width, height };
  }
  return { width: +$('cv').width || 0, height: +$('cv').height || 0 };
}

function cropImageAspect() {
  const { width, height } = cropSourceSize();
  return width > 0 && height > 0 ? width / height : 1;
}

function cropOutputRatio() {
  if (S.cropRatio === 'free' || S.cropLocked === false) return null;
  let ratio = S.cropRatio === 'original' ? cropImageAspect()
    : S.cropRatio === 'custom' ? S.cropCustomWidth / S.cropCustomHeight
    : parseFloat(S.cropRatio);
  if (S.cropAspectFlipped) ratio = 1 / ratio;
  return Number.isFinite(ratio) && ratio > 0 ? ratio : null;
}

function cropLayerRatio() {
  const outputRatio = cropOutputRatio();
  return outputRatio ? outputRatio / cropImageAspect() : null;
}

function clampCrop(crop) {
  if (!crop) return null;
  const width = clamp(+crop.w || 0, 0, 1);
  const height = clamp(+crop.h || 0, 0, 1);
  return {
    x: clamp(+crop.x || 0, 0, 1 - width),
    y: clamp(+crop.y || 0, 0, 1 - height),
    w: width,
    h: height,
  };
}

function previewCrop() {
  return S.crop && !S.cropping && !S.cropTransition ? clampCrop(S.crop) : null;
}

function cropViewportSize(availableWidth, availableHeight, sourceWidth, sourceHeight, crop) {
  if (!crop || availableWidth <= 0 || availableHeight <= 0 ||
      sourceWidth <= 0 || sourceHeight <= 0) return null;
  const aspect = (sourceWidth * crop.w) / (sourceHeight * crop.h);
  if (!Number.isFinite(aspect) || aspect <= 0) return null;
  const width = Math.min(availableWidth, availableHeight * aspect);
  return { width, height: width / aspect };
}

function previewSourceX(position, crop = previewCrop()) {
  // Zero disables comparison in both preview engines; do not map it into the
  // left edge of a committed crop.
  if (!crop || position <= 0) return position;
  return crop.x + crop.w * position;
}

function syncCropPresentationNow() {
  const cmp = $('cmp');
  const crop = previewCrop();
  const canvas = $('cv');
  const wrap = $('zoomwrap').getBoundingClientRect();
  const framed = !!cur() && canvas.width > 0 && canvas.height > 0 &&
    wrap.width > 0 && wrap.height > 0;
  cmp.classList.toggle('preview-framed', framed);
  cmp.classList.toggle('crop-framed', S.cropping || !!crop);
  cmp.classList.toggle('crop-committed', !!crop);
  // While cropping the frame is the whole photo under a zoom/pan transform, so
  // its handles may reach past the frame edge and the workspace does the clipping.
  cmp.classList.toggle('is-cropping', S.cropping || !!S.cropTransition);
  if (!framed) {
    cmp.style.removeProperty('width');
    cmp.style.removeProperty('height');
    for (const name of [
      '--crop-source-left', '--crop-source-top',
      '--crop-source-width', '--crop-source-height',
    ]) cmp.style.removeProperty(name);
    scheduleNativeViewportLayout();
    return;
  }

  const frameCrop = crop || { x: 0, y: 0, w: 1, h: 1 };
  // Crop constraints use the requested orientation, but the visible frame must
  // follow the pixels already uploaded. Rotation renders arrive asynchronously.
  const source = { width: canvas.width, height: canvas.height };
  const viewport = cropViewportSize(
    wrap.width, wrap.height, source.width, source.height, frameCrop);
  if (viewport) {
    // The cropping view zooms by resizing this frame rather than transforming
    // it, so the crop chrome keeps its screen size at any zoom.
    const scale = S.cropping || S.cropTransition ? (S.zoom || 1) : 1;
    cropViewState().fit = { width: viewport.width, height: viewport.height };
    cmp.style.width = `${viewport.width * scale}px`;
    cmp.style.height = `${viewport.height * scale}px`;
  }
  cmp.style.setProperty('--crop-source-left', `${-frameCrop.x / frameCrop.w * 100}%`);
  cmp.style.setProperty('--crop-source-top', `${-frameCrop.y / frameCrop.h * 100}%`);
  cmp.style.setProperty('--crop-source-width', `${100 / frameCrop.w}%`);
  cmp.style.setProperty('--crop-source-height', `${100 / frameCrop.h}%`);
  scheduleNativeViewportLayout();
}

function cropForRatio(crop, ratio) {
  if (!ratio) return clampCrop(crop || { x: 0, y: 0, w: 1, h: 1 });
  if (!crop) {
    const width = Math.min(1, ratio);
    const height = Math.min(1, 1 / ratio);
    return { x: (1 - width) / 2, y: (1 - height) / 2, w: width, h: height };
  }
  const area = Math.max(0.0001, crop.w * crop.h);
  let width = Math.sqrt(area * ratio);
  let height = width / ratio;
  if (width > 1) { width = 1; height = 1 / ratio; }
  if (height > 1) { height = 1; width = ratio; }
  const cx = crop.x + crop.w / 2;
  const cy = crop.y + crop.h / 2;
  return clampCrop({ x: cx - width / 2, y: cy - height / 2, w: width, h: height });
}

function syncCropPanel() {
  $('cropRatio').value = S.cropRatio;
  $('cropRatio').disabled = !cur();
  $('cropLock').disabled = !cur();
  $('cropLock').textContent = S.cropLocked ? tr("Locked") : tr("Lock");
  $('cropLock').setAttribute('aria-pressed', String(!!S.cropLocked));
  $('cropLock').title = S.cropLocked ? tr("Unlock aspect ratio") : tr("Lock aspect ratio");
  $('cropSwap').disabled = !cur() || !cropOutputRatio();
  $('cropSwap').setAttribute('aria-pressed', String(!!S.cropAspectFlipped));
  $('cropCustomRatio').hidden = S.cropRatio !== 'custom';
  $('cropCustomWidth').value = S.cropCustomWidth || 3;
  $('cropCustomHeight').value = S.cropCustomHeight || 2;
  $('cropReset').disabled = !S.crop && !(S.params?.rotate % 360) &&
    !['rotate', 'vertical', 'horizontal', 'scale', 'flipHorizontal', 'flipVertical']
      .some((key) => S.optics?.[key] !== OPTICS_DEFAULTS[key]);

  if (!S.crop) {
    $('cropReadout').textContent = tr("Full image");
    $('cropPercent').textContent = tr("100% retained");
    return;
  }
  const { width, height } = cropSourceSize();
  const cropWidth = Math.max(1, Math.round(width * S.crop.w));
  const cropHeight = Math.max(1, Math.round(height * S.crop.h));
  $('cropReadout').textContent = tr("{value} × {value2} px", {value: cropWidth.toLocaleString(), value2: cropHeight.toLocaleString()});
  $('cropPercent').textContent = tr("{value}% retained", {value: Math.round(S.crop.w * S.crop.h * 100)});
  $('cropRect').setAttribute(
    'aria-label',
    tr("Crop selection, {cropWidth} by {cropHeight} pixels. Drag to move; use the handles to resize.", {cropWidth: cropWidth, cropHeight: cropHeight}),
  );
}

function applyCropVisualNow() {
  const r = $('cropRect');
  const layer = $('cropLayer');
  syncCropPresentationNow();
  const selection = S.crop ? clampCrop(S.crop)
    : (S.cropping ? { x: 0, y: 0, w: 1, h: 1 } : null);
  if (!selection) {
    r.style.display = 'none';
    layer.style.setProperty('--crop-x', '0%');
    layer.style.setProperty('--crop-y', '0%');
    layer.style.setProperty('--crop-w', '100%');
    layer.style.setProperty('--crop-h', '100%');
    syncCropPanel();
    return;
  }
  if (S.crop) S.crop = selection;
  layer.style.setProperty('--crop-x', (selection.x * 100) + '%');
  layer.style.setProperty('--crop-y', (selection.y * 100) + '%');
  layer.style.setProperty('--crop-w', (selection.w * 100) + '%');
  layer.style.setProperty('--crop-h', (selection.h * 100) + '%');
  r.style.display = 'block';
  r.style.left = (selection.x * 100) + '%';
  r.style.top = (selection.y * 100) + '%';
  r.style.width = (selection.w * 100) + '%';
  r.style.height = (selection.h * 100) + '%';
  // Keep the photo gliding under the frame: a move drag pans it 1:1, a drawn
  // frame settles into view once the pointer lifts, everything else eases.
  syncCropView({ immediate: cropInteractionKind === 'move', defer: cropInteractionKind === 'draw' });
  syncCropPanel();
}
const cropFrameScheduler = createFrameScheduler(() => applyCropVisualNow());
function applyCropVisual() { cropFrameScheduler.request({ crop: true }); }
function setCropMode(on) {
  if (on && !cur()) return;
  stopZoomMotion({finish: true});
  if (on) setCompareActive(false);
  const next = Boolean(on);
  const changed = S.cropping !== next;
  S.cropping = next;
  // Leaving glides the frame out to its committed framing before the source
  // frame is swapped, so the transition is marked before the presentation sync.
  if (changed && !next) cropViewTransition(false);
  $('cropLayer').classList.toggle('on', next || S.cropTransition === 'exit');
  syncCropPresentationNow();
  if (changed && next) cropViewTransition(true);
  applyCropVisual();
  // Crop editing needs the whole source; the committed result should fit the
  // crop itself. Resetting on either transition makes both states predictable.
  if (changed && !next && !S.cropTransition) zoomReset();
  else if (!changed) applyView();
  if (next) requestAnimationFrame(() => $('cropRect').focus({ preventScroll: true }));
  renderCompare();
  syncCompareControl();
}

/* ---------------------------------------------- crop view (centred frame) */
// While cropping, the crop frame stays centred in the workspace and the photo
// zooms and pans underneath it. The on-screen frame is the geometric mean of
// the crop's fitted size and the workspace (zoom bias 0.5), so a dragged handle
// stays under the pointer while the rest of the frame glides toward the centre
// and the photo swells to follow. Dragging inside moves the photo under the
// frame. Entering and leaving ease between the committed framing and this view.
let cropInteractionKind = null;
let cropPointerRefresh = null;
function cropViewState() {
  return (cropViewState.value ||= {
    bias: 0.5, dimAlpha: 0.62, tau: 90, transitionTau: 70, pad: 14,
    target: null, dimTarget: null, dim: null, photo: null, fit: null,
    running: false, lastAt: 0, onSettle: null,
  });
}
function cropViewPrefersImmediate() {
  return typeof matchMedia === 'function' &&
    matchMedia('(prefers-reduced-motion: reduce)').matches;
}
function cropViewWrapKey() {
  const rect = $('zoomwrap')?.getBoundingClientRect?.();
  return rect ? `${Math.round(rect.width)}x${Math.round(rect.height)}` : '';
}
function cropViewFrame() {
  const rect = $('zoomwrap')?.getBoundingClientRect?.();
  const fit = cropViewState().fit;
  const width = fit?.width || 0;
  const height = fit?.height || 0;
  if (!rect || !(rect.width > 0) || !(rect.height > 0) || !(width > 0) || !(height > 0)) return null;
  return { wrapWidth: rect.width, wrapHeight: rect.height, width, height };
}
function cropFitScale(crop, frame) {
  // How much the crop could grow, relative to the whole photo's fit, before it
  // would leave the workspace. The whole photo is 1 by construction.
  const full = Math.min(frame.wrapWidth / frame.width, frame.wrapHeight / frame.height);
  const part = Math.min(frame.wrapWidth / (frame.width * crop.w),
    frame.wrapHeight / (frame.height * crop.h));
  return Math.max(1, part / full);
}
function cropViewTarget(crop, bias = cropViewState().bias) {
  const frame = cropViewFrame();
  if (!frame) return null;
  const c = clampCrop(crop || { x: 0, y: 0, w: 1, h: 1 });
  if (!(c.w > 0) || !(c.h > 0)) return null;
  // The cropping view keeps a small margin so handles on the photo's edge
  // stay inside the workspace; the committed framing (bias 1) uses none.
  const pad = bias < 1 ? cropViewState().pad : 0;
  const base = Math.min(1, (frame.wrapWidth - 2 * pad) / frame.width,
    (frame.wrapHeight - 2 * pad) / frame.height);
  const zoom = Math.pow(cropFitScale(c, frame), bias) * base;
  return {
    zoom,
    panX: -frame.width * (c.x + c.w / 2 - 0.5) * zoom,
    panY: -frame.height * (c.y + c.h / 2 - 0.5) * zoom,
  };
}
function cropViewBackgroundRGB() {
  let value = '';
  if (typeof getComputedStyle === 'function' && typeof document !== 'undefined') {
    value = getComputedStyle(document.documentElement).getPropertyValue('--viewer-bg').trim();
  }
  const hex = /^#([0-9a-f]{6})$/i.exec(value)?.[1] || '121212';
  return [0, 2, 4].map((offset) => parseInt(hex.slice(offset, offset + 2), 16));
}
function cropViewDimStyle(mix, alpha) {
  const layer = $('cropLayer');
  if (!layer?.style?.setProperty) return;
  const rgb = cropViewBackgroundRGB().map((channel) => Math.round(channel * mix));
  layer.style.setProperty('--crop-dim-rgb', rgb.join(' '));
  layer.style.setProperty('--crop-dim-alpha', alpha.toFixed(3));
}
function snapCropView() {
  const state = cropViewState();
  const target = state.target;
  state.running = false;
  if (!target) return;
  S.zoom = target.zoom; S.panX = target.panX; S.panY = target.panY;
  if (state.dimTarget) {
    state.dim = { ...state.dimTarget };
    cropViewDimStyle(state.dim.mix, state.dim.alpha);
  }
  applyViewNow();
  const settle = state.onSettle;
  state.onSettle = null;
  if (settle) settle();
}
function stepCropView(now) {
  const state = cropViewState();
  const target = state.target;
  if (!state.running || !target) { state.running = false; return; }
  if (!S.cropping && S.cropTransition !== 'exit') { state.running = false; return; }
  const dt = state.lastAt ? Math.min(64, now - state.lastAt) : 16;
  state.lastAt = now;
  const k = 1 - Math.exp(-dt / state.tau);
  S.zoom += (target.zoom - S.zoom) * k;
  S.panX += (target.panX - S.panX) * k;
  S.panY += (target.panY - S.panY) * k;
  let dimDone = true;
  if (state.dimTarget) {
    state.dim ||= { ...state.dimTarget };
    state.dim.alpha += (state.dimTarget.alpha - state.dim.alpha) * k;
    state.dim.mix += (state.dimTarget.mix - state.dim.mix) * k;
    cropViewDimStyle(state.dim.mix, state.dim.alpha);
    dimDone = Math.abs(state.dimTarget.alpha - state.dim.alpha) < 0.01 &&
      Math.abs(state.dimTarget.mix - state.dim.mix) < 0.01;
  }
  const settled = dimDone && Math.abs(target.zoom - S.zoom) < 0.002 * target.zoom &&
    Math.abs(target.panX - S.panX) < 0.5 && Math.abs(target.panY - S.panY) < 0.5;
  if (settled) { snapCropView(); return; }
  applyViewNow();
  // A held handle stays under the pointer while the photo glides beneath it.
  if (cropInteractionKind === 'resize' && cropPointerRefresh) cropPointerRefresh();
  requestAnimationFrame(stepCropView);
}
function applyCropView(target, options = {}) {
  const { immediate = false, dim = null, onSettle, tau = null, essential = false } = options;
  const state = cropViewState();
  if (!target) return;
  state.target = target;
  state.tau = tau || 90;
  if (dim) state.dimTarget = dim;
  // A re-target (a drag step, a workspace resize) keeps any pending completion.
  if (onSettle !== undefined) state.onSettle = onSettle;
  S.zoomMode = 'crop';
  S.targetPixelScale = null;
  // Reduced motion skips the decorative enter/leave glides; the short easing
  // that follows a handle drag is direct-manipulation feedback and stays.
  if (immediate || (!essential && cropViewPrefersImmediate())) { snapCropView(); return; }
  if (state.running) return;
  state.running = true;
  state.lastAt = 0;
  requestAnimationFrame(stepCropView);
}
function syncCropView({ immediate = false, defer = false } = {}) {
  if (!S.cropping || S.cropTransition) return;
  const state = cropViewState();
  const photo = cur()?.name || null;
  const changedPhoto = state.photo !== photo;
  state.photo = photo;
  if (defer) return;
  const target = cropViewTarget(S.crop);
  if (!target) return;
  applyCropView(target, {
    immediate: immediate || changedPhoto,
    dim: { mix: 0, alpha: state.dimAlpha },
    essential: true,
  });
}
function finishCropViewExit() {
  if (S.cropTransition !== 'exit') return;
  S.cropTransition = null;
  $('cropLayer').classList.remove('exiting');
  if (!S.cropping) $('cropLayer').classList.remove('on');
  syncCropPresentationNow();
  applyCropVisual();
  if (!S.cropping) zoomReset();
}
function cropViewTransition(entering) {
  const state = cropViewState();
  const layer = $('cropLayer');
  state.onSettle = null;
  state.running = false;
  state.wrapKey = cropViewWrapKey();
  layer.classList.remove('exiting');
  if (entering) {
    S.cropTransition = null;
    state.photo = cur()?.name || null;
    state.target = null;
    const committed = S.crop ? clampCrop(S.crop) : null;
    const start = committed ? cropViewTarget(committed, 1) : null;
    S.zoomMode = 'crop';
    S.targetPixelScale = null;
    if (start) {
      // Start exactly where the committed crop was framed; the crop visual
      // then eases the photo out to the cropping view.
      S.zoom = start.zoom; S.panX = start.panX; S.panY = start.panY;
      state.dim = { mix: 1, alpha: 1 };
    } else {
      S.zoom = 1; S.panX = 0; S.panY = 0;
      state.dim = { mix: 0, alpha: state.dimAlpha };
    }
    state.dimTarget = { ...state.dim };
    cropViewDimStyle(state.dim.mix, state.dim.alpha);
    // Paint the starting view in this frame; the crop visual eases from here.
    applyViewNow();
    return;
  }
  const target = S.crop ? cropViewTarget(clampCrop(S.crop), 1) : null;
  if (!target || cropViewPrefersImmediate()) {
    S.cropTransition = null;
    return;
  }
  S.cropTransition = 'exit';
  layer.classList.add('exiting');
  applyCropView(target, {
    dim: { mix: 1, alpha: 1 }, onSettle: finishCropViewExit, tau: state.transitionTau,
  });
}

function restoreCropChoices(choices) {
  S.cropRatio = choices?.ratio || 'free';
  S.cropLocked = choices?.locked || false;
  S.cropAspectFlipped = choices?.flipped || false;
  S.cropCustomWidth = choices?.width || 3;
  S.cropCustomHeight = choices?.height || 2;
  if (cur()) cur().cropChoices = choices ? cloneValue(choices) : null;
}
function rememberCropChoices() {
  if (cur()) cur().cropChoices = {
    ratio: S.cropRatio, locked: S.cropLocked, flipped: S.cropAspectFlipped,
    width: S.cropCustomWidth, height: S.cropCustomHeight,
  };
}
function applyCropRatioChoice() {
  const ratio = cropLayerRatio();
  if (ratio) {
    pushUndo();
    S.crop = cropForRatio(S.crop, ratio);
    applyCropVisual();
    saveState();
  }
  rememberCropChoices();
  syncCropPanel();
}
function setCropRatio(mode) {
  if (!cur()) return;
  S.cropRatio = mode;
  S.cropLocked = mode !== 'free';
  S.cropAspectFlipped = false;
  applyCropRatioChoice();
}

/* ------------------------------------------------------------ navigation */
let navigationGeneration = 0;
let lastUserNavigationAt = 0;

document.addEventListener('pointerdown', (event) => {
  if (event.target.closest('#grid, #strip, #folderTree, .source-block')) {
    lastUserNavigationAt = performance.now();
  }
}, true);
document.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    lastUserNavigationAt = performance.now();
  }
}, true);

function normalizeLibraryImage(im, stateLoaded = !S.catalogEnabled) {
  return {
    ...im,
    sourceName: im.sourceName || im.relpath || im.name,
    date: im.date || im.captureTime || im.mtime,
    catalogId: im.catalogId || (Number.isInteger(im.id) ? im.id : null),
    stateLoaded: im.stateLoaded ?? stateLoaded,
    keywords: Array.isArray(im.keywords) ? im.keywords : [],
    versions: Array.isArray(im.versions) ? im.versions : [],
    masks: normalizeMasks(im.masks),
    heals: normalizeHeals(im.heals),
    optics: normalizeOptics(im.optics),
  };
}

function showCurrentImage(im) {
  stopZoomMotion({finish: true});
  photoPanKey = JSON.stringify([S.rootFolder, im.name, im.recoverySourceKey || im.fileKey || null]);
  S.editingName = im.name;
  $('panel').inert = false;
  $('cmp').inert = false;
  setRenderPresentation('pending', im.name);
  const pending = editSaveQueue.getPending(im.name);
  // A recovery awaiting identity validation must not be shown on a replacement
  // original. Ordinary unsaved edits keep their existing optimistic display.
  if (pending && !pending.expectedRecoverySourceKey) Object.assign(im, pending.state);
  const hadSavedParams = !!im.params;
  S.params = normalizeFilmParams(im.params);
  const isRaw = im.raw === true || isRawInput();
  const rawDefaults = (isRaw && !im.hasEdits && !im.grade) ? { sharpness: 0.25, colorNoise: 0.25 } : {};
  S.grade = { ...(S.newPhotoGradeDefaults || GRADE_DEFAULTS), ...rawDefaults, ...(im.grade || {}) };
  S.crop = im.crop || null;
  S.preset = cloneValue(im.preset || null);
  presetAmountGesture = null;
  S.masks = normalizeMasks(im.masks);
  S.heals = normalizeHeals(im.heals);
  S.optics = normalizeOptics(im.optics);
  S.selectedMaskId = S.masks[0]?.id || null;
  S.selectedHealId = S.heals[0]?.id || null;
  S.maskCreateOpen = !S.masks.length;
  S.maskRefineMode = selectedMask()?.type === 'brush' ? 'add' : null;
  S.localPinsVisible = true;
  if (im.width && im.height) {
    const maxDim = 1100;
    const scale = Math.min(1.0, maxDim / Math.max(im.width, im.height));
    const targetW = Math.round(im.width * scale);
    const targetH = Math.round(im.height * scale);
    if ($('cv').width !== targetW || $('cv').height !== targetH) {
      $('cv').width = targetW;
      $('cv').height = targetH;
      applyCropVisual();
      scheduleNativeViewportLayout();
    }
  }
  S.editGesture = null; S.maskTextureDirty = true; S.lensProfile = null;
  S.exif = {};
  S.rawDefault = null;
  S.pointColorPick = false; S.maskColorPick = false; S.wbPick = false;
  $('wbBtn').classList.remove('on');
  $('cmp').classList.remove('wb-picking', 'color-picking');
  restoreCropChoices(im.cropChoices);
  Object.assign(S, photoUndo.activate(im.name));
  updateUndoRedoButtons();
  syncControls(); syncGrade(); syncMaskPanel(); syncHealPanel(); syncOpticsPanel();
  $('editOverlay').classList.toggle('active', S.activePane === 'maskPane' || S.activePane === 'healPane');
  applyView();
  if (S.activePane === 'cropPane') beginCropSession();
  setCropMode(S.activePane === 'cropPane');
  restorePhotoPan();
  setCompareActive(false);
  S.originalImageName = null;
  browserOriginal = null;
  browserOriginalTextureURL = null;
  S.gl?.clearOriginalImage();
  $('orig').removeAttribute('src');
  $('currentName').textContent = im.availability === 'cloud-only'
    ? tr('{name} · Cloud only — download in Finder and rescan', {name: displayName(im)}) : displayName(im);
  syncPairControls();
  $('rawCameraDefaultStatus').textContent = isRawInput()
    ? tr('Checking camera default…') : tr('RAW originals only.');
  updateLoupeInfoOverlay();
  syncAIPhoto();
  loadLensProfile(im.name);
  loadRawCameraDefault(im.name, hadSavedParams);
  refreshLists();
  showExif(im.name);
  syncCurveFromGrade();
  syncHsl();
  renderKeywords();
  renderVersions();
  _lastHistorySnapshot = editHistorySnapshot();
  if (HISTORY) HISTORY.refresh(im.name);
  PRESET_BROWSER?.refresh();
  if (METADATA) METADATA.refresh(im.name);
  presentVideo(im);
  broadcastToLoupe(im);
  if (S.viewMode === 'detail') renderFilm(0);
}

const loupeChannel = new BroadcastChannel('lighttable-loupe');

function broadcastToLoupe(photo) {
  if (!photo) {
    loupeChannel.postMessage({ type: 'sync', name: null });
    return;
  }
  const exif = S.exif || {};
  const metaParts = [
    exif.LensModel || exif.LensID,
    exif.FocalLength ? `${exif.FocalLength}mm` : '',
    exif.FNumber ? `f/${exif.FNumber}` : '',
    exif.ExposureTime ? `${exif.ExposureTime}s` : '',
    exif.ISO ? `ISO ${exif.ISO}` : '',
  ].filter(Boolean).join(' · ');
  loupeChannel.postMessage({
    type: 'sync',
    name: photo.name,
    metadata: metaParts,
    /* The server marks this response immutable for a year, so the URL has to
     * carry the file's identity. Without it an external edit to the original
     * leaves the loupe showing the previous frame for the life of the cache. */
    url: `/api/orig?name=${encodeURIComponent(photo.name)}&w=${sourceLongEdge(photo)}&v=2`
      + `&key=${encodeURIComponent(photo.fileKey || photo.mtime || '')}`,
  });
}

loupeChannel.onmessage = (event) => {
  if (event.data?.type === 'ready') {
    broadcastToLoupe(cur());
  }
};


async function go(i) {
  if (i < 0 || i >= S.images.length) return;
  stopZoomMotion({finish: true});
  rememberPhotoPan();
  cropSession = null;
  if (cur()) {
    if (globalThis._controlDirty && typeof saveState === 'function') {
      saveState();
    }
    const outgoing = cur().name;
    if (isStateLoaded(cur())) {
      try {
        S.priorPhotoSettings = { ...JSON.parse(snapshot()), sourceName: outgoing };
      } catch (_) {}
    }
    void editSaveQueue.flush(outgoing).then(() => HISTORY?.flush(outgoing)).catch(() => {});
  }
  lastNavigationDirection = i >= S.idx ? 1 : -1;
  S.idx = i;
  $('panel').inert = true;
  $('cmp').inert = true;
  syncPhotoActions();
  const im = cur();
  CAPTURE_TIME?.selectionChanged();
  const generation = ++navigationGeneration;
  ++S.seq;
  if (NATIVE_PREVIEW) {
    postNative('nativeNavigate', { generation: S.seq });
    postNative('nativePreloadReset', { epoch: generation });
  }
  clearTimeout(renderTimer);
  clearTimeout(settleRenderTimer);
  setRenderPresentation('pending', im.name);
  if (!isStateLoaded(im)) {
    try {
      await prefetchState(im);
    } catch (_) {
      // The lean row is still safe to display with defaults. Leave it marked
      // unloaded so a later navigation can retry a transient catalog read.
    }
  }
  if (generation !== navigationGeneration || cur() !== im) return;
  showCurrentImage(im);
  prefetch();
}

function scrollGridToImage(image) {
  if (!image || !$('library').classList.contains('show') || !_gridLayout) return;
  const position = _gridLayout.positions[_gridIndexByName.get(image.name)];
  if (!position) return;
  const top = gridViewportTop();
  const inset = $('library').querySelector('.library-head').offsetHeight;
  if (position.top < top + inset) $('library').scrollTop += position.top - top - inset;
  else if (position.bottom > top + $('library').clientHeight) {
    $('library').scrollTop += position.bottom - top - $('library').clientHeight;
  }
}

function goRelative(direction) {
  const list = visible();
  if (!list.length) return;
  const currentIndex = list.indexOf(cur());
  const nextIndex = currentIndex >= 0
    ? currentIndex + direction
    : (direction > 0 ? 0 : list.length - 1);
  const next = list[nextIndex];
  if (next) {
    scrollGridToImage(next);
    go(S.images.indexOf(next));
  }
}

/* Up and down in the grid move the selection a row, the way culling expects;
 * the browser would otherwise scroll the library and leave the photo behind.
 * Returns false when there is no grid to move within, so the caller can let
 * the key do its ordinary thing. */
function goGridRow(direction) {
  if (!$('library').classList.contains('show')) return false;
  const list = visible();
  if (!list.length) return false;
  // A stale layout would move by the wrong number of columns, so lay out
  // first; the work is the same as one scroll frame and mostly short-circuits.
  renderGrid();
  if (!_gridLayout || _gridList !== list) return false;
  const index = _gridIndexByName.get(cur()?.name);
  const next = index === undefined
    ? (direction > 0 ? list[0] : list[list.length - 1])
    : list[gridRowNeighbour(_gridLayout, index, direction)];
  if (!next) return false;
  scrollGridToImage(next);
  go(S.images.indexOf(next));
  return true;
}

const _rawDefaultCache = new Map();
async function loadRawCameraDefault(name, hadSavedParams) {
  if (cur()?.name !== name || cur()?.raw !== true) {
    S.rawDefault = null;
    syncControls();
    return;
  }
  try {
    let result = _rawDefaultCache.get(name);
    if (!result) {
      result = await fetch(`/api/raw-default?name=${encodeURIComponent(name)}`)
        .then((response) => response.json());
      _rawDefaultCache.set(name, result);
    }
    if (cur()?.name !== name) return;
    S.rawDefault = result;
    $('rawCameraDefaultStatus').textContent = result.settings ? tr("{resultLabel} default is saved.", {resultLabel: result.label}) : tr("{resultLabel} uses the app defaults.", {resultLabel: result.label});
    if (!hadSavedParams && result.settings) {
      S.params = normalizeFilmParams({ ...S.params, ...result.settings });
      cur().params = { ...S.params };
      syncControls();
      renderFilm(0);
    } else {
      syncControls();
    }
  } catch (_) {
    if (cur()?.name === name) {
      S.rawDefault = null;
      $('rawCameraDefaultStatus').textContent = tr("Camera default unavailable.");
      syncControls();
    }
  }
}

const _lensProfileCache = new Map();
async function loadLensProfile(name) {
  if (_lensProfileCache.has(name)) {
    if (cur()?.name === name) {
      const cached = _lensProfileCache.get(name);
      S.lensProfile = cached.profile;
      if (!S.optics.profileOverride && (!cur()?.optics || !cur()?.hasEdits) &&
          cached.found && cached.reason === "Exact camera and lens metadata match.") {
        S.optics.profileEnabled = true;
      }
      syncOpticsPanel();
    }
    return;
  }
  try {
    const result = await fetch(`/api/lens-profile?name=${encodeURIComponent(name)}`).then((response) => response.json());
    const profile = result.found ? result.profile : null;
    _lensProfileCache.set(name, { ...result, profile });
    if (cur()?.name === name) {
      S.lensProfile = profile;
      if (!S.optics.profileOverride && (!cur()?.optics || !cur()?.hasEdits) &&
          result.found && result.reason === "Exact camera and lens metadata match.") {
        S.optics.profileEnabled = true;
      }
      syncOpticsPanel();
    }
  } catch (_) {
    if (cur()?.name === name) S.lensProfile = null;
  }
  syncOpticsPanel();
}
/* Selected photos that are still in the current view.
 *
 * Nothing clears the selection when the filter, folder, or search changes, so
 * resolving it against the whole library let flags, ratings and labels land on
 * photos that were no longer on screen while the marking bar still counted
 * them. `openSurvey` already intersects the same way. */
function selectedVisibleTargets() {
  if (!S.msel.size) return [];
  const listed = new Set(visible().map((image) => image.name));
  return [...S.msel]
    .map((name) => S.images.find((image) => image.name === name))
    .filter((image) => image && listed.has(image.name));
}

function markingTargets() {
  if (SELECTION_REQUEST?.pending) return [];
  const survey = surveyTarget();
  if (survey) return [survey];
  if (S.msel.size || S.viewMode !== 'detail') {
    if (S.msel.size) return selectedVisibleTargets();
    const ordered = visible();
    return ordered.includes(cur()) ? [cur()] : (ordered[0] ? [ordered[0]] : []);
  }
  return cur() ? [cur()] : [];
}

function commonMarkValue(targets, field, fallback) {
  if (!targets.length) return fallback;
  const first = targets[0][field] ?? fallback;
  return targets.every((image) => (image[field] ?? fallback) === first)
    ? first : null;
}

function persistMark(targets, entry) {
  for (const image of targets) {
    CULL_BATCH.noteFlagChange(image.name, entry);
    const pending = editSaveQueue.getPending(image.name);
    if (image.stateLoadEdits) Object.assign(image.stateLoadEdits, cloneValue(entry));
    editSaveQueue.enqueue(image.name, { sourceKey: image.recoverySourceKey || null, ...pending,
      state: { ...pending?.state, name: image.name, ...entry } }, { immediate: true });
  }
  void flushEditSaves();
}

function syncCullBars() {
  const targets = markingTargets();
  const active = targets[0] || null;
  const status = commonMarkValue(targets, 'status', 'pending');
  const rating = commonMarkValue(targets, 'rating', 0);
  const multiple = targets.length > 1;
  let title = tr("No photo selected");
  let detail = '';
  if (active && multiple) {
    title = trn('{count} photo selected', '{count} photos selected', targets.length);
    detail = tr('Flags and ratings apply to selection');
  } else if (active) {
    title = displayName(active);
    if (SURVEY && SURVEY.isOpen) {
      const index = Math.max(0, SURVEY.names.indexOf(active.name));
      detail = tr("Survey · {value} of {SURVEYNamesLength}", {value: index + 1, SURVEYNamesLength: SURVEY.names.length});
    } else {
      const ordered = visible();
      const index = ordered.indexOf(active);
      detail = (index >= 0 ? tr("{value} of {orderedLength}", {value: index + 1, orderedLength: ordered.length}) : tr("Current photo"));
    }
  }
  if (SELECTION_REQUEST?.pending) { title = 'Loading the full selection…'; detail = `${S.images.length} of ${S.catalogTotal} photos loaded`; }
  else if (S.catalogLoadError) detail += ' · Catalog loading paused; Select All retries';
  const linkedCount = linkedMetadataTargets(targets).length;
  if (linkedCount > targets.length) detail += trn(" · {count} paired file linked", " · {count} paired files linked", linkedCount - targets.length, {value: linkedCount - targets.length});
  document.querySelectorAll('[data-cull-context-title]').forEach((element) => {
    element.textContent = title;
  });
  document.querySelectorAll('[data-cull-context-detail]').forEach((element) => {
    element.textContent = detail;
  });
  document.querySelectorAll('[data-cull-status]').forEach((button) => {
    const on = status !== null && button.dataset.cullStatus === status;
    button.disabled = !targets.length;
    button.classList.toggle('on', on);
    button.classList.toggle('mixed', multiple && status === null);
    button.setAttribute('aria-pressed', String(on));
  });
  document.querySelectorAll('.cullbar-rating').forEach((group) => {
    group.classList.toggle('mixed', multiple && rating === null);
  });
  document.querySelectorAll('[data-cull-rating]').forEach((button) => {
    const value = +button.dataset.cullRating;
    const on = rating !== null && value <= rating;
    button.disabled = !targets.length;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
  });
  document.querySelectorAll('[data-cull-auto]').forEach((button) => {
    const on = APP_PREFS.autoAdvance !== false;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
  });
}

function setStatus(st) {
  const targets = markingTargets(); if (!targets.length) return;
  const next = st !== 'pending' && targets.every((image) => image.status === st)
    ? 'pending' : st;
  /* Read the position before the mark. A flag or rating filter drops the photo
   * out of the visible list, and its old position is where the next one lands. */
  const resumeAt = markResumeIndex(targets[0]);
  const linked = linkedMetadataTargets(targets);
  for (const image of linked) image.status = next;
  S.libraryRevision = (S.libraryRevision || 0) + 1;
  invalidateVisibleCache();
  persistMark(linked, { status: next });
  /* Unflagging is a correction, not a decision, so it does not advance. */
  if (targets.length === 1 && next !== 'pending' && APP_PREFS.autoAdvance !== false) {
    advanceAfterMark(targets[0], resumeAt);
  } else if (SURVEY && SURVEY.isOpen) {
    refreshLists();
  } else {
    refreshFilteredView(resumeAt);
  }
}
function setRating(r, advance = false) {
  const targets = markingTargets(); if (!targets.length) return;
  const next = targets.every((image) => (image.rating || 0) === r) ? 0 : r;
  const resumeAt = markResumeIndex(targets[0]);
  const linked = linkedMetadataTargets(targets);
  for (const image of linked) image.rating = next;
  S.libraryRevision = (S.libraryRevision || 0) + 1;
  invalidateVisibleCache();
  persistMark(linked, { rating: next });
  if (targets.length === 1 && (advance || APP_PREFS.autoAdvance !== false)) {
    advanceAfterMark(targets[0], resumeAt);
  } else if (SURVEY && SURVEY.isOpen) {
    refreshLists();
  } else {
    refreshFilteredView(resumeAt);
  }
}

function setLabel(label) {
  const targets = markingTargets(); if (!targets.length) return;
  const labelValue = cleanLabel(label);
  const next = targets.every(image => cleanLabel(image.label) === labelValue) ? 'none' : labelValue;
  const linked = linkedMetadataTargets(targets);
  for (const image of linked) image.label = next;
  S.libraryRevision = (S.libraryRevision || 0) + 1; invalidateVisibleCache();
  persistMark(linked, { label: next });
  if (SURVEY && SURVEY.isOpen) refreshLists(); else refreshFilteredView();
}

/* When survey is open the marking keys act on its active cell, not on the
 * photo the editor happens to be showing. */
function surveyTarget() {
  if (!SURVEY || !SURVEY.isOpen) return null;
  return S.images.find((image) => image.name === SURVEY.active) || null;
}

function advanceAfterMark(im, resumeAt = -1) {
  if (SURVEY && SURVEY.isOpen) { SURVEY.step(1); refreshLists(); return; }
  const next = photoAfterMark(visible(), im, resumeAt);
  if (next) {
    go(S.images.indexOf(next));
  } else {
    refreshLists();
  }
}

// Every local state mutation joins the same per-photo chain. A partial patch
// must retain any full recipe still waiting to save, including after a failure.
function enqueuePhotoPatch(im, patch, { historyLabel } = {}) {
  CULL_BATCH.noteFlagChange(im.name, patch);
  const pending = editSaveQueue.getPending(im.name);
  const state = { ...pending?.state, ...cloneValue(patch), name: im.name };
  Object.assign(im, cloneValue(patch));
  if (im.stateLoadEdits) Object.assign(im.stateLoadEdits, cloneValue(patch));
  const history = pending?.history ? {
    label: historyLabel || pending.history.label,
    state: { ...pending.history.state },
  } : historyLabel ? { label: historyLabel, state: {
    params: im.params, grade: im.grade, crop: im.crop || null,
    masks: im.masks || [], heals: im.heals || [], optics: im.optics,
  } } : null;
  if (history) {
    for (const key of ['params', 'grade', 'crop', 'masks', 'heals', 'optics']) {
      if (Object.prototype.hasOwnProperty.call(patch, key)) history.state[key] = cloneValue(patch[key]);
    }
  }
  editSaveQueue.enqueue(im.name, { state, history, sourceKey: im.recoverySourceKey || null }, { immediate: true });
}

/* saveState() always writes the photo the editor has open; marking from the
 * survey needs to write a different one. */
function saveStateFor(im, immediate = false) {
  if (im === cur() && S.editingName === im.name) return saveState(immediate);
  const pending = editSaveQueue.getPending(im.name);
  if (im.stateLoadEdits) Object.assign(im.stateLoadEdits, {
    status: im.status, rating: im.rating, label: cleanLabel(im.label) });
  editSaveQueue.enqueue(im.name, {
    ...pending, sourceKey: im.recoverySourceKey || null,
    state: { ...pending?.state, name: im.name, status: im.status,
      rating: im.rating, label: cleanLabel(im.label) },
  }, { immediate });
  return immediate ? flushEditSaves() : Promise.resolve(true);
}

/* ---------------------------------------------------- assisted culling */
/* Verdicts arrive with the index; this only decides what to show and, when
 * asked outright, what to flag. Nothing here writes a flag on its own. */

const CULL_LABELS = {
  subjectSharpness: tr("Subject sharpness"), eyeSharpness: tr("Eye sharpness"),
  eyesOpen: tr("Eyes open"), exposure: tr("Exposure issues"),
  misfire: tr("Misfires"), document: tr("Documents"),
};

function chosenCull(group) {
  return group.filter((name) => S.cull.on[name]);
}

function matchesCullReview(image) {
  if (S.cull.review === 'all') return true;
  const criteria = chosenCull(
    S.cull.review === 'selects' ? CULL_SELECT : CULL_REJECT);
  if (!criteria.length) return false;
  return cullMatches(image.ai, criteria);
}

function syncCullPanel() {
  const scope = collectionScope().filter((im) => im.kind !== 'video');
  const scored = scope.filter((im) => im.ai?.cull?.criteria).length;
  const tally = cullTally(scope, [...CULL_SELECT, ...CULL_REJECT]);
  const ready = S.ai.enabled && scored > 0;

  for (const name of [...CULL_SELECT, ...CULL_REJECT]) {
    const box = $(`cull${name[0].toUpperCase()}${name.slice(1)}`);
    if (!box) continue;
    box.checked = !!S.cull.on[name];
    box.disabled = !ready;
    box.closest('.check-row')?.classList.toggle('unavailable', !ready);
    const count = $(`cull${name[0].toUpperCase()}${name.slice(1)}Count`);
    if (count) {
      const entry = tally[name];
      count.textContent = !ready ? '' : entry.judged ? `${entry.yes}` : tr("not judged");
    }
  }

  $('cullEnableIndex').hidden = !!S.ai.enabled;
  $('cullIntroText').textContent = !S.ai.enabled ? '' : !scored
    ? (S.ai.running ? tr('Scoring photos as the index reaches them.') : tr('No photos have been scored yet.'))
    : tr('{scored} of {scopeLength} photos scored. Nothing is flagged until you ask for it.', {scored, scopeLength: scope.length});

  for (const button of document.querySelectorAll('.cull-review button')) {
    button.classList.toggle('on', button.dataset.review === S.cull.review);
    button.disabled = !ready;
  }
  const reviewing = S.cull.review !== 'all';
  const shown = reviewing ? visible() : [];
  const group = S.cull.review === 'selects' ? CULL_SELECT : CULL_REJECT;
  const criteria = chosenCull(group).map(name => CULL_LABELS[name]).join(', ');
  $('cullReviewHint').textContent = !reviewing ? tr("Choose Selects or Rejects to review matches before applying flags.")
    : !chosenCull(group).length ? tr("Choose criteria above.")
    : trn("{count} matching photo shown: {criteria}.", "{count} matching photos shown: {criteria}.", shown.length, {criteria});
  const replace = $('cullReplaceFlags').checked;
  const linked = linkedMetadataTargets(shown);
  const picks = cullFlagTargets(linked, 'approved', replace).length;
  const rejects = cullFlagTargets(linked, 'skipped', replace).length;
  $('cullApplyPicks').hidden = S.cull.review !== 'selects';
  $('cullApplyRejects').hidden = S.cull.review !== 'rejects';
  $('cullApplyPicks').disabled = !ready || !picks || CULL_BATCH.busy;
  $('cullApplyRejects').disabled = !ready || !rejects || CULL_BATCH.busy;
  $('cullApplyPicks').textContent = trn("Flag {count} photo as a pick", "Flag {count} photos as picks", picks);
  $('cullApplyRejects').textContent = trn("Flag {count} photo as a reject", "Flag {count} photos as rejects", rejects);
  $('cullReplaceFlags').disabled = CULL_BATCH.busy;
  $('cullFlagHint').hidden = !reviewing;
  $('cullFlagHint').textContent = replace
    ? tr("Existing flags may be replaced. Counts include linked RAW/JPEG files.")
    : tr("Existing flags are preserved. Counts include unflagged linked RAW/JPEG files.");
  $('cullUndo').hidden = !CULL_BATCH.canUndo;
  $('cullUndo').disabled = CULL_BATCH.busy;
  $('cullSimilar').disabled = !ready || CULL_BATCH.busy;

}

function setCullReview(review) {
  S.cull.review = review;
  if (review !== 'all') setViewMode(S.gridViewMode || 'photo', false);
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  refreshLists();
  syncCullPanel();
  savePrefs();
}

function refreshCullFlags() {
  S.libraryRevision = (S.libraryRevision || 0) + 1;
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  refreshLists();
  syncCullPanel();
}

const CULL_BATCH = createCullBatch({
  imageFor: name => S.images.find(image => image.name === name),
  enqueue: (image, patch) => enqueuePhotoPatch(image, patch),
  flush: flushEditSaves,
  changed: refreshCullFlags,
});

async function applyCullFlags(group, status) {
  if (S.cull.review !== (status === 'approved' ? 'selects' : 'rejects')) return;
  if (!chosenCull(group).length) return;
  const targets = linkedMetadataTargets(visible());
  const result = await CULL_BATCH.apply(targets, status, $('cullReplaceFlags').checked);
  if (!result?.saved) return;
  toast(status === 'approved'
    ? trn('Picked {count} photo', 'Picked {count} photos', result.count)
    : trn('Rejected {count} photo', 'Rejected {count} photos', result.count),
    {label: tr('Undo'), run: undoCullFlags});
}

async function undoCullFlags() {
  const result = await CULL_BATCH.undo();
  if (result?.saved) toast(trn('Restored {count} photo flag. Later flag changes are preserved.',
    'Restored {count} photo flags. Later flag changes are preserved.', result.count));
}

let cullSimilarGroups = [], cullSimilarIndex = 0;
function showCullSimilarGroup(step = 0) {
  cullSimilarIndex = Math.max(0, Math.min(cullSimilarGroups.length - 1, cullSimilarIndex + step));
  const group = cullSimilarGroups[cullSimilarIndex]
    ?.map(image => S.images.find(current => current.name === image.name)).filter(Boolean);
  if (!group?.length) return;
  const suggestion = cullSuggestion(group);
  $('cullSimilarNavigation').hidden = false;
  $('surveyKeep').hidden = true;
  $('cullGroupHint').textContent = suggestion
    ? tr('Group {index} of {count}. Suggested: {name}. Based on focus and eye checks; review every frame.',
      {index: cullSimilarIndex + 1, count: cullSimilarGroups.length, name: displayName(suggestion)})
    : tr('Group {index} of {count}. No clear recommendation; compare these photos.',
      {index: cullSimilarIndex + 1, count: cullSimilarGroups.length});
  $('cullSimilarPrevious').disabled = cullSimilarIndex === 0;
  $('cullSimilarNext').disabled = cullSimilarIndex === cullSimilarGroups.length - 1;
  SURVEY.open(group.map(image => image.name), 'survey');
}
$('cullSimilar').onclick = () => {
  cullSimilarGroups = groupSimilarPhotos(collectionScope(), captureSortValue);
  cullSimilarIndex = 0;
  $('cullSimilarHint').textContent = '';
  $('cullSimilarNavigation').hidden = !cullSimilarGroups.length;
  if (cullSimilarGroups.length) showCullSimilarGroup();
  else $('cullSimilarHint').textContent = tr('No similar groups found. Photos need current index analysis and capture times.');
};
$('cullSimilarPrevious').onclick = () => showCullSimilarGroup(-1);
$('cullSimilarNext').onclick = () => showCullSimilarGroup(1);
document.querySelector('.ai-cull-disclosure').addEventListener('toggle', syncCullPanel);
$('cullReplaceFlags').onchange = syncCullPanel;
$('cullUndo').onclick = undoCullFlags;

for (const name of [...CULL_SELECT, ...CULL_REJECT]) {
  const box = $(`cull${name[0].toUpperCase()}${name.slice(1)}`);
  if (!box) continue;
  box.onchange = () => {
    S.cull.on[name] = box.checked;
    setCullReview(CULL_SELECT.includes(name) ? 'selects' : 'rejects');
  };
}
for (const button of document.querySelectorAll('.cull-review button')) {
  button.onclick = () => setCullReview(button.dataset.review);
}
$('cullApplyPicks').onclick = () => applyCullFlags(CULL_SELECT, 'approved');
$('cullApplyRejects').onclick = () => applyCullFlags(CULL_REJECT, 'skipped');

/* ------------------------------------------------------------ local AI */
let aiPollTimer = null;
let aiPollCount = 0;

function syncAIPhoto() {
  const host = $('aiPhotoMetadata');
  if (!host) return;
  const metadata = cur()?.ai;
  if (!S.ai.enabled) {
    host.textContent = tr("Enable the index to create searchable metadata.");
    return;
  }
  if (!metadata) {
    host.textContent = S.ai.running ? tr("Waiting for this photo to be indexed.") : tr("No index metadata is available for this photo.");
    return;
  }
  const lines = [];
  if (metadata.caption) lines.push(metadata.caption);
  if (metadata.tags?.length) lines.push(tr("Tags · {value}", {value: metadata.tags.join(', ')}));
  if (metadata.ocr?.length) lines.push(tr("Visible text · {value}", {value: metadata.ocr.join(' · ')}));
  lines.push(trn("{count} face detected", "{count} faces detected", metadata.faceCount || 0, {value: metadata.faceCount || 0}));
  const called = [...CULL_SELECT, ...CULL_REJECT]
    .map((name) => [name, cullVerdict(metadata, name)])
    .filter(([, entry]) => entry && entry.verdict !== 'unknown')
    .map(([name, entry]) => tr('{criterion} · {verdict} — {detail}',
      {criterion: CULL_LABELS[name], verdict: entry.verdict === 'yes' ? tr('yes') : tr('no'), detail: entry.detail}));
  if (called.length) lines.push('', tr("Culling"), ...called);
  host.textContent = lines.join('\n');
}

function syncAI(status = S.ai) {
  if (!status || status.error) return;
  S.ai = { ...S.ai, ...status };
  const enabled = !!S.ai.enabled;
  const running = !!S.ai.running;
  const capabilities = S.ai.capabilities || {};
  const vision = capabilities.vision || {};
  const foundation = capabilities.foundationModels || {};
  const toggle = $('aiToggle');
  toggle.classList.toggle('on', enabled);
  toggle.setAttribute('aria-pressed', String(enabled));
  toggle.querySelector('.switch-label').textContent = enabled ? tr("On") : tr("Off");
  toggle.disabled = !vision.available && !enabled;
  $('aiVisionStatus').textContent = vision.available ? tr("Available") : (S.serverPlatform && S.serverPlatform !== 'darwin' ? tr("Not available on this platform") : tr("Build required"));
  $('aiFoundationStatus').textContent = foundation.available ? tr("Rich descriptions on") : (foundation.reason || tr("Not available"));
  $('aiToggleDescription').textContent = foundation.available ? tr("Descriptions, objects, scenes, text, and faces") : tr("Objects, scenes, visible text, and faces");
  $('search').placeholder = enabled ? tr("Search photos, contents, text, and faces") : tr("Search photos and keywords");

  const card = $('aiStatusCard');
  card.classList.toggle('running', running);
  card.classList.toggle('ready', enabled && !running && !S.ai.lastError && !S.ai.skipped);
  card.classList.toggle('error', !!S.ai.lastError);
  const fraction = S.ai.total ? clamp(S.ai.completed / S.ai.total, 0, 1) : 0;
  $('aiProgress').style.width = `${fraction * 100}%`;
  if (!enabled) {
    $('aiStatus').textContent = S.ai.indexed ? tr("Paused") : tr("Off");
    $('aiStatusDetail').textContent = S.ai.indexed ? trn("{count} indexed photo remains local until deleted.", "{count} indexed photos remain local until deleted.", S.ai.indexed, {SAiIndexed: S.ai.indexed}) : tr("No background analysis is running.");
  } else if (running) {
    $('aiStatus').textContent = tr("Indexing {SAiCompleted} of {SAiTotal}", {SAiCompleted: S.ai.completed, SAiTotal: S.ai.total});
    $('aiStatusDetail').textContent = ((S.ai.current || tr("Preparing the library…")));
  } else if (S.ai.lastError) {
    $('aiStatus').textContent = tr("{SAiIndexed} photos indexed", {SAiIndexed: S.ai.indexed});
    $('aiStatusDetail').textContent = S.ai.lastError;
  } else {
    $('aiStatus').textContent = S.ai.skipped ? tr('Complete with skipped photos') : tr('Ready');
    $('aiStatusDetail').textContent = trn('{count} photo indexed on this Mac.', '{count} photos indexed on this Mac.', S.ai.indexed);
  }
  if (enabled && !running && S.ai.skipped) {
    $('aiStatusDetail').textContent += ` ${aiSkippedSummary(S.ai)}`;
  }
  $('aiRebuild').disabled = !enabled || !vision.available;
  $('aiClear').disabled = !enabled && !S.ai.indexed && !S.ai.errors;
  syncAIPhoto();
  syncCullPanel();
}

async function refreshAIResults() {
  const response = await fetch('/api/ai-index/results');
  const results = await response.json();
  if (results.error) throw new Error(results.error);
  for (const image of S.images) image.ai = results[image.name] || null;
  S.cull.revision += 1;
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  refreshLists();
  syncAIPhoto();
  syncCullPanel();
}

function scheduleAIStatusPoll(reset = false) {
  if (reset) aiPollCount = 0;
  clearTimeout(aiPollTimer);
  if (++aiPollCount > 3600) return;
  aiPollTimer = setTimeout(async () => {
    try {
      const response = await fetch('/api/ai-index/status');
      const status = await response.json();
      syncAI(status);
      if (status.enabled && !status.scanComplete) {
        scheduleAIStatusPoll();
      } else if (status.enabled) {
        await refreshAIResults();
      }
    } catch (error) {
      toast(tr("Local index: {errorMessage}", {errorMessage: error.message}));
    }
  }, 1000);
}

async function runAIAction(action) {
  $('aiToggle').disabled = true;
  $('cullEnableIndex').disabled = true;
  try {
    const status = await api('/api/ai-index', { action });
    if (status.error) throw new Error(status.error);
    syncAI(status);
    if (status.enabled) scheduleAIStatusPoll(true);
    else {
      clearTimeout(aiPollTimer);
      for (const image of S.images) image.ai = null;
      S.cull.revision += 1;
      S.cull.review = 'all';
      invalidateVisibleCache();
      _stripKey = _gridKey = '';
      refreshLists();
    }
  } catch (error) {
    toast(tr("Local index: {errorMessage}", {errorMessage: error.message}));
  } finally {
    $('cullEnableIndex').disabled = false;
    syncAI();
  }
}

function catalogIdleTurn() {
  return new Promise((resolve) => {
    if ('requestIdleCallback' in window) {
      requestIdleCallback(resolve, { timeout: 500 });
    } else {
      setTimeout(resolve, 80);
    }
  });
}

const LIBRARY_CHUNK_SIZE = 600;

function buildCatalogQuerySpec(extra = {}) {
  const f = $('filter')?.value || 'all';
  const rf = $('ratingFilter')?.value || 'all';
  const kind = $('kindFilter')?.value || 'all';
  const labelFilter = $('labelFilter') ? $('labelFilter').value : 'all';
  const editState = $('editFilter')?.value || 'all';
  const fileTypes = typeof LIBRARY_FILTERS?.types === 'function' ? LIBRARY_FILTERS.types() : [];
  const metadata = typeof LIBRARY_FILTERS?.metadata === 'function' ? LIBRARY_FILTERS.metadata() : {};
  const search = $('search')?.value?.trim() || '';
  const s = $('sort')?.value || 'capture';

  const filter = { ...metadata };
  if (f === 'rated') filter.ratingMin = 1;
  else if (f === 'unrated') filter.unrated = true;
  else if (f === 'edited') filter.editState = 'edited';
  else if (f === 'unedited') filter.editState = 'unedited';
  else if (f === 'virtual') filter.kind = 'virtual';
  else if (f === 'approved' || f === 'skipped' || f === 'pending') filter.status = f;

  if (rf === 'unrated') filter.unrated = true;
  else if (rf !== 'all' && rf !== '0' && !isNaN(+rf)) filter.ratingMin = +rf;

  if (labelFilter !== 'all') filter.label = labelFilter;
  if (kind !== 'all') filter.kind = kind;
  if (editState !== 'all') filter.editState = editState;
  if (fileTypes.length) filter.fileTypes = fileTypes;
  if (search) filter.query = search;

  let scope = 'all';
  let folderId, collectionId;
  const collection = typeof activeCollection === 'function' ? activeCollection() : null;
  if (collection) {
    scope = 'collection';
    collectionId = collection.id;
  } else if (S.activeFolder) {
    scope = 'folder';
    // The server addresses folders by row id; the sidebar holds source-relative
    // paths. A path with no catalog row fails closed as folder 0.
    folderId = activeFolderId() ?? 0;
  }

  const spec = {
    scope,
    filter,
    ...sourceScopeSpec(),
    sort: {
      field: s === 'date' ? 'capture' : s,
      dir: 'desc',
    },
    ...extra,
  };
  if (scope === 'folder') {
    spec.folderId = folderId;
    spec.includeSubfolders = S.includeSubfolders !== false;
  }
  if (collectionId) spec.collectionId = collectionId;

  return spec;
}

let catalogPageTask = null;
function loadRemainingCatalogRows(total) {
  if (catalogPageTask?.images === S.images) return catalogPageTask.promise;
  const images = S.images;
  const generation = ++catalogPageGeneration;
  S.catalogLoadError = '';
  const promise = (async () => {
    let offset = images.length;
    const known = new Set(images.map(image => image.name));
    while (S.catalogEnabled && generation === catalogPageGeneration && images === S.images && offset < total) {
      await catalogIdleTurn();
      const page = await api('/api/catalog/query', {limit: LIBRARY_CHUNK_SIZE, offset, sort: {field: 'capture', dir: 'desc'},
        ...sourceScopeSpec()});
      if (generation !== catalogPageGeneration || images !== S.images) throw new Error(tr('The library changed; select photos again'));
      if (page.error || !Array.isArray(page.items)) throw new Error(page.error || 'Could not load the full catalog');
      total = Number.isFinite(+page.total) ? +page.total : total;
      if (!page.items.length && offset < total) throw new Error(tr('Catalog loading stopped before all photos arrived'));
      for (const row of page.items) {
        if (known.has(row.name)) continue;
        known.add(row.name);
        images.push(normalizeLibraryImage(row, false));
      }
      offset += page.items.length;
      S.catalogTotal = total;
      _stripKey = _gridKey = '';
      refreshLists();
    }
    if (images !== S.images) throw new Error(tr('The library changed; select photos again'));
  })().catch(error => {
    if (images === S.images) { S.catalogLoadError = error.message; syncCullBars(); }
    throw error;
  }).finally(() => {
    if (catalogPageTask?.images === images) catalogPageTask = null;
  });
  catalogPageTask = {images, promise};
  return promise;
}

let catalogPageGeneration = 0;
var catalogScanWatchActive = false;
async function watchCatalogScan() {
  if (!S.catalogEnabled || catalogScanWatchActive) return;
  catalogScanWatchActive = true;
  let observedWork = false;
  try {
    // Ten minutes is a generous bound for a first scan of a very large or
    // network-backed source. The catalog pane remains usable throughout.
    for (let attempt = 0; attempt < 1200; attempt++) {
      const status = await fetch('/api/catalog/scan')
        .then((response) => response.json()).catch(() => null);
      const busy = !!status?.running || (+status?.queued || 0) > 0;
      if (busy) {
        observedWork = true;
      } else if (observedWork) {
        await reloadLibrary();
        if (CATALOG_UI) CATALOG_UI.refresh();
        return;
      } else if (attempt >= 2) {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  } finally {
    catalogScanWatchActive = false;
  }
}

async function chooseEditRecovery(records) {
  const dialog = document.createElement('dialog');
  dialog.className = 'modal'; dialog.id = 'editRecoveryDialog';
  dialog.setAttribute('aria-labelledby', 'editRecoveryTitle');
  dialog.style.color = 'var(--ink)';
  const title = document.createElement('strong'); title.id = 'editRecoveryTitle';
  title.textContent = tr("Recover unsaved edits?");
  const description = document.createElement('p');
  description.textContent = trn('Local recovery found changes for {count} photo. Restoring replaces their saved edits with these recovered changes.',
    'Local recovery found changes for {count} photos. Restoring replaces their saved edits with these recovered changes.', records.length);
  if (records.some(item => item.legacyIdentity)) {
    description.textContent += ' ' + tr('Some drafts use a partial file identity that cannot verify the whole original. Restore these only if the original photos have not been replaced.');
  }
  const list = document.createElement('p');
  list.textContent = records.slice(0, 3).map(item => item.legacyIdentity ? tr('{name} (partial identity)', {name: item.name}) : item.name).join(' · ')
    + (records.length > 3 ? ' …' : '');
  const actions = document.createElement('div'); actions.className = 'modal-actions';
  const discard = document.createElement('button'); discard.textContent = tr("Keep saved edits");
  const restore = document.createElement('button'); restore.className = 'accent-btn';
  restore.textContent = tr("Restore edits");
  actions.append(discard, restore); dialog.append(title, description, list, actions);
  document.body.append(dialog);
  return new Promise(resolve => {
    const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
    discard.onclick = () => finish(false); restore.onclick = () => finish(true);
    // Escape leaves recovery undecided; never silently discard drafts.
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.addEventListener('keydown', event => event.stopPropagation());
    dialog.showModal(); restore.focus();
  });
}
async function initializeEditRecovery(data) {
  const scope = data.catalog?.path || `folder:${data.folder}`;
  let storage = null;
  try { storage = window.localStorage; } catch (_) { /* surfaced by the journal */ }
  editRecovery = createEditRecovery({scope,
    nativeRequest: nativeBridge() ? nativeJournalRequest : null, storage,
    onWarning: updateEditRecoveryHealth});
  let records;
  try { records = await editRecovery.list(); editRecoveryReady = true; }
  catch (error) {
    updateEditRecoveryHealth(error);
    toast(tr("Local edit recovery is unavailable: {errorMessage}. Keep this window open if saving fails.", {errorMessage: error.message}));
    return;
  }
  const outstanding = [];
  for (const record of records) {
    // An acknowledged save whose cleanup was interrupted needs no replay.
    const saved = await getJSON(`/api/state?name=${encodeURIComponent(record.name)}&recovery=1`).catch(() => null);
    const legacyIdentity = Boolean(record.payload.sourceKey && saved?._recoverySourceKey
      && record.payload.sourceKey !== saved._recoverySourceKey
      && record.payload.sourceKey === saved._recoveryLegacySourceKey);
    if (!saved || saved.error || !saved._recoverySourceKey
      || (record.payload.sourceKey && record.payload.sourceKey !== saved._recoverySourceKey && !legacyIdentity)) {
      toast(tr('Recovery kept for {recordName}: its original is unavailable or has changed.', {recordName: record.name}));
      continue;
    }
    if (saved && !saved.error && recoveryAcknowledged(record.payload, saved)) {
      await editRecovery.remove(record.name, record.token).catch(error =>
        toast(tr('Saved edits are safe; recovery cleanup needs attention: {errorMessage}', {errorMessage: error.message})));
    } else outstanding.push({...record, legacyIdentity,
      // A confirmed legacy recovery is guarded against the complete identity
      // seen before the dialog. Changes while the dialog is open still fail.
      payload: {...record.payload, sourceKey: saved._recoverySourceKey}});
  }
  if (!outstanding.length) return;
  if (await chooseEditRecovery(outstanding)) {
    for (const record of outstanding) {
      editSaveQueue.enqueue(record.name, {...record.payload,
        expectedRecoverySourceKey: record.payload.sourceKey,
        history: record.payload.history ? {...record.payload.history, label: tr("Recovered edit")} : null},
      {immediate: true});
    }
    await flushEditSaves();
    for (const record of outstanding) {
      if (editSaveQueue.getPending(record.name)) {
        deferredEditRecovery.set(record.name, record.payload.sourceKey);
        continue;
      }
      deferredEditRecovery.delete(record.name);
      const image = S.images.find(item => item.name === record.name);
      if (image) Object.assign(image, normalizeLibraryImage({
        ...image, ...record.payload.state, stateLoaded: true, hasEdits: true,
        recoverySourceKey: record.payload.sourceKey,
      }, true));
    }
  } else {
    for (const record of outstanding) await editRecovery.remove(record.name, record.token)
      .catch(error => toast(tr("Saved edits kept; recovery cleanup needs attention: {errorMessage}", {errorMessage: error.message})));
  }
}

/* ------------------------------------------------------------------ boot */
fetch('/api/images').then((r) => r.json()).then(async (d) => {
  S.serverPlatform = d.platform || null;
  installDesktopTheme({ platform: d.platform });
  if (!window.__LIGHTTABLE_PLATFORM__ && d.platform) {
    window.__LIGHTTABLE_PLATFORM__ = { darwin: 'macos', win32: 'windows' }[d.platform] || d.platform;
  }
  if (d.platform && d.platform !== 'darwin') {
    for (const id of ['exFormat', 'modalExFormat']) {
      const select = $(id);
      const heif = select.querySelector('option[value="heif"]');
      if (heif) { heif.disabled = true; heif.hidden = true; }
      if (select.value === 'heif') select.value = 'jpeg';
    }
    for (const id of ['maskAddPeople', 'maskSoftenSkin']) $(id).hidden = true;
  }
  FIRST_RUN?.setLibrary(d);
  S.rootFolder = d.folder;
  S.catalogEnabled = !!d.catalog?.enabled;
  S.catalogTotal = Number.isFinite(+d.total) ? +d.total : 0;
  S.primarySourceId = catalogSourceId(d);
  S.folders = Array.isArray(d.folders) ? d.folders : [];
  S.folderIds = d.folderIds && typeof d.folderIds === 'object' ? d.folderIds : {};
  if (typeof S.activeFolders[S.rootFolder] === 'string') {
    S.activeFolder = S.activeFolders[S.rootFolder];
  }
  S.filmDefaults = d.defaults;
  S.newPhotoGradeDefaults = d.gradeDefaults || GRADE_DEFAULTS;
  S.grainBaselines = d.stocks.grainBaselines || {};
  S.profiles = Array.isArray(d.profiles) ? d.profiles : [];
  S.profileById = Object.fromEntries(S.profiles.map((profile) =>
    [profile.id, profile]));
  S.rustAvailable = !!d.rust;
  S.engineCapabilityKnown = true;
  S.provenance = d.provenance || null;
  if (S.provenance) {
    $('renderProvenance').textContent = [
      `Render ${String(S.provenance.rendererIdentity || '').slice(0, 12)}`,
      `Profiles ${String(S.provenance.profileCatalogSha256 || '').slice(0, 12)}`,
      S.provenance.rustCoreRevision || 'Rust revision unavailable',
    ].join('\n');
  }
  S.params = { ...d.defaults };
  $('output_recipe').replaceChildren(...Object.entries(d.outputRecipes || {}).map(([id, recipe]) => {
    const option = document.createElement('option');
    option.value = id; option.textContent = recipe.name;
    option.title = ((recipe.description || ''));
    return option;
  }));
  $('stock').replaceChildren();
  for (const { label, options } of filmStockGroups(S.profiles)) {
    const group = document.createElement('optgroup');
    group.label = label;
    options.forEach((choice) => {
      const option = profileOption({ ...choice, name: choice.label });
      option.title = choice.label;
      group.appendChild(option);
    });
    if (group.children.length) $('stock').appendChild(group);
  }
  populatePaperOptions();
  syncEngineForProfile();
  S.library = d.library || { collections: [], stacks: [], virtualCopies: [] };
  S.libraryLoaded = true;
  applyPendingActiveCollection();
  S.images = d.images.map((im) => normalizeLibraryImage(
    im, !S.catalogEnabled));
  PHOTO_DISPLAY_STATUS.retain(S.images);
  await initializeEditRecovery(d);
  syncAI(d.aiIndex || S.ai);
  if (S.ai.enabled && !S.ai.scanComplete) scheduleAIStatusPoll(true);
  // A finished index still has to be read once on load. In catalog mode the
  // library payload carries no generated metadata, so without this the
  // search terms and culling verdicts stay empty until the next rescan.
  else if (S.ai.enabled) refreshAIResults().catch(() => {});
  if (!S.folders.some((item) => item.path === S.activeFolder)) S.activeFolder = '';
  renderFolders();
  const initialScope = visible();
  const firstImage = initialScope.find((im) => im.status === 'pending')
    || initialScope[0];
  const first = firstImage ? S.images.indexOf(firstImage) : -1;
  const benchmarkImage = window.__LIGHTTABLE_NATIVE_BENCHMARK_IMAGE__;
  const benchmarkIndex = benchmarkImage
    ? S.images.findIndex((im) => im.name === benchmarkImage) : -1;
  if (benchmarkIndex >= 0 || first >= 0) {
    await go(benchmarkIndex >= 0 ? benchmarkIndex : first);
  } else {
    refreshLists();
  }
  if (S.activePane === 'cropPane') setCropMode(true);
  if (S.catalogEnabled && S.images.length < S.catalogTotal) {
    loadRemainingCatalogRows(S.catalogTotal).catch(() => {});
  }
  watchCatalogScan();
  postNative('requestSources', {}, true);
  const benchmarkWidth = +window.__LIGHTTABLE_NATIVE_BENCHMARK_WIDTH__;
  if (Number.isFinite(benchmarkWidth) &&
      [...$('pw').options].some((option) => +option.value === benchmarkWidth)) {
    $('pw').value = String(benchmarkWidth);
    $('engine').value = 'rs';
    $('filmProfileToggle').setAttribute('aria-checked', 'true');
    S.params.profile_enabled = true;
    const iterations = +window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__;
    const presentation = nativePreviewActive() ? 'native-metal' : 'webgl';
    const coldSettled = Number.isInteger(iterations) && iterations > 0
      ? waitForBenchmarkSettled(benchmarkWidth, presentation) : null;
    renderFilm(0);
    if (coldSettled) {
      coldSettled.then(() => runNativeBenchmark(benchmarkWidth, iterations))
        .catch((error) => postNative('nativeBenchmarkComplete', {
          schema: 1, requestedWidth: benchmarkWidth, iterations,
          error: String(error?.message || error), renders: PERF.snapshot(),
        }));
    }
  }
});

/* ---------------------------------------------------------------- events */
FILM_SLIDERS.forEach((id) => {
  const input = $(id);
  if (!input) return;
  let gesturePhoto = null;
  input.addEventListener('input', () => {
    if (typeof markControlDirty === 'function') markControlDirty();
    if (gesturePhoto !== S.editingName) { pushUndo(); gesturePhoto = S.editingName; }
    // An explicit gesture owns this value, even if it matches the rounded
    // thumb position from the last sync. Other controls retain their precision.
    S.params[id] = +input.value;
    syncFilmReadout(id, +input.value);
    renderPhysicalPreview();
  });
  input.addEventListener('change', () => {
    if (gesturePhoto !== S.editingName && readNumericControl(input, S.params[id]) !== S.params[id]) pushUndo();
    gesturePhoto = null;
    saveState();
  });
  const resetFilmSlider = () => {
    pushUndo();
    gesturePhoto = null;
    const def = S.filmDefaults?.[id] ?? 0;
    S.params[id] = def;
    syncNumericControl(input, def);
    syncFilmReadout(id, def);
    saveState();
    renderFilm(0);
  };
  input.addEventListener('dblclick', resetFilmSlider);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetFilmSlider);
});
FILM_TOGGLES.forEach((id) => {
  $(id).addEventListener('change', () => {
    pushUndo(); readControls(); syncControls(); saveState(); renderFilm(0);
  });
});
$('filmProfileToggle').addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  readControls();
  setDevelopMode(!S.params.profile_enabled);
});
$('stock').onchange = () => {
  pushUndo();
  $('filmProfileToggle').setAttribute('aria-checked', 'true');
  readControls();
  S.params = filmParamsForStock(S.params, $('stock').value, S.profiles);
  syncControls(); saveState(); renderFilm(0);
};
const filmBrowser = createFilmBrowser({
  context: () => cur() ? {
    name: cur().name, label: displayName(cur()), engine: $('engine').value,
    profiles: S.profiles, stocks: [...$('stock').options].map((o) => ({ id: o.value, label: o.textContent })),
    state: cloneValue({ params: S.params, grade: S.grade, crop: S.crop, optics: S.optics, heals: S.heals, masks: S.masks }),
  } : null,
  apply: (stock, name) => {
    if (cur()?.name !== name) { toast(tr("Photo changed. Open film previews again.")); return; }
    $('stock').value = stock;
    $('filmProfileToggle').setAttribute('aria-checked', 'true');
    $('stock').dispatchEvent(new Event('change', { bubbles: true }));
  },
});
$('browseFilmStocks').onclick = () => filmBrowser.open();
$('paper').onchange = () => {
  pushUndo(); readControls();
  S.params.paper_locked = true;
  S.params.print_development_time =
    selectedPaperProfile()?.defaultDevelopmentTime || 0;
  syncControls(); saveState(); renderFilm(0);
};
FILM_SELECTS.forEach((id) => {
  $(id).addEventListener('change', () => {
    pushUndo(); readControls();
    if (id === 'workflow_mode' && S.params.workflow_mode === 'authentic' &&
        !S.params.paper_locked && selectedFilmProfile()?.targetPrint) {
      S.params.paper = selectedFilmProfile().targetPrint;
    }
    if (id === 'wb_mode') {
      const presets = {
        daylight: [5500, 0.1],
        cloudy: [6500, 0.1],
        shade: [7500, 0.1],
        tungsten: [2850, 0],
        fluorescent: [3800, 0.1],
        flash: [5500, 0],
      };
      if (presets[S.params.wb_mode]) {
        [S.params.wb_temperature, S.params.wb_tint] = presets[S.params.wb_mode];
      }
    }
    syncControls(); saveState(); renderFilm(0);
  });
});
$('rawSaveCameraDefault').onclick = async () => {
  if (!cur() || !isRawInput()) return;
  readControls();
  const result = await api('/api/raw-default', {
    action: 'save', name: cur().name,
    settings: Object.fromEntries([
      'raw_profile', 'raw_highlight_recovery', 'raw_sensor_denoise',
      'learned_denoise', 'learned_denoise_strength',
      'developProfile', 'wb_mode', 'wb_temperature', 'wb_tint',
    ].map((key) => [key, S.params[key]])),
  });
  if (result.error) return toast(result.error);
  S.rawDefault = result;
  $('rawCameraDefaultStatus').textContent = tr("{resultLabel} default is saved.", {resultLabel: result.label});
  syncControls();
  toast(tr("Camera default saved"));
};
$('rawResetCameraDefault').onclick = async () => {
  if (!cur() || !isRawInput()) return;
  const result = await api('/api/raw-default', {
    action: 'delete', name: cur().name,
  });
  if (result.error) return toast(result.error);
  S.rawDefault = result;
  $('rawCameraDefaultStatus').textContent = tr("{resultLabel} uses the app defaults.", {resultLabel: result.label});
  syncControls();
  toast(tr("Camera default removed"));
};
$('development_time').onchange = () => {
  pushUndo(); readControls(); saveState(); renderFilm(0);
};
$('print_development_time').onchange = () => {
  pushUndo(); readControls(); saveState(); renderFilm(0);
};
$('engine').onchange = () => renderFilm(0);
$('pw').onchange = () => renderFilm(0);

document.querySelectorAll('[data-g]').forEach((el) => {
  const k = el.dataset.g;
  el.addEventListener('pointerdown', pushUndo);
  el.addEventListener('input', () => {
    if (typeof markControlDirty === 'function') markControlDirty();
    S.grade[k] = +el.value;
    const out = document.querySelector(`[data-gv="${k}"]`);
    if (out) out.textContent = fmtG(el.value);
    drawGrade();
  });
  el.addEventListener('change', () => saveState());
  const resetGradeSlider = () => {
    pushUndo();
    const def = GRADE_DEFAULTS[k] ?? 0;
    S.grade[k] = def;
    el.value = String(def);
    const out = document.querySelector(`[data-gv="${k}"]`);
    if (out) out.textContent = fmtG(def);
    drawGrade();
    saveState();
  };
  el.addEventListener('dblclick', resetGradeSlider);
  const row = el.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetGradeSlider);
});

document.querySelectorAll('a.reset').forEach((a) => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (['curve', 'hsl', 'pointColor', 'colorGrading'].includes(a.dataset.reset)) return;
    pushUndo();
    for (const k of RESET_GROUPS[a.dataset.reset] || []) {
      if (k in GRADE_DEFAULTS) S.grade[k] = GRADE_DEFAULTS[k];
      else S.params[k] = S.filmDefaults[k];
    }
    if (a.dataset.reset === 'colour') {
      delete S.grade.hsl;
      delete S.grade.pointColor;
      delete S.grade.colorGrading;
      syncHsl();
    }
    syncControls(); syncGrade(); drawGrade(); saveState();
    if (['raw', 'film', 'stages'].includes(a.dataset.reset)) renderFilm(0);
  });
});

$('stars').addEventListener('click', (e) => {
  const s = e.target.closest('.s');
  if (s) setRating(+s.dataset.r);
});
$('approveBtn').onclick = () => setStatus('approved');
$('skipBtn').onclick = () => setStatus('skipped');
document.querySelectorAll('[data-cull-status]').forEach((button) => {
  button.onclick = () => setStatus(button.dataset.cullStatus);
});
document.querySelectorAll('[data-cull-rating]').forEach((button) => {
  button.onclick = () => setRating(+button.dataset.cullRating);
});
document.querySelectorAll('[data-cull-auto]').forEach((button) => {
  button.onclick = () => {
    const control = $('autoAdvance');
    if (!control) return;
    control.checked = !control.checked;
    control.dispatchEvent(new Event('change', { bubbles: true }));
    APP_PREFS.autoAdvance = control.checked;
    syncCullBars();
  };
});
document.querySelectorAll('[data-view]').forEach((button) => {
  button.onclick = () => setViewMode(button.dataset.view);
});
$('filter').onchange = refreshFilteredView;
$('editFilter').onchange = () => { refreshFilteredView(); savePrefs(); };
$('ratingFilter').onchange = () => { refreshFilteredView(); savePrefs(); };
$('kindFilter').onchange = () => { refreshFilteredView(); savePrefs(); };
$('sort').onchange = refreshFilteredView;
document.querySelectorAll('[data-source]').forEach((b) => {
  b.onclick = () => {
    S.activeCollection = '';
    $('filter').value = b.dataset.source;
    $('filter').dispatchEvent(new Event('change'));
  };
});
$('addPhotosBtn').onclick = () => postNative('addPhotos');
$('importPhotosBtn').onclick = () => {
  if (!postNative('importApplePhotos')) {
    toast(tr("Apple Photos import is available in the macOS app"));
  }
};
$('addFolderBtn').onclick = () => postNative('addFolder');
function setFolderMode(mode, persist = true) {
  S.folderMode = mode;
  $('browseFoldersTab').classList.toggle('on', mode === 'browse');
  $('favoriteFoldersTab').classList.toggle('on', mode === 'favorites');
  $('browseFoldersTab').setAttribute('aria-selected', String(mode === 'browse'));
  $('favoriteFoldersTab').setAttribute('aria-selected', String(mode === 'favorites'));
  renderFolders();
  if (persist) savePrefs();
}
$('browseFoldersTab').onclick = () => setFolderMode('browse');
$('favoriteFoldersTab').onclick = () => setFolderMode('favorites');
$('includeSubfolders').onchange = () => {
  S.includeSubfolders = $('includeSubfolders').checked;
  refreshFilteredView();
  renderFolders();
  savePrefs();
};
const refreshSearch = debounce(() => {
  refreshFilteredView();
}, 120);
$('search').addEventListener('input', refreshSearch);
$('search').addEventListener('search', refreshSearch);
$('search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if ($('search').value) {
      $('search').value = '';
      refreshSearch();
    }
    $('search').blur();
    e.preventDefault();
  } else if (e.key === 'Enter') {
    const first = visible()[0];
    if (first) go(S.images.indexOf(first));
    $('search').blur();
    e.preventDefault();
  }
});
$('gridSize').addEventListener('input', () => {
  document.documentElement.style.setProperty('--cell', `${$('gridSize').value}px`);
  layoutPhotoGrid();
});
$('gridSize').addEventListener('change', savePrefs);
$('gridSize').addEventListener('dblclick', () => {
  $('gridSize').value = '180';
  document.documentElement.style.setProperty('--cell', '180px');
  layoutPhotoGrid();
  savePrefs();
});
document.querySelectorAll('.tool-btn').forEach((b) => {
  b.onclick = () => PHOTO_TOOL_PANES.includes(b.dataset.pane)
    ? selectPhotoTool(b.dataset.pane) : switchPane(b.dataset.pane);
});
$('aiToggle').onclick = () => runAIAction(S.ai.enabled ? 'disable' : 'enable');
$('cullEnableIndex').onclick = () => runAIAction('enable');
$('aiRebuild').onclick = () => runAIAction('rebuild');
$('aiClear').onclick = () => {
  if (window.confirm(tr("Delete the generated local photo index? Your originals and edits will not be changed."))) {
    runAIAction('clear');
  }
};
$('leftPanelToggle').onclick = () => {
  if (window.innerWidth <= 800) {
    $('appShell').classList.toggle('left-expanded');
    $('leftPanelToggle').classList.toggle('on', $('appShell').classList.contains('left-expanded'));
  } else {
    $('appShell').classList.toggle('left-collapsed');
    $('leftPanelToggle').classList.toggle('on', !$('appShell').classList.contains('left-collapsed'));
  }
  savePrefs();
};
$('filmstripToggle').onclick = () => {
  document.querySelector('.workspace').classList.toggle('filmstrip-hidden');
  $('filmstripToggle').classList.toggle('on', !document.querySelector('.workspace').classList.contains('filmstrip-hidden'));
  savePrefs();
};

const FILMSTRIP_DEFAULT_HEIGHT = 116;
const FILMSTRIP_MIN_HEIGHT = 86;
const FILMSTRIP_MAX_HEIGHT = 260;
function currentFilmstripHeight() {
  return parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--filmstrip-h')) || FILMSTRIP_DEFAULT_HEIGHT;
}
function applyFilmstripHeight(value, persist = false) {
  const workspaceHeight = document.querySelector('.workspace')?.clientHeight || 0;
  const responsiveMax = workspaceHeight
    ? Math.max(FILMSTRIP_MIN_HEIGHT,
      Math.min(FILMSTRIP_MAX_HEIGHT, Math.round(workspaceHeight * 0.55)))
    : FILMSTRIP_MAX_HEIGHT;
  const height = Math.round(clamp(+value || FILMSTRIP_DEFAULT_HEIGHT,
    FILMSTRIP_MIN_HEIGHT, responsiveMax));
  document.documentElement.style.setProperty('--filmstrip-h', `${height}px`);
  $('filmstripResize').setAttribute('aria-valuenow', String(height));
  $('filmstripResize').setAttribute('aria-valuemax', String(responsiveMax));
  _stripKey = '';
  renderStrip();
  if (persist) savePrefs();
}

let filmstripResizeGesture = null;
$('filmstripResize').addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  filmstripResizeGesture = {
    pointerId: event.pointerId,
    startY: event.clientY,
    startHeight: currentFilmstripHeight(),
  };
  document.body.classList.add('filmstrip-resizing');
  $('filmstripResize').setPointerCapture(event.pointerId);
});
$('filmstripResize').addEventListener('pointermove', (event) => {
  if (!filmstripResizeGesture || filmstripResizeGesture.pointerId !== event.pointerId) return;
  applyFilmstripHeight(filmstripResizeGesture.startHeight +
    filmstripResizeGesture.startY - event.clientY);
});
function finishFilmstripResize(event) {
  if (!filmstripResizeGesture || filmstripResizeGesture.pointerId !== event.pointerId) return;
  filmstripResizeGesture = null;
  document.body.classList.remove('filmstrip-resizing');
  if ($('filmstripResize').hasPointerCapture(event.pointerId)) {
    $('filmstripResize').releasePointerCapture(event.pointerId);
  }
  applyFilmstripHeight(currentFilmstripHeight(), true);
}
$('filmstripResize').addEventListener('pointerup', finishFilmstripResize);
$('filmstripResize').addEventListener('pointercancel', finishFilmstripResize);
$('filmstripResize').addEventListener('dblclick', () => {
  applyFilmstripHeight(FILMSTRIP_DEFAULT_HEIGHT, true);
});
$('filmstripResize').addEventListener('keydown', (event) => {
  if (!['ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? FILMSTRIP_DEFAULT_HEIGHT
    : currentFilmstripHeight() + (event.key === 'ArrowUp' ? 1 : -1) *
      (event.shiftKey ? 20 : 8);
  applyFilmstripHeight(next, true);
});
$('resetEdit').onclick = () => {
  if (!cur()) return;
  pushUndo();
  const rawDefaults = isRawInput() ? { sharpness: 0.25, colorNoise: 0.25 } : {};
  S.grade = { ...GRADE_DEFAULTS, ...rawDefaults };
  syncGrade(); syncCurveFromGrade(); syncHsl();
  drawGrade(); saveState(true);
  toast(tr("Edit adjustments reset"));
};

$('resetFilm').onclick = () => {
  if (!cur()) return;
  pushUndo();
  for (const group of ['film', 'stages']) {
    for (const key of RESET_GROUPS[group]) S.params[key] = S.filmDefaults[key];
  }
  syncControls(); saveState(true); renderFilm(0);
  toast(tr("Film settings reset"));
};

$('zoomIn').onclick = () => { S.zoomMode = 'custom'; zoomCentre(1.25); };
$('zoomOut').onclick = () => { S.zoomMode = 'custom'; zoomCentre(1 / 1.25); };
$('zoomFit').onclick = () => zoomReset({animate: true});
document.querySelectorAll('[data-exit-tool]').forEach((button) => { button.onclick = exitPhotoTool; });
$('cropDone').onclick = exitPhotoTool;
$('cropCancel').onclick = cancelCropSession;
$('cropReset').onclick = () => {
  if ($('cropReset').disabled) return;
  switchPane('cropPane');
  pushUndo();
  const hadRotate = !!(S.params?.rotate % 360);
  S.crop = null;
  S.cropRatio = 'free'; S.cropLocked = false; S.cropAspectFlipped = false;
  S.params.rotate = 0;
  for (const key of ['rotate', 'vertical', 'horizontal', 'scale', 'flipHorizontal', 'flipVertical']) {
    S.optics[key] = OPTICS_DEFAULTS[key];
  }
  rememberCropChoices();
  syncOpticsPanel();
  setCropMode(true);
  syncBrowserOriginal(requestedPreviewWidth());
  applyCropVisual(); zoomReset(); saveState();
  if (hadRotate) renderFilm(0); else refreshBaseEdits();
  toast(tr("Crop & geometry reset"));
  syncControls();
};

function rotate(delta) {
  pushUndo();
  S.params.rotate = (((S.params.rotate || 0) + delta) % 360 + 360) % 360;
  const ratio = cropLayerRatio();
  if (S.crop && ratio) S.crop = cropForRatio(S.crop, ratio);
  syncBrowserOriginal(requestedPreviewWidth());
  applyCropVisual(); zoomReset(); saveState(); renderFilm(0);
  setCropMode(S.cropping);
}
$('rotL').onclick = () => rotate(-90);
$('rotR').onclick = () => rotate(90);

function renderedComparePosition() {
  // The original texture bypasses Film, geometry, masks, grade, and proofing.
  if (S.holdBefore) return 1;
  return S.compareActive ? S.comparePosition : 0;
}

function compareEditingBlocked() {
  return !cur() || S.cropping || S.wbPick || S.pointColorPick || S.maskColorPick ||
    ['maskPane', 'healPane'].includes(S.activePane);
}

function syncCompareControl() {
  const button = $('compareBtn');
  const blocked = !cur() || S.wbPick || S.pointColorPick || S.maskColorPick;
  button.disabled = blocked;
  button.classList.toggle('on', S.compareActive);
  button.setAttribute('aria-pressed', String(S.compareActive));
  button.title = blocked ? tr("Select a photo or finish sampling to compare") : tr("Toggle split before and after view (\\)");
}

function syncCompareView() {
  $('compareSnap').hidden = !S.compareActive || S.zoomMode === 'fit' || S.zoom <= 1;
  const overlay = $('compareOverlay');
  const geometry = S.compareActive ? compareViewGeometry(
    $('cmp').getBoundingClientRect(), $('zoomwrap').getBoundingClientRect(), S.comparePosition) : null;
  overlay.hidden = !geometry;
  if (!geometry) return;
  // This sibling of the transformed photo keeps the line, handle and labels
  // at their normal screen size, with the handle centred in the visible area.
  for (const property of ['left', 'top', 'width', 'height']) {
    overlay.style[property] = `${geometry[property]}px`;
  }
  overlay.style.setProperty('--pos', `${geometry.dividerX}px`);
  $('tagL').hidden = geometry.dividerX <= 0;
  $('tagR').hidden = geometry.dividerX >= geometry.width;
}

function snapCompareToView() {
  if (!S.compareActive || compareEditingBlocked()) return;
  const position = comparePositionAtViewCenter(
    $('cmp').getBoundingClientRect(), $('zoomwrap').getBoundingClientRect());
  if (position !== null) queueComparePosition(position);
}

function renderCompare() {
  const position = renderedComparePosition();
  const sourcePosition = previewSourceX(position);
  if (nativePreviewActive()) {
    postNative('nativeCompare', { position: sourcePosition });
  } else if (S.gl?.drawCompare(sourcePosition)) {
    $('orig').removeAttribute('src');
  } else {
    $('cmp').style.setProperty('--clip', (100 - sourcePosition * 100) + '%');
  }
  $('cmp').classList.toggle('comparing', S.compareActive);
  $('zoomwrap').classList.toggle('comparing', S.compareActive);
  syncCompareView();
  syncCompareControl();
}

function setCompareActive(on, { restoreTool = true } = {}) {
  const next = Boolean(on) && !!cur() && !S.wbPick && !S.pointColorPick && !S.maskColorPick;
  const entering = next && !S.compareActive;
  const returnPane = compareReturnPane;
  if (entering && PHOTO_TOOL_PANES.includes(S.activePane)) {
    compareReturnPane = S.activePane;
    switchPane(lastAdjustmentPane, { fromCompare: true });
  }
  S.compareActive = next;
  if (next) syncBrowserOriginal();
  if (entering) snapCompareToView();
  if (!next) compareReturnPane = null;
  renderCompare();
  const back = $('compareReturn');
  back.hidden = !next || !compareReturnPane;
  back.textContent = tr("Return to {value}", {value: ({ cropPane: 'Crop', maskPane: 'Mask', healPane: 'Remove' }[compareReturnPane] || tr("tool"))});
  if (!next && restoreTool && returnPane) switchPane(returnPane, { fromCompare: true });
  scheduleNativeMenuState();
}
$('compareReturn').onclick = () => setCompareActive(false);

let compareFrame = null;
let pendingComparePosition = 0.5;
function queueComparePosition(value) {
  pendingComparePosition = clampComparePosition(value);
  if (compareFrame !== null) return;
  compareFrame = requestAnimationFrame(() => {
    compareFrame = null;
    if (!S.compareActive || compareEditingBlocked()) return;
    S.comparePosition = pendingComparePosition;
    renderCompare();
  });
}

$('compareBtn').addEventListener('click', () => setCompareActive(!S.compareActive));
$('compareSnap').addEventListener('click', snapCompareToView);

(function compareDrag() {
  const cmp = $('cmp');
  const bar = $('compareBar');
  let dragging = false;
  let dragRect = null;
  const update = (event) => {
    if (!dragRect?.width) return;
    queueComparePosition((event.clientX - dragRect.left) / dragRect.width);
  };
  bar.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || !S.compareActive || compareEditingBlocked()) return;
    dragging = true;
    dragRect = cmp.getBoundingClientRect();
    bar.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  });
  bar.addEventListener('pointermove', (event) => {
    if (dragging) update(event);
  });
  const finish = (event) => {
    if (!dragging) return;
    dragging = false;
    dragRect = null;
    if (bar.hasPointerCapture(event.pointerId)) bar.releasePointerCapture(event.pointerId);
  };
  bar.addEventListener('pointerup', finish);
  bar.addEventListener('pointercancel', finish);
})();

function setBefore(on) {
  if (on && S.compareActive) setCompareActive(false);
  S.holdBefore = on;
  if (on) syncBrowserOriginal();
  $('beforeBtn').classList.toggle('on', on);
  renderCompare();
  drawGrade();
}
$('beforeBtn').addEventListener('pointerdown', () => setBefore(true));
window.addEventListener('pointerup', () => { if (S.holdBefore) setBefore(false); });

function confirmTransfer(id) {
  const button = $(id);
  button.classList.add('confirmed');
  clearTimeout(button._confirmTimer);
  button._confirmTimer = setTimeout(() => button.classList.remove('confirmed'), 900);
}
function transferTargets() {
  if (SELECTION_REQUEST?.pending) return [];
  // Same rule as marking: a selection that a filter change has hidden must not
  // silently become the target of a paste, an export, or a batch action.
  const selected = selectedVisibleTargets();
  return selected.length ? selected : (cur() ? [cur()] : []);
}
function photoReadyForEditing() {
  const photo = cur();
  return !!photo && photo.kind !== 'video' && S.editingName === photo.name;
}
function syncPhotoActions() {
  const ready = photoReadyForEditing();
  for (const id of ['editPane', 'filmPane', 'cropPane', 'maskPane', 'healPane']) {
    $(id).inert = !ready;
  }
  for (const id of ['resetEdit', 'autoBtn', 'zoomFit', 'zoom1', 'beforeBtn',
    'wbBtn', 'clipBtn', 'versionCreate']) {
    $(id).disabled = !ready;
  }
  $('maskReset').disabled = !ready || !S.masks.length;
  $('healReset').disabled = !ready || !S.heals.length;
  ENHANCE?.sync();
}
function updateTransferActions() {
  syncPhotoActions();
  const targets = transferTargets();
  const primaryKey = ['windows', 'linux'].includes(window.__LIGHTTABLE_PLATFORM__) ? 'Ctrl' : '⌘';
  $('copyBtn').disabled = !cur();
  $('pasteBtn').hidden = !S.clipboard;
  $('pasteBtn').disabled = !S.clipboard || !targets.length;
  $('pasteAllBtn').disabled = !S.clipboard || !visible().length;
  $('virtualCopyBtn').disabled = !cur();
  $('deleteVirtualBtn').disabled = !cur()?.virtual;
  $('stackBtn').disabled = targets.length < 2;
  $('unstackBtn').disabled = !targets.some((image) => stackForImage(image.name));
  $('matchExposureBtn').disabled = targets.length < 2;
  $('pregenPreviewsBtn').disabled = !targets.length;
  $('batchAiMaskBtn').disabled = !targets.length || Boolean(MASK_BATCH?.active);
  const mergeTargets = [...new Set(targets.map((image) => image.sourceName || image.name))];
  $('mergeRun').disabled = mergeTargets.length < 2;
  $('mergeSelectionSummary').textContent = mergeTargets.length >= 2 ? $('mergeMode').value === 'focus' ? tr("{mergeTargetsLength} distinct originals selected. Use a tripod or focus rail sequence.", {mergeTargetsLength: mergeTargets.length}) : tr("{mergeTargetsLength} distinct originals selected.", {mergeTargetsLength: mergeTargets.length}) : tr("Select two or more photos in the library.");
  const collection = activeCollection();
  $('addToCollection').disabled = !collection || collection.type !== 'regular' ||
    !targets.length;
  $('copyBtn').title = cur() ? tr("Choose edit settings to copy ({primaryKey}+Shift+C)", {primaryKey: primaryKey}) : tr("Select a photo to copy its settings");
  $('pasteBtn').title = !S.clipboard ? tr("Copy settings first") : targets.length > 1 ? tr("Paste edit settings to {targetsLength} selected photos ({primaryKey}+Shift+V)", {targetsLength: targets.length, primaryKey: primaryKey}) : tr("Paste edit settings ({primaryKey}+Shift+V)", {primaryKey: primaryKey});
  if ($('previousBtn')) {
    const hasPrior = Boolean(S.priorPhotoSettings && cur() && S.priorPhotoSettings.sourceName !== cur()?.name);
    const altKey = ['windows', 'linux'].includes(window.__LIGHTTABLE_PLATFORM__) ? 'Ctrl+Alt' : '⌘⌥';
    $('previousBtn').disabled = !hasPrior;
    $('previousBtn').title = hasPrior
      ? tr("Apply settings from previous photo ({key}+V)", {key: altKey})
      : tr("Apply settings from previous photo");
  }
  scheduleNativeMenuState();
}
let transferReturnFocus = null, transferSource = null, transferRunning = false, transferCancelled = false;
function closeTransferDialog() {
  if (transferRunning) { transferCancelled = true; $('transferStatus').textContent = tr("Stopping after the current photo…"); return; }
  $('transferDialog').classList.remove('on'); $('transferDialog').setAttribute('aria-hidden', 'true');
  transferReturnFocus?.focus?.({ preventScroll: true });
}
function showTransferDialog(title) {
  transferReturnFocus = document.activeElement;
  $('transferTitle').textContent = title;
  $('transferDialog').classList.add('on'); $('transferDialog').setAttribute('aria-hidden', 'false');
}
$('transferCancel').onclick = closeTransferDialog;
$('transferDialog').addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeTransferDialog(); }
  if (event.key === 'Tab') {
    const focusable = [...$('transferDialog').querySelectorAll('button:not(:disabled), input:not(:disabled)')]
      .filter(element => !element.hidden && !element.closest('[hidden]'));
    const index = focusable.indexOf(document.activeElement);
    if (focusable.length && ((event.shiftKey && index <= 0) || (!event.shiftKey && index === focusable.length - 1))) {
      event.preventDefault(); focusable[event.shiftKey ? focusable.length - 1 : 0].focus();
    }
  }
});
$('copyBtn').onclick = () => {
  if (!cur() || S.editingName !== cur().name || transferRunning) return;
  readControls();
  transferSource = { ...JSON.parse(snapshot()), sourceName: cur().name };
  const selected = transferChoices(APP_PREFS.copySettings);
  const host = $('transferGroups'); host.replaceChildren(); host.hidden = false;
  for (const [id, title, description] of TRANSFER_GROUPS) {
    const label = document.createElement('label'); label.className = 'transfer-choice';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.dataset.transferGroup = id; checkbox.checked = selected[id];
    const text = document.createElement('span'), strong = document.createElement('strong'), small = document.createElement('small');
    strong.textContent = title; small.textContent = description; text.append(strong, small); label.append(checkbox, text); host.append(label);
    checkbox.onchange = () => { $('transferCopy').disabled = !host.querySelector('input:checked'); };
  }
  $('transferCopy').hidden = false; $('transferCopy').disabled = !Object.values(selected).some(Boolean);
  $('transferCancel').textContent = tr("Cancel");
  $('transferStatus').textContent = tr("Only selected settings replace the destination. AI masks are detected again; object selections and painted AI refinements require manual review.");
  showTransferDialog(tr("Copy edit settings"));
  requestAnimationFrame(() => host.querySelector('input')?.focus());
};
$('transferCopy').onclick = async () => {
  const choices = Object.fromEntries([...$('transferGroups').querySelectorAll('input')].map(input => [input.dataset.transferGroup, input.checked]));
  S.clipboard = { ...cloneValue(transferSource), choices };
  APP_PREFS.copySettings = choices;
  const saved = await api('/api/prefs', { copySettings: choices }).catch(() => null);
  updateTransferActions(); closeTransferDialog(); confirmTransfer('copyBtn');
  toast(saved?.error || !saved ? tr("Settings copied; choices could not be remembered") : tr("Settings copied"));
};
async function pasteSettingsTo(targets) {
  if (!S.clipboard || transferRunning) return toast(tr("Copy settings first"));
  const items = [...new Set(targets)].filter(Boolean);
  if (!items.length) return;
  // Capture pending controls before opening the blocking transfer dialog.
  saveState();
  const clipboard = cloneValue(S.clipboard), failures = [];
  transferRunning = true; transferCancelled = false;
  $('transferGroups').hidden = true; $('transferCopy').hidden = true; $('transferCancel').textContent = tr("Stop");
  showTransferDialog(tr("Pasting edit settings"));
  $('transferCancel').focus();
  let completed = 0;
  for (const [index, target] of items.entries()) {
    let image = target;
    if (transferCancelled) break;
    $('transferStatus').textContent = tr("Photo {value} of {itemsLength} · {value2}", {value: index + 1, itemsLength: items.length, value2: displayName(image)});
    try {
      // A lean catalog row is not a destination edit state. Load it before
      // merging so unchecked settings can never be replaced with defaults.
      await prefetchState(image);
      if (!isStateLoaded(image)) throw new Error(tr("Existing settings could not be loaded; this photo was left unchanged"));
      const readDestination = () => {
        image = S.images.find(item => item.name === target.name);
        if (!image) throw new Error(tr("This photo is no longer in the library; settings were not pasted"));
        if (!isStateLoaded(image)) throw new Error(tr("The latest existing settings are not loaded. Try pasting again; this photo was left unchanged"));
        const pending = editSaveQueue.getPending(image.name)?.state || {};
        const currentEdits = {...image, ...pending};
        return { ...currentEdits, params: normalizeFilmParams(currentEdits.params),
          grade: { ...GRADE_DEFAULTS, ...(currentEdits.grade || {}) }, optics: normalizeOptics(currentEdits.optics) };
      };
      const destination = readDestination();
      let patch = transferPatch(clipboard, destination, clipboard.choices);
      if (patch.masks) {
        // A generated bitmap is tied to the effective target pixels/geometry.
        // Sort object keys so an equivalent externally supplied recipe is not
        // mistaken for a change just because its JSON member order differs.
        const maskInput = (state, changes) => JSON.stringify({
          source: image.recoverySourceKey || image.fileKey || null,
          params: changes.params || state.params,
          optics: changes.optics || state.optics,
          crop: Object.hasOwn(changes, 'crop') ? changes.crop : state.crop || null,
        }, (_key, value) => value && typeof value === 'object' && !Array.isArray(value)
          ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
        const generatedFor = maskInput(destination, patch);
        const masks = await regenerateTransferMasks(patch.masks,
          kind => api('/api/mask/semantic', { name: image.name, kind, params: patch.params || destination.params }),
          { samePhoto: image.name === clipboard.sourceName &&
            (clipboard.params?.rotate || 0) === ((patch.params || destination.params).rotate || 0) });
        // Detection can take seconds. Merge selected groups again so current
        // unselected settings survive changes made during that asynchronous work.
        const latest = readDestination();
        const refreshed = transferPatch(clipboard, latest, clipboard.choices);
        if (generatedFor !== maskInput(latest, refreshed)) {
          throw new Error(tr("The photo or its geometry changed while masks were prepared. Try pasting again; this photo was left unchanged"));
        }
        patch = {...refreshed, masks};
      }
      if (transferCancelled) break;
      const { cropChoices, ...entry } = patch;
      if (image === cur() && S.editingName === image.name) pushUndo();
      enqueuePhotoPatch(image, entry, {historyLabel: tr("Paste selected settings")});
      image.cropChoices = cropChoices ?? image.cropChoices;
      image.stateLoaded = true;
      if (image !== cur()) photoUndo.clear(image.name);
      invalidateEditedThumbnail(image);
      // Apply once before waiting: an external update arriving during the save
      // must never be overwritten by completion of this older operation.
      if (image === cur() && S.editingName === image.name) {
        restore(JSON.stringify({ ...JSON.parse(snapshot()), ...patch }), null, false);
        _lastHistorySnapshot = editHistorySnapshot();
      }
      await editSaveQueue.flush(image.name);
      completed++;
    } catch (error) { failures.push(`${displayName(image)}: ${error.message || 'Could not paste'}`); }
  }
  transferRunning = false;
  refreshLists(); confirmTransfer('pasteBtn');
  $('transferCancel').textContent = tr("Done");
  $('transferStatus').textContent = (transferCancelled ? tr("Pasted to {completed} of {itemsLength} photos · stopped.{value}", {completed: completed, itemsLength: items.length, value: failures.length ? `\n${failures.join('\n')}` : ''}) : tr("Pasted to {completed} of {itemsLength} photos.{value}", {completed: completed, itemsLength: items.length, value: failures.length ? `\n${failures.join('\n')}` : ''}));
  $('transferCancel').focus();
  if (!failures.length && !transferCancelled) { closeTransferDialog(); toast(trn("Pasted to {count} photo", "Pasted to {count} photos", completed, {completed: completed})); }

}
$('pasteBtn').onclick = () => pasteSettingsTo(transferTargets());
$('pasteAllBtn').onclick = () => pasteSettingsTo(visible());
async function applyPreviousSettings() {
  if (!S.priorPhotoSettings || !cur()) return toast(tr("No previous photo settings to apply"));
  const image = cur();
  if (image.name === S.priorPhotoSettings.sourceName) return toast(tr("Already on the source photo"));
  await prefetchState(image);
  if (!isStateLoaded(image)) return toast(tr("Existing settings could not be loaded; this photo was left unchanged"));

  const pending = editSaveQueue.getPending(image.name)?.state || {};
  const currentEdits = { ...image, ...pending };
  const destination = {
    ...currentEdits,
    params: normalizeFilmParams(currentEdits.params),
    grade: { ...GRADE_DEFAULTS, ...(currentEdits.grade || {}) },
    optics: normalizeOptics(currentEdits.optics),
  };

  const choices = APP_PREFS.copySettings || transferChoices();
  const patch = transferPatch(S.priorPhotoSettings, destination, choices);
  const { cropChoices, ...entry } = patch;
  if (image === cur() && S.editingName === image.name) pushUndo();
  enqueuePhotoPatch(image, entry, { historyLabel: tr("Previous settings") });
  image.cropChoices = cropChoices ?? image.cropChoices;
  image.stateLoaded = true;
  invalidateEditedThumbnail(image);
  if (image === cur() && S.editingName === image.name) {
    restore(JSON.stringify({ ...JSON.parse(snapshot()), ...patch }), null, false);
    _lastHistorySnapshot = editHistorySnapshot();
  }
  await editSaveQueue.flush(image.name);
  refreshLists();
  if ($('previousBtn')) confirmTransfer('previousBtn');
  updateTransferActions();
  toast(tr("Applied previous settings"));
}
if ($('previousBtn')) $('previousBtn').onclick = () => applyPreviousSettings();
$('undoBtn').onclick = undo;
$('redoBtn').onclick = redo;

let exportTimer = null;
let EXPORT_RECIPES = [];
let EXPORT_RECIPE_EXTRAS = {};
let activeExportJobId = null;
let latestExportStatus = null;
const EXPORT_EXTRA_FIELDS = ['DestinationMode', 'Metadata', 'Sidecar', 'PreserveCaptureTime', 'CaptureTimePolicy'];
function exportExtraOptions(prefix) {
  return {
    ...EXPORT_RECIPE_EXTRAS,
    pairView: pairViewPreference(APP_PREFS),
    destinationMode: $(prefix + 'DestinationMode').value,
    metadata: $(prefix + 'Metadata').value,
    sidecar: $(prefix + 'Sidecar').value === 'true',
    preserveCaptureTime: $(prefix + 'PreserveCaptureTime').value === 'true',
    captureTimePolicy: $(prefix + 'CaptureTimePolicy').value,
  };
}
function syncExportDestination(prefix) {
  const relative = $(prefix + 'DestinationMode').value === 'original-folder-relative';
  $(prefix + 'ChooseDestination').disabled = relative;
  $(prefix + 'Destination').placeholder = relative ? tr("Subfolder name, e.g. film-exports") : tr("Choose a destination folder");
  if (relative && /^(?:[/\\]|[a-zA-Z]:)/.test($(prefix + 'Destination').value)) {
    $(prefix + 'Destination').value = 'film-exports';
  }
}
$('exportCancel').onclick = async () => {
  if (!activeExportJobId) return;
  $('exportCancel').disabled = true;
  const result = await api(`/api/jobs/${activeExportJobId}/cancel`, {});
  if (result.error) { $('exportCancel').disabled = false; toast(result.error); }
  else $('estat').textContent = tr("Stopping export…");
};
$('exportDetails').onclick = () => {
  const status = latestExportStatus || {};
  const lines = [`${status.completed || 0} exported · ${status.skipped || 0} skipped · ${status.cancelledCount || 0} cancelled`];
  for (const error of status.errors || []) lines.push(tr("Error: {error}", {error: error}));
  for (const item of status.warnings || []) {
    lines.push(`${item.name}${item.path ? ' → ' + item.path : ''}`);
    lines.push(...item.warnings.map((warning) => `  ${warning}`));
  }
  $('exportResultText').textContent = lines.join('\n');
  $('exportResultDialog').classList.add('on');
  $('exportResultDialog').setAttribute('aria-hidden', 'false');
  $('exportResultClose').focus();
};
function closeExportResult() {
  $('exportResultDialog').classList.remove('on');
  $('exportResultDialog').setAttribute('aria-hidden', 'true');
  $('exportDetails').focus();
}
$('exportResultClose').onclick = closeExportResult;
$('exportResultDialog').addEventListener('keydown', (event) => {
  event.stopPropagation();
  if (event.key === 'Escape') closeExportResult();
  if (event.key === 'Tab') { event.preventDefault(); $('exportResultClose').focus(); }
});
function currentExportRecipe(name = tr('Export recipe')) {
  return {
    ...exportExtraOptions('ex'),
    name, format: $('exFormat').value, quality: +$('exQuality').value,
    longEdge: $('exSize').value ? +$('exSize').value : null,
    outputSpace: $('exColorSpace').value,
    destination: $('exDestination').value.trim() || 'film-exports',
    filenameTemplate: $('exFilenameTemplate').value.trim() || '{filename}_{stock}',
    collision: $('exCollision').value,
  };
}
function renderExportRecipes(selected = '') {
  const host = $('exRecipe');
  host.replaceChildren(new Option(tr("Current settings"), ''));
  for (const recipe of EXPORT_RECIPES) {
    const option = new Option(recipe.name, recipe.id);
    option.dataset.builtin = String(!!recipe.builtin);
    host.appendChild(option);
  }
  host.value = selected;
  const active = EXPORT_RECIPES.find((recipe) => recipe.id === host.value);
  $('exRecipeDelete').disabled = !active || active.builtin;
}
function applyExportRecipe(recipe) {
  if (!recipe) return;
  if (recipe.format === 'heif' && S.serverPlatform && S.serverPlatform !== 'darwin') {
    toast(tr('HEIF export requires macOS. Choose JPEG, PNG, or TIFF.'));
    return;
  }
  const { id: _id, name: _name, builtin: _builtin, ...settings } = recipe;
  EXPORT_RECIPE_EXTRAS = settings;
  for (const key of EXPORT_EXTRA_FIELDS) {
    const field = key[0].toLowerCase() + key.slice(1);
    const defaults = { destinationMode: 'fixed', metadata: 'all-except-location', sidecar: true, preserveCaptureTime: false, captureTimePolicy: 'require-offset' };
    $('ex' + key).value = String(recipe[field] ?? defaults[field]);
  }
  $('exFormat').value = recipe.format;
  $('exQuality').value = recipe.quality;
  $('exQualityV').textContent = recipe.quality;
  $('exSize').value = recipe.longEdge || '';
  $('exColorSpace').value = recipe.outputSpace;
  $('exDestination').value = recipe.destination;
  $('exFilenameTemplate').value = recipe.filenameTemplate;
  $('exCollision').value = recipe.collision;
  syncExportDestination('ex');
}
async function loadExportRecipes() {
  EXPORT_RECIPES = await fetch('/api/export-recipes').then((response) => response.json()).catch(() => []);
  renderExportRecipes();
}
$('exRecipe').onchange = () => {
  const recipe = EXPORT_RECIPES.find((item) => item.id === $('exRecipe').value);
  applyExportRecipe(recipe);
  $('exRecipeDelete').disabled = !recipe || recipe.builtin;
};
$('exRecipeSave').onclick = async () => {
  const name = await askName(tr("Save export recipe"));
  if (!name) return;
  const recipes = await api('/api/export-recipes', {
    action: 'save', recipe: currentExportRecipe(name),
  });
  if (recipes.error) return toast(recipes.error);
  EXPORT_RECIPES = recipes;
  const saved = [...recipes].reverse().find((recipe) => recipe.name === name && !recipe.builtin);
  renderExportRecipes(saved?.id || '');
  toast(tr("Export recipe saved"));
};
$('exRecipeDelete').onclick = async () => {
  const recipe = EXPORT_RECIPES.find((item) => item.id === $('exRecipe').value);
  if (!recipe || recipe.builtin) return;
  const recipes = await api('/api/export-recipes', { action: 'delete', id: recipe.id });
  if (recipes.error) return toast(recipes.error);
  EXPORT_RECIPES = recipes; renderExportRecipes(); toast(tr("Export recipe deleted"));
};
$('exChooseDestination').onclick = () => postNative('chooseExportFolder');
$('exQuality').addEventListener('input', () => {
  $('exQualityV').textContent = $('exQuality').value;
});

async function runExport(customOpts = {}) {
  if (!await saveState(true)) return false;
  const which = customOpts.which || $('exWhich').value;
  const targets = transferTargets();
  const names = customOpts.names !== undefined
    ? customOpts.names
    : (which === 'selected' ? targets.map((im) => im.name) : undefined);

  const payload = {
    ...exportExtraOptions('ex'), ...customOpts,
    which,
    format: customOpts.format || $('exFormat').value,
    quality: customOpts.quality !== undefined ? customOpts.quality : +$('exQuality').value,
    longEdge: customOpts.longEdge !== undefined
      ? customOpts.longEdge
      : ($('exSize').value ? +$('exSize').value : null),
    outputSpace: customOpts.outputSpace || $('exColorSpace').value,
    destination: (customOpts.destination || $('exDestination').value).trim() || 'film-exports',
    filenameTemplate: (customOpts.filenameTemplate || $('exFilenameTemplate').value).trim() || '{filename}_{stock}',
    collision: customOpts.collision || $('exCollision').value,
    engine: $('engine').value,
  };
  if (names !== undefined) {
    payload.names = names;
  } else if (typeof S !== 'undefined' && S?.catalogEnabled && typeof buildCatalogQuerySpec === 'function') {
    payload.query = buildCatalogQuerySpec();
  }

  const r = await api('/api/export', payload);
  if (r.error) return toast(r.error);
  if (!r.queued) return toast(tr("Nothing matches that selection"));
  const destinationLabel = (r.destination || '').split('/').filter(Boolean).at(-1) || tr('destination');
  toast(trn('Exporting {count} photo to {destination}…', 'Exporting {count} photos to {destination}…',
    r.queued, {destination: destinationLabel}));
  clearInterval(exportTimer);
  const ident = r.jobId;
  activeExportJobId = ident;
  $('exportCancel').hidden = false;
  $('exportCancel').disabled = false;
  $('exportDetails').hidden = true;
  let exportPolls = 0;
  let polling = false;
  exportTimer = setInterval(async () => {
    if (polling) return;
    if (++exportPolls > 7200) {
      clearInterval(exportTimer);
      $('estat').textContent = tr("Export status unavailable; check Jobs.");
      return;
    }
    polling = true;
    try {
      const record = await fetch(`/api/jobs/${ident}`).then((response) => response.json());
      if (activeExportJobId !== ident) return;
      const st = record.result || {};
      latestExportStatus = st;
      $('estat').textContent = st.cancel_requested ? tr("Stopping export…") : `${record.progress}/${record.total}`;
      $('exportDetails').hidden = !(st.errors?.length || st.warnings?.length);
      if (['done', 'failed', 'cancelled'].includes(record.state)) {
        clearInterval(exportTimer);
        activeExportJobId = null;
        $('exportCancel').hidden = true;
        const warnings = st.warnings?.length || 0;
        const errors = record.errors?.length || 0;
        latestExportStatus = { ...st, errors: record.errors || [] };
        $('exportDetails').hidden = false;
        $('estat').textContent = [
          trn('{count} photo exported', '{count} photos exported', st.completed || 0),
          record.state === 'cancelled' ? tr('Cancelled') : tr('Done'),
          warnings ? trn('{count} warning', '{count} warnings', warnings) : '',
          errors ? trn('{count} error', '{count} errors', errors) : '',
        ].filter(Boolean).join(' · ');
        toast($('estat').textContent, st.completed > 0 && st.revealPath ? {
          label: tr('Show in Finder'), link: true,
          run: () => postNative('revealFolder', { path: st.revealPath }),
        } : null, 2800);
        notifyCompletion(record.state === 'cancelled' ? tr("Export cancelled") : tr("Export complete"), $('estat').textContent);
      }
    } catch (_error) {
      if (activeExportJobId === ident) $('estat').textContent = tr("Reconnecting to export…");
    } finally { polling = false; }
  }, 600);
  return r;
}

/* ------------------------------------------------ modern export modal */
function closeExportModal() {
  clearTimeout(exportPreviewTimer);
  exportPreviewGeneration++;
  exportPreviewController?.abort();
  const dialog = $('exportDialog');
  if (!dialog) return;
  dialog.classList.remove('on');
  dialog.setAttribute('aria-hidden', 'true');
  exportReturnFocus?.focus();
  exportReturnFocus = null;
}

const EXPORT_MODAL_PRESETS = {
  'jpg-large': { format: 'jpeg', size: '', quality: 92, colorSpace: 'srgb' },
  'jpg-small': { format: 'jpeg', size: '2048', quality: 90, colorSpace: 'srgb' },
  'tiff-archive': { format: 'tif', size: '', quality: 100, colorSpace: 'prophoto' },
};

let exportReturnFocus = null;
let exportPreviewTimer = null;
let exportPreviewGeneration = 0;
let exportPreviewController = null;

function exportModalOptions() {
  const which = $('modalExWhich').value;
  return {
    ...exportExtraOptions('modalEx'),
    which,
    ...(which === 'selected' ? { names: transferTargets().map((im) => im.name) } : {}),
    format: $('modalExFormat').value,
    quality: +$('modalExQuality').value,
    longEdge: $('modalExSize').value ? +$('modalExSize').value : null,
    outputSpace: $('modalExColorSpace').value,
    destination: $('modalExDestination').value.trim() || 'film-exports',
    filenameTemplate: $('modalExFilenameTemplate').value.trim() || '{filename}_{stock}',
    collision: $('modalExCollision').value,
  };
}

function scheduleExportPreview() {
  clearTimeout(exportPreviewTimer);
  const request = ++exportPreviewGeneration;
  exportPreviewController?.abort();
  $('exportPreviewFilename').textContent = tr("Updating preview…");
  $('exportPreviewDimensions').textContent = '';
  $('exportPreviewDestination').textContent = '';
  $('exportPreviewNote').textContent = '';
  exportPreviewTimer = setTimeout(async () => {
    exportPreviewController = new AbortController();
    try {
      const response = await fetch('/api/export/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(exportModalOptions()), signal: exportPreviewController.signal,
      });
      const result = await response.json();
      if (request !== exportPreviewGeneration) return;
      if (!response.ok || result.error) throw new Error(((result.error || tr("Preview unavailable"))));
      const sample = result.sample;
      $('exportModalTitle').textContent = $('exportModalRun').textContent =
        trn("Export {count} Photo", "Export {count} Photos", result.total, {resultTotal: result.total});
      $('exportModalRun').disabled = !result.total;
      $('exportPreviewFilename').textContent = ((sample?.filename || tr("No photos match this selection")));
      $('exportPreviewDimensions').textContent = sample?.dimensionsExact ? tr("{value} × {value2} px", {value: sample.width.toLocaleString(), value2: sample.height.toLocaleString()}) : (sample?.dimensionsNote || '');
      $('exportPreviewDestination').textContent = (result.samples || [sample]).filter(Boolean).map((item) => item.path).join('\n');
      $('exportPreviewNote').textContent = sample?.skipped ? tr("This file already exists and will be skipped.") : tr("{value}{value2}Names reflect existing files and may change before export.", {value: result.total > 1 ? tr("{resultTotal} photos · first output shown. ", {resultTotal: result.total}) : '', value2: result.destinationCount > 1 ? tr("{resultDestinationCount} destination folders. ", {resultDestinationCount: result.destinationCount}) : ''});
    } catch (error) {
      if (request !== exportPreviewGeneration || error.name === 'AbortError') return;
      $('exportPreviewFilename').textContent = tr("Preview unavailable");
      $('exportPreviewNote').textContent = error.message;
    }
  }, 180);
}


function markExportPreset(presetKey) {
  document.querySelectorAll('.export-preset-pill').forEach((pill) => {
    const active = pill.dataset.preset === presetKey;
    pill.classList.toggle('on', active);
    pill.setAttribute('aria-selected', String(active));
  });
}

function matchingExportPreset() {
  const current = {
    format: $('modalExFormat').value,
    size: $('modalExSize').value,
    quality: +$('modalExQuality').value,
    colorSpace: $('modalExColorSpace').value,
  };
  return Object.entries(EXPORT_MODAL_PRESETS).find(([, preset]) =>
    preset.format === current.format && preset.size === current.size &&
    preset.quality === current.quality && preset.colorSpace === current.colorSpace
  )?.[0] || 'custom';
}

function applyExportPreset(presetKey) {
  markExportPreset(presetKey);
  const p = EXPORT_MODAL_PRESETS[presetKey];
  if (!p) return;
  $('modalExFormat').value = p.format;
  $('modalExSize').value = p.size;
  $('modalExQuality').value = p.quality;
  $('modalExQualityV').textContent = p.quality;
  $('modalExColorSpace').value = p.colorSpace;
  $('modalExQualityRow').style.display = ['jpeg', 'heif'].includes(p.format) ? '' : 'none';
  scheduleExportPreview();
}

function updateExportModalScope() {
  const which = $('modalExWhich').value;
  const targets = transferTargets();
  const pickedCount = S.images.filter((im) => im.status === 'approved').length;
  const ratedCount = S.images.filter((im) => (im.rating || 0) >= 1).length;
  const allCount = S.images.filter((im) => im.status !== 'skipped').length;

  let count = 0;
  let label = '';
  if (which === 'selected') {
    count = targets.length;
    label = (count === 1 ? (targets[0]?.displayName || targets[0]?.name || tr("Selected photo")) : tr("{count} photos selected", {count: count}));
  } else if (which === 'approved') {
    count = pickedCount;
    label = trn("{count} picked photo", "{count} picked photos", count, {count: count});
  } else if (which === 'rated') {
    count = ratedCount;
    label = trn("{count} rated photo", "{count} rated photos", count, {count: count});
  } else {
    count = allCount;
    label = trn('{count} photo', '{count} photos', count);
  }

  const titleText = trn('Export {count} Photo', 'Export {count} Photos', count);
  $('exportModalTitle').textContent = titleText;
  $('exportModalRun').textContent = titleText;
  $('exportTargetLabel').textContent = label;
  $('exportModalRun').disabled = !count;
  scheduleExportPreview();
}

function openExportModal() {
  const targets = transferTargets();
  const activeImage = targets[0] || cur();
  if (!$('exportDialog').classList.contains('on')) {
    exportReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
  }

  $('modalExFormat').value = $('exFormat').value;
  $('modalExQuality').value = $('exQuality').value;
  $('modalExQualityV').textContent = $('exQuality').value;
  $('modalExSize').value = $('exSize').value;
  $('modalExColorSpace').value = $('exColorSpace').value;
  if ($('exDestination')?.value) $('modalExDestination').value = $('exDestination').value;
  if ($('exFilenameTemplate')?.value) $('modalExFilenameTemplate').value = $('exFilenameTemplate').value;
  if ($('exCollision')?.value) $('modalExCollision').value = $('exCollision').value;
  for (const key of EXPORT_EXTRA_FIELDS) $('modalEx' + key).value = $('ex' + key).value;
  syncExportDestination('modalEx');

  const thumbEl = $('exportTargetThumb');
  if (thumbEl) {
    if (activeImage) {
      const thumbKey = encodeURIComponent(activeImage.fileKey || activeImage.mtime || '');
      thumbEl.style.backgroundImage = `url('/api/thumb?name=${encodeURIComponent(activeImage.name)}&key=${thumbKey}')`;
      thumbEl.innerHTML = '';
    } else {
      thumbEl.style.backgroundImage = 'none';
      thumbEl.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';
    }
  }

  const pickedCount = S.images.filter((im) => im.status === 'approved').length;
  const ratedCount = S.images.filter((im) => (im.rating || 0) >= 1).length;
  const allCount = S.images.filter((im) => im.status !== 'skipped').length;

  const select = $('modalExWhich');
  select.innerHTML = '';
  const selLabel = (targets.length === 1 ? tr("Selected photo ({value})", {value: activeImage?.displayName || activeImage?.name || '1'}) : tr("Selected photos ({targetsLength})", {targetsLength: targets.length}));
  select.appendChild(new Option(selLabel, 'selected'));
  select.appendChild(new Option(tr("Picked only ({pickedCount})", {pickedCount: pickedCount}), 'approved'));
  select.appendChild(new Option(tr("Rated 1+ ({ratedCount})", {ratedCount: ratedCount}), 'rated'));
  select.appendChild(new Option(tr("All except rejected ({allCount})", {allCount: allCount}), 'all'));

  select.value = targets.length ? 'selected' : (pickedCount ? 'approved' : 'all');

  $('exportModalSubtitle').textContent = activeImage ? (activeImage.displayName || activeImage.name) : targets.length ? tr("{targetsLength} photos selected", {targetsLength: targets.length}) : tr("Choose photos to export");

  updateExportModalScope();
  markExportPreset(matchingExportPreset());
  $('modalExQualityRow').style.display = ['jpeg', 'heif'].includes($('modalExFormat').value) ? '' : 'none';

  $('exportDialog').classList.add('on');
  $('exportDialog').setAttribute('aria-hidden', 'false');
  requestAnimationFrame(() => $('modalExWhich').focus());
}

$('exportModalClose').onclick = closeExportModal;
$('exportModalCancel').onclick = closeExportModal;
$('exportDialog').addEventListener('pointerdown', (e) => {
  if (e.target === $('exportDialog')) closeExportModal();
});
$('exportDialog').addEventListener('keydown', (event) => {
  event.stopPropagation();
  if (event.key === 'Escape') {
    event.preventDefault();
    closeExportModal();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...$('exportDialog').querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
  )].filter((element) => !element.hidden && element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

document.querySelectorAll('.export-preset-pill').forEach((pill) => {
  pill.onclick = () => applyExportPreset(pill.dataset.preset);
});

['modalExFormat', 'modalExSize', 'modalExQuality', 'modalExColorSpace'].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener('input', () => {
    document.querySelectorAll('.export-preset-pill').forEach((p) => {
      const active = p.dataset.preset === 'custom';
      p.classList.toggle('on', active);
      p.setAttribute('aria-selected', String(active));
    });
    if (id === 'modalExQuality') {
      $('modalExQualityV').textContent = $('modalExQuality').value;
    }
    if (id === 'modalExFormat') {
      $('modalExQualityRow').style.display = ['jpeg', 'heif'].includes($('modalExFormat').value) ? '' : 'none';
    }
  });
});

['modalExFormat', 'modalExSize', 'modalExQuality', 'modalExColorSpace',
  'modalExDestination', 'modalExFilenameTemplate', 'modalExCollision',
  ...EXPORT_EXTRA_FIELDS.map((key) => 'modalEx' + key)].forEach((id) => {
  $(id).addEventListener('input', scheduleExportPreview);
  $(id).addEventListener('change', scheduleExportPreview);
});

for (const prefix of ['ex', 'modalEx']) {
  $(prefix + 'DestinationMode').addEventListener('change', () => {
    syncExportDestination(prefix);
    if (prefix === 'modalEx') scheduleExportPreview();
  });
}
$('modalExWhich').onchange = updateExportModalScope;
$('modalExChooseDestination').onclick = () => postNative('chooseExportFolder');

$('exportModalRun').onclick = async () => {
  const which = $('modalExWhich').value;
  const targets = transferTargets();
  const names = which === 'selected'
    ? (targets.length ? targets.map((im) => im.name) : (cur() ? [cur().name] : []))
    : undefined;

  $('exWhich').value = which;
  $('exFormat').value = $('modalExFormat').value;
  $('exQuality').value = $('modalExQuality').value;
  $('exQualityV').textContent = $('modalExQuality').value;
  $('exSize').value = $('modalExSize').value;
  $('exColorSpace').value = $('modalExColorSpace').value;
  $('exDestination').value = $('modalExDestination').value.trim() || 'film-exports';
  $('exFilenameTemplate').value = $('modalExFilenameTemplate').value.trim() || '{filename}_{stock}';
  $('exCollision').value = $('modalExCollision').value;
  for (const key of EXPORT_EXTRA_FIELDS) $('ex' + key).value = $('modalEx' + key).value;
  savePrefs();
  closeExportModal();
  await runExport({
    ...exportExtraOptions('modalEx'),
    which,
    names,
    format: $('modalExFormat').value,
    quality: +$('modalExQuality').value,
    longEdge: $('modalExSize').value ? +$('modalExSize').value : null,
    outputSpace: $('modalExColorSpace').value,
    destination: $('modalExDestination').value.trim() || 'film-exports',
    filenameTemplate: $('modalExFilenameTemplate').value.trim() || '{filename}_{stock}',
    collision: $('modalExCollision').value,
  });
};

$('exportBtn').onclick = openExportModal;
/* Call it, do not hand it the click. `runExport`'s first argument is the option
 * overrides, and a MouseEvent carries a truthy `which`, which would override
 * the export scope with a mouse button number and queue the whole catalog. */
$('exportBtn2').onclick = () => runExport();
loadExportRecipes();

/* ------------------------------------------------ external pixel editor */
let externalEditTimer = null;
function closeExternalEdit() {
  $('externalEditDialog').classList.remove('on');
  $('externalEditDialog').setAttribute('aria-hidden', 'true');
}
$('editExternalOpen').onclick = async () => {
  if (!transferTargets().length) return toast(tr("Select a photo to edit"));
  EXTERNAL_PREFS = await getJSON('/api/prefs').catch(() => ({}));
  postNative('listEditors', {}, true);
  const savedPath = EXTERNAL_PREFS.externalEditor?.path || '';
  if (savedPath && !$('externalEditor').querySelector(
      `option[value="${CSS.escape(savedPath)}"]`)) {
    $('externalEditor').appendChild(new Option(
      ((EXTERNAL_PREFS.externalEditor?.name || savedPath.split('/').pop())), savedPath));
  }
  $('externalEditor').value = savedPath;
  $('externalEditSpace').value = EXTERNAL_PREFS.externalEditorSpace || 'prophoto';
  $('externalEditBitDepth').value = String(EXTERNAL_PREFS.externalEditBitDepth || 16);
  $('externalEditStack').checked = EXTERNAL_PREFS.externalEditStack !== false;
  const hasRaw = transferTargets().some((image) => image.raw);
  $('externalEditMode').querySelector('[value="original"]').disabled = hasRaw;
  $('externalEditMode').value = hasRaw ? 'adjusted' : 'adjusted';
  $('externalEditStatus').textContent = hasRaw ? tr("RAW originals are rendered to an adjusted {valueValue}-bit TIFF.", {valueValue: $('externalEditBitDepth').value}) : tr("The adjusted TIFF is written beside its original.");
  $('externalEditDialog').classList.add('on');
  $('externalEditDialog').setAttribute('aria-hidden', 'false');
};
$('externalEditCancel').onclick = closeExternalEdit;
$('externalEditDialog').addEventListener('pointerdown', (event) => {
  if (event.target === $('externalEditDialog') && !externalEditTimer) closeExternalEdit();
});
$('externalEditMode').onchange = () => {
  $('externalEditSpace').disabled = $('externalEditMode').value === 'original';
};
$('externalEditor').onchange = () => {
  if ($('externalEditor').value === '__choose__') {
    $('externalEditor').value = '';
    postNative('chooseExternalEditor');
  }
};
$('externalEditRun').onclick = async () => {
  const targets = transferTargets();
  if (!targets.length) return;
  if (!await saveState(true)) return;
  const editorPath = $('externalEditor').value;
  const editorName = $('externalEditor').selectedOptions[0]?.textContent || '';
  const externalPatch = {
    externalEditor: { path: editorPath, name: editorName },
    externalEditorSpace: $('externalEditSpace').value,
    externalEditBitDepth: +$('externalEditBitDepth').value,
    externalEditStack: $('externalEditStack').checked,
  };
  Object.assign(APP_PREFS, externalPatch);
  api('/api/prefs', externalPatch);
  const result = await api('/api/edit-external', {
    names: targets.map((image) => image.name),
    mode: $('externalEditMode').value,
    space: $('externalEditSpace').value,
    bitDepth: +$('externalEditBitDepth').value,
    stackWithOriginal: $('externalEditStack').checked,
    app: editorPath,
  });
  if (!result.ok) {
    $('externalEditStatus').textContent = ((result.error || tr("External edit failed.")));
    return;
  }
  const openPaths = async (paths, names = []) => {
    closeExternalEdit();
    postNative('openWith', { paths, app: editorPath });
    await reloadLibrary();
    const selected = names.length
      ? S.images.findIndex((image) => image.name === names.at(-1)) : -1;
    if (selected >= 0) go(selected);
  };
  if (!result.running) return openPaths(result.paths, result.names);
  $('externalEditRun').disabled = true;
  $('externalEditStatus').textContent = tr("Rendering 0/{resultQueued}…", {resultQueued: result.queued});
  clearInterval(externalEditTimer);
  let polls = 0;
  externalEditTimer = setInterval(async () => {
    if (++polls > 1800) {
      clearInterval(externalEditTimer); externalEditTimer = null;
      $('externalEditRun').disabled = false;
      $('externalEditStatus').textContent = tr("The TIFF is still rendering.");
      return;
    }
    const status = await getJSON('/api/edit-external/status').catch(() => null);
    if (!status) return;
    $('externalEditStatus').textContent = tr("Rendering {statusDone}/{statusTotal}{value}", {statusDone: status.done, statusTotal: status.total, value: status.errors?.length ? tr(" · {statusErrorsLength} error", {statusErrorsLength: status.errors.length}) : ''});
    if (!status.running) {
      clearInterval(externalEditTimer); externalEditTimer = null;
      $('externalEditRun').disabled = false;
      if (status.paths?.length) {
        await openPaths(status.paths, status.names || []);
        const warnings = status.warnings || [];
        if (warnings.length) toast(tr("TIFF created with warnings: {valueName}: {value}", {valueName: warnings[0].name, value: warnings[0].warnings.join(' ')}));
      }
      else $('externalEditStatus').textContent = ((status.errors?.[0] || tr("No TIFF was created.")));
    }
  }, 900);
};

/* ----------------------------------------------------- merge and proof */
let mergeTimer = null;
$('mergeMode').onchange = updateTransferActions;
function selectedMergeNames() {
  return [...new Set(transferTargets().map((image) => image.sourceName || image.name))];
}
function finishMergePolling(message, kind = '') {
  clearInterval(mergeTimer); mergeTimer = null;
  $('mergeRun').disabled = selectedMergeNames().length < 2;
  $('mergeStatus').textContent = message;
  $('mergeStatus').className = `merge-status${kind ? ` ${kind}` : ''}`;
}
$('mergeRun').onclick = async () => {
  const names = selectedMergeNames();
  if (names.length < 2) return toast(tr("Select at least two distinct originals"));
  if (!await saveState(true)) return;
  const result = await api('/api/merge', {
    mode: $('mergeMode').value, names, name: $('mergeName').value.trim(),
  });
  if (result.error) return finishMergePolling(result.error, 'error');
  $('mergeRun').disabled = true;
  $('mergeStatus').className = 'merge-status running';
  $('mergeStatus').textContent = tr("Preparing 0/{resultQueued} photos…", {resultQueued: result.queued});
  let polls = 0;
  clearInterval(mergeTimer);
  mergeTimer = setInterval(async () => {
    polls += 1;
    if (polls > 1800) return finishMergePolling(tr("Merge is still running; reopen this panel to check."), 'error');
    try {
      const status = await fetch('/api/merge/status').then((response) => response.json());
      if (status.running) {
        const labels = {
          preparing: tr("Preparing photos"), aligning: tr("Aligning frames"),
          fusing: tr("Fusing exposures"), analyzing: tr("Measuring sharpness"),
          blending: (status.mode === 'panorama' ? tr("Blending panorama") : tr("Fusing sharp regions")),
          saving: tr("Writing 16-bit TIFF"),
        };
        const phase = (labels[status.phase] || tr("Merging"));
        const count = status.phaseTotal > 1
          ? ` ${status.phaseProgress || 0}/${status.phaseTotal}` : '';
        const elapsed = status.elapsedSeconds >= 1
          ? tr('{seconds}s', { seconds: Math.round(status.elapsedSeconds) }) : '';
        const inliers = status.mode === 'focus' && status.phase === 'aligning'
          ? trn('{count} minimum alignment inlier', '{count} minimum alignment inliers', status.alignmentInliers || 0) : '';
        $('mergeStatus').textContent = [`${phase}${count}…`, elapsed, inliers].filter(Boolean).join(' · ');
      } else if (status.error) {
        finishMergePolling(status.error, 'error');
        toast(tr("Merge failed"));
      } else if (status.output) {
        finishMergePolling(status.elapsedSeconds
          ? tr('Created {name} in {seconds}s', { name: status.output, seconds: status.elapsedSeconds.toFixed(1) })
          : tr('Created {name}', { name: status.output }));
        toast(tr("Merge complete"));
        setTimeout(() => location.reload(), 650);
      }
    } catch (error) {
      finishMergePolling(tr("Merge status unavailable: {errorMessage}", {errorMessage: error.message}), 'error');
    }
  }, 1000);
};

function syncSoftProof() {
  $('softProofEnabled').checked = !!S.softProof.enabled;
  $('softProofProfile').value = S.softProof.profile;
  $('softProofPaper').checked = !!S.softProof.paper;
  $('softProofGamut').checked = !!S.softProof.gamut;
  const printTarget = ['matte', 'gloss'].includes(S.softProof.profile);
  $('softProofPaper').disabled = !S.softProof.enabled || !printTarget;
  $('softProofProfile').disabled = !S.softProof.enabled;
  $('softProofGamut').disabled = !S.softProof.enabled;
  $('softProofToolbar').hidden = !S.softProof.enabled;
  $('softProofToolbar').classList.toggle('on', !!S.softProof.enabled);
  $('softProofToolbar').setAttribute('aria-pressed', String(!!S.softProof.enabled));
}
function readSoftProof() {
  const wasEnabled = S.softProof.enabled;
  S.softProof = { enabled: $('softProofEnabled').checked,
    profile: $('softProofProfile').value, paper: $('softProofPaper').checked,
    gamut: $('softProofGamut').checked };
  syncSoftProof(); savePrefs();
  if (wasEnabled !== S.softProof.enabled) renderFilm(0); else drawGrade();
  scheduleNativeMenuState();
}
['softProofEnabled', 'softProofProfile', 'softProofPaper', 'softProofGamut']
  .forEach((id) => $(id).addEventListener('change', readSoftProof));
$('softProofToolbar').onclick = () => {
  $('softProofEnabled').checked = !$('softProofEnabled').checked;
  $('softProofEnabled').dispatchEvent(new Event('change', { bubbles: true }));
};
$('viewProofControls').append(...$('proofSupport').childNodes);
$('viewOptions').addEventListener('keydown', (event) => {
  event.stopPropagation();
  if (event.key === 'Escape') { $('viewOptions').open = false; $('viewOptions').querySelector('summary').focus(); }
});
document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('#viewOptions')) $('viewOptions').open = false;
});
syncSoftProof();

/* ------------------------------------------------------ hold-key sliders */
const speedTapTimes = new Map();

function speedInput(control) {
  return document.querySelector(`[data-g="${control}"]`);
}

function showSpeedHud(control) {
  const input = speedInput(control);
  const hud = $('speedHud');
  if (!input || !hud) return;
  const row = input.closest('.slider-row');
  const label = row?.querySelector('.name')?.textContent?.trim() || control;
  hud.textContent = `${label}  ${fmtG(input.value)}`;
  hud.hidden = false;
}

function setSpeedValue(value) {
  if (!S.speed) return;
  const input = speedInput(S.speed.control);
  if (!input) return;
  if (!S.speed.undoCaptured) {
    pushUndo();
    S.speed.undoCaptured = true;
  }
  const minimum = Number(input.min);
  const maximum = Number(input.max);
  const next = clamp(value,
    Number.isFinite(minimum) ? minimum : -Infinity,
    Number.isFinite(maximum) ? maximum : Infinity);
  input.value = String(next);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  S.speed.used = true;
  showSpeedHud(S.speed.control);
}

function adjustSpeed(direction, event, absolute = null) {
  if (!S.speed) return;
  const input = speedInput(S.speed.control);
  if (!input) return;
  const step = Number(input.step) || 0.01;
  const multiplier = event?.altKey ? 20 : event?.shiftKey ? 1 : 5;
  setSpeedValue(absolute == null
    ? Number(input.value) + direction * step * multiplier
    : absolute);
}

function runSpeedTapAction(key) {
  if (key === KEYS.detail) setViewMode('detail');
  else if (key === 'h' &&
      (S.activePane === 'maskPane' || S.activePane === 'healPane')) {
    S.localPinsVisible = !S.localPinsVisible;
    drawEditOverlay();
    toast(S.localPinsVisible ? tr("Edit pins shown") : tr("Edit pins hidden"));
  }
}

function finishSpeedKey(key) {
  if (!S.speed || S.speed.key !== key) return false;
  const speed = S.speed;
  S.speed = null;
  $('speedHud').hidden = true;
  if (speed.used) {
    speedInput(speed.control)?.dispatchEvent(new Event('change', { bubbles: true }));
    speedTapTimes.delete(key);
    return true;
  }
  const now = performance.now();
  const last = speedTapTimes.get(key) || 0;
  speedTapTimes.set(key, now);
  if (now - last <= 350) {
    const input = speedInput(speed.control);
    if (input) {
      pushUndo();
      input.value = String(GRADE_DEFAULTS[speed.control] ?? 0);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      showSpeedHud(speed.control);
      $('speedHud').hidden = true;
    }
    speedTapTimes.delete(key);
  } else runSpeedTapAction(key);
  return true;
}

/* ------------------------------------------------------------ pointer UI */
(function () {
  const wrap = $('zoomwrap');
  let wheelFrame = 0;
  let wheelScale = 1;
  let wheelPanX = 0;
  let wheelPanY = 0;
  let wheelPoint = [0, 0];
  const flushWheel = () => {
    wheelFrame = 0;
    if (wheelScale !== 1) {
      zoomAt(wheelScale, wheelPoint[0], wheelPoint[1]);
    } else if (wheelPanX || wheelPanY) {
      stopZoomMotion();
      S.panX -= wheelPanX; S.panY -= wheelPanY; applyView();
    }
    wheelScale = 1; wheelPanX = 0; wheelPanY = 0;
  };
  window.addEventListener('lighttable-magnify', ({detail}) => {
    if (S.viewMode !== 'detail' || !cur() || S.editGesture || S.cropTransition ||
        document.querySelector('.modal-backdrop.on')) return;
    const {factor, x, y} = detail || {};
    if (!Number.isFinite(factor) || factor <= 0 || factor === 1 ||
        !Number.isFinite(x) || !Number.isFinite(y) ||
        !wrap.contains(document.elementFromPoint(x, y))) return;
    wheelScale *= factor;
    wheelPoint = [x, y];
    if (!wheelFrame) wheelFrame = requestAnimationFrame(flushWheel);
  });
  wrap.addEventListener('wheel', (e) => {
    if (S.speed) {
      e.preventDefault();
      adjustSpeed(e.deltaY > 0 ? -1 : 1, e);
      return;
    }
    if (e.ctrlKey) {
      e.preventDefault();
      wheelScale *= Math.exp(-e.deltaY * 0.01);
      wheelPoint = [e.clientX, e.clientY];
    } else if (S.zoom > 1) {
      e.preventDefault(); wheelPanX += e.deltaX; wheelPanY += e.deltaY;
    } else return;
    if (!wheelFrame) wheelFrame = requestAnimationFrame(flushWheel);
  }, { passive: false });
  let gStart = 1;
  let gestureFrame = 0;
  let gestureEvent = null;
  wrap.addEventListener('gesturestart', (e) => { e.preventDefault(); stopZoomMotion(); gStart = S.zoom; });
  wrap.addEventListener('gesturechange', (e) => {
    e.preventDefault();
    gestureEvent = { scale: e.scale, x: e.clientX, y: e.clientY };
    if (!gestureFrame) gestureFrame = requestAnimationFrame(() => {
      gestureFrame = 0;
      const latest = gestureEvent;
      zoomAt(clamp(gStart * latest.scale, Math.min(1, gStart), Math.max(16, gStart)) / S.zoom,
        latest.x, latest.y);
    });
  });
  wrap.addEventListener('gestureend', (e) => e.preventDefault());

  // Claim horizontal speed drags before crop, mask, heal, or pan handlers.
  wrap.addEventListener('pointerdown', (e) => {
    if (!S.speed || e.button !== 0) return;
    const input = speedInput(S.speed.control);
    if (!input) return;
    S.speed.drag = { pointerId: e.pointerId, startX: e.clientX,
      startValue: Number(input.value) };
    wrap.setPointerCapture?.(e.pointerId);
    e.preventDefault(); e.stopImmediatePropagation();
  }, true);
  wrap.addEventListener('pointermove', (e) => {
    if (!S.speed?.drag || S.speed.drag.pointerId !== e.pointerId) return;
    const input = speedInput(S.speed.control);
    const step = Number(input?.step) || 0.01;
    const multiplier = e.altKey ? 20 : e.shiftKey ? 1 : 5;
    const delta = (e.clientX - S.speed.drag.startX) / 4;
    if (Math.abs(delta) >= 0.25) {
      adjustSpeed(0, e, S.speed.drag.startValue + delta * step * multiplier);
    }
    e.preventDefault(); e.stopImmediatePropagation();
  }, true);
  const finishSpeedDrag = (e) => {
    if (!S.speed?.drag || S.speed.drag.pointerId !== e.pointerId) return;
    S.speed.drag = null;
    e.preventDefault(); e.stopImmediatePropagation();
  };
  wrap.addEventListener('pointerup', finishSpeedDrag, true);
  wrap.addEventListener('pointercancel', finishSpeedDrag, true);

  // Crop: draw a new selection, move the existing frame, or resize from any handle.
  const layer = $('cropLayer');
  let interaction = null;

  // The frame moves and grows under the pointer while cropping, so map through
  // its live rectangle rather than the one captured on pointer down.
  const pointInLayer = (event, rect = layer.getBoundingClientRect()) => {
    return {
      x: clamp((event.clientX - rect.left) / rect.width, 0, 1),
      y: clamp((event.clientY - rect.top) / rect.height, 0, 1),
    };
  };

  const ratioBox = (anchor, point, ratio) => {
    const signX = point.x >= anchor.x ? 1 : -1;
    const signY = point.y >= anchor.y ? 1 : -1;
    let width = Math.abs(point.x - anchor.x);
    let height = Math.abs(point.y - anchor.y);
    if (height * ratio > width) width = height * ratio;
    else height = width / ratio;
    const maxWidth = signX > 0 ? 1 - anchor.x : anchor.x;
    const maxHeight = signY > 0 ? 1 - anchor.y : anchor.y;
    const scale = Math.min(1, maxWidth / Math.max(width, 0.0001), maxHeight / Math.max(height, 0.0001));
    width *= scale; height *= scale;
    return clampCrop({
      x: signX > 0 ? anchor.x : anchor.x - width,
      y: signY > 0 ? anchor.y : anchor.y - height,
      w: width,
      h: height,
    });
  };

  const resizeCrop = (start, handle, point, ratio, layerRect) => {
    const left = start.x;
    const right = start.x + start.w;
    const top = start.y;
    const bottom = start.y + start.h;
    const minWidth = Math.min(0.2, 24 / Math.max(layerRect.width, 1));
    const minHeight = Math.min(0.2, 24 / Math.max(layerRect.height, 1));

    if (ratio && handle.length === 2) {
      const anchor = {
        x: handle.includes('w') ? right : left,
        y: handle.includes('n') ? bottom : top,
      };
      const next = ratioBox(anchor, point, ratio);
      if (next.w >= minWidth && next.h >= minHeight) return next;
      return start;
    }

    if (ratio && (handle === 'e' || handle === 'w')) {
      const fixedX = handle === 'e' ? left : right;
      const sign = handle === 'e' ? 1 : -1;
      let width = Math.max(minWidth, minHeight * ratio, Math.abs(point.x - fixedX));
      width = Math.min(width, sign > 0 ? 1 - fixedX : fixedX, ratio);
      let height = width / ratio;
      if (height > 1) { height = 1; width = height * ratio; }
      return clampCrop({
        x: sign > 0 ? fixedX : fixedX - width,
        y: top + start.h / 2 - height / 2,
        w: width,
        h: height,
      });
    }

    if (ratio && (handle === 'n' || handle === 's')) {
      const fixedY = handle === 's' ? top : bottom;
      const sign = handle === 's' ? 1 : -1;
      let height = Math.max(minHeight, minWidth / ratio, Math.abs(point.y - fixedY));
      height = Math.min(height, sign > 0 ? 1 - fixedY : fixedY, 1 / ratio);
      let width = height * ratio;
      if (width > 1) { width = 1; height = width / ratio; }
      return clampCrop({
        x: left + start.w / 2 - width / 2,
        y: sign > 0 ? fixedY : fixedY - height,
        w: width,
        h: height,
      });
    }

    let nextLeft = handle.includes('w') ? Math.min(point.x, right - minWidth) : left;
    let nextRight = handle.includes('e') ? Math.max(point.x, left + minWidth) : right;
    let nextTop = handle.includes('n') ? Math.min(point.y, bottom - minHeight) : top;
    let nextBottom = handle.includes('s') ? Math.max(point.y, top + minHeight) : bottom;
    nextLeft = clamp(nextLeft, 0, right - minWidth);
    nextRight = clamp(nextRight, left + minWidth, 1);
    nextTop = clamp(nextTop, 0, bottom - minHeight);
    nextBottom = clamp(nextBottom, top + minHeight, 1);
    return clampCrop({ x: nextLeft, y: nextTop, w: nextRight - nextLeft, h: nextBottom - nextTop });
  };

  const captureUndoOnce = () => {
    if (interaction.historyCaptured) return;
    pushUndoState(interaction.beforeState);
    interaction.historyCaptured = true;
  };

  layer.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || S.cropTransition) return;
    const rect = layer.getBoundingClientRect();
    const point = pointInLayer(e, rect);
    const handle = e.target.closest('[data-crop-handle]')?.dataset.cropHandle;
    const startCrop = S.crop ? { ...S.crop } : { x: 0, y: 0, w: 1, h: 1 };
    // Without a committed crop the frame shown is the full image, which has
    // nowhere to move; a drag inside it draws the first crop instead of
    // recording a no-op edit and leaving a phantom full-frame crop behind.
    const moving = !handle && S.crop && e.target.closest('#cropRect');
    interaction = {
      action: handle ? 'resize' : moving ? 'move' : 'draw',
      beforeState: snapshot(),
      historyCaptured: false,
      startCrop,
      startPoint: point,
      handle,
      rect,
    };
    if (handle) layer.dataset.activeCropHandle = handle;
    cropInteractionKind = interaction.action;
    layer.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  layer.addEventListener('pointermove', (e) => {
    if (!interaction) return;
    const layerRect = layer.getBoundingClientRect();
    const point = pointInLayer(e, layerRect);
    if (Math.hypot(point.x - interaction.startPoint.x, point.y - interaction.startPoint.y) < 0.001) return;
    captureUndoOnce();
    interaction.lastEvent = { clientX: e.clientX, clientY: e.clientY };
    const ratio = cropLayerRatio();
    if (interaction.action === 'move') {
      // The frame stays put and the photo follows the pointer underneath it,
      // so the crop travels the opposite way through the photo. The photo pans
      // as the pointer moves, so measure against the rectangle from pointer down.
      const start = interaction.startCrop;
      const moved = pointInLayer(e, interaction.rect);
      S.crop = {
        x: clamp(start.x - (moved.x - interaction.startPoint.x), 0, 1 - start.w),
        y: clamp(start.y - (moved.y - interaction.startPoint.y), 0, 1 - start.h),
        w: start.w,
        h: start.h,
      };
    } else if (interaction.action === 'resize') {
      S.crop = resizeCrop(
        interaction.startCrop, interaction.handle, point, ratio, layerRect);
    } else if (ratio) {
      S.crop = ratioBox(interaction.startPoint, point, ratio);
    } else {
      S.crop = clampCrop({
        x: Math.min(interaction.startPoint.x, point.x),
        y: Math.min(interaction.startPoint.y, point.y),
        w: Math.abs(point.x - interaction.startPoint.x),
        h: Math.abs(point.y - interaction.startPoint.y),
      });
    }
    markContinuousInput();
    applyCropVisual();
    e.preventDefault();
  });
  cropPointerRefresh = () => {
    if (!interaction || interaction.action !== 'resize' || !interaction.lastEvent) return;
    const layerRect = layer.getBoundingClientRect();
    const point = pointInLayer(interaction.lastEvent, layerRect);
    S.crop = resizeCrop(
      interaction.startCrop, interaction.handle, point, cropLayerRatio(), layerRect);
    applyCropVisualNow();
  };
  const finishCropInteraction = () => {
    if (!interaction) return;
    const changed = interaction.historyCaptured;
    const drawn = interaction.action === 'draw';
    interaction = null;
    cropInteractionKind = null;
    delete layer.dataset.activeCropHandle;
    if (drawn) syncCropView();
    if (changed) saveState();
  };
  layer.addEventListener('pointerup', finishCropInteraction);
  layer.addEventListener('pointercancel', finishCropInteraction);

  let isPanning = false;
  let panStartX = 0;
  let panStartY = 0;
  let panOriginX = 0;
  let panOriginY = 0;

  wrap.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || S.speed) return;
    if (S.cropping || S.activePane === 'maskPane' || S.activePane === 'healPane') return;
    if (S.wbPick || S.pointColorPick || S.maskColorPick) return;
    if (e.target.closest('#cropLayer, #editOverlay, .cmp-bar, #compareSnap')) return;
    if (S.zoom <= 1 && S.zoomMode === 'fit') return;
    stopZoomMotion();
    isPanning = true;
    panStartX = e.clientX;
    panStartY = e.clientY;
    panOriginX = S.panX;
    panOriginY = S.panY;
    $('cmp').classList.add('is-panning');
    try { wrap.setPointerCapture(e.pointerId); } catch {}
  });

  wrap.addEventListener('pointermove', (e) => {
    if (!isPanning) return;
    S.panX = panOriginX + (e.clientX - panStartX);
    S.panY = panOriginY + (e.clientY - panStartY);
    applyView();
  });

  const finishPan = (e) => {
    if (!isPanning) return;
    isPanning = false;
    $('cmp').classList.remove('is-panning');
    try { wrap.releasePointerCapture(e.pointerId); } catch {}
  };
  wrap.addEventListener('pointerup', finishPan);
  wrap.addEventListener('pointercancel', finishPan);

  wrap.addEventListener('dblclick', (e) => {
    if (S.cropping || S.activePane === 'maskPane' || S.activePane === 'healPane') return;
    if (S.wbPick || S.pointColorPick || S.maskColorPick) return;
    if (e.target.closest('#cropLayer, #editOverlay, .cmp-bar, #compareSnap')) return;
    toggleActualZoomAt(e.clientX, e.clientY);
  });
}());

document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || document.querySelector('.modal-backdrop.on')) return;
  const tag = e.target.tagName;
  if (tag === 'INPUT' && ['range', 'number'].includes(e.target.type) &&
      PHOTO_TOOL_PANES.includes(S.activePane) && e.target.closest('.panel-pane.on') &&
      ['Enter', 'Escape'].includes(e.key)) {
    e.preventDefault(); e.target.blur();
    if (e.key === 'Escape' && S.activePane === 'cropPane') cancelCropSession();
    else exitPhotoTool();
    return;
  }
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.target.isContentEditable) return;
  const meta = e.metaKey || e.ctrlKey;
  if (meta && e.key.toLowerCase() === 'a') {
    e.preventDefault(); void setAllPhotoSelection(!e.shiftKey); return;
  }
  if (cur() && S.editingName !== cur().name &&
      !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
  if (meta && e.key.toLowerCase() === 'z') {
    e.preventDefault(); e.shiftKey ? redo() : undo(); return;
  }
  if (meta && e.shiftKey && e.key.toLowerCase() === 'c') {
    e.preventDefault(); $('copyBtn').click(); return;
  }
  if (meta && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'c') {
    e.preventDefault(); void PHOTO_CLIPBOARD.copy(); return;
  }
  if (meta && e.shiftKey && e.key.toLowerCase() === 'v') {
    e.preventDefault(); $('pasteBtn').click(); return;
  }
  if (meta && e.altKey && e.key.toLowerCase() === 'v') {
    e.preventDefault(); $('previousBtn')?.click(); return;
  }
  if (meta && e.shiftKey && e.key.toLowerCase() === 'e') {
    e.preventDefault(); $('exportBtn').click(); return;
  }
  if (meta && !e.shiftKey && e.key.toLowerCase() === 'e') {
    e.preventDefault(); $('editExternalOpen').click(); return;
  }
  if (meta && !e.shiftKey && e.key.toLowerCase() === 'f') {
    e.preventDefault(); $('search').focus(); $('search').select(); return;
  }
  if (meta && (e.key === 'Backspace' || e.key === 'Delete')) {
    e.preventDefault(); trashRejected(); return;
  }
  if (meta) return;
  if (e.shiftKey && !e.altKey && e.key.toLowerCase() === 'e') {
    e.preventDefault();
    openExportModal();
    return;
  }

  if (e.key === 'Escape' && $('exportDialog')?.classList.contains('on')) {
    e.preventDefault();
    closeExportModal();
    return;
  }

  if (e.code === 'Space' && S.viewMode === 'detail' && cur() &&
      !(SURVEY && SURVEY.isOpen) &&
      (e.target === document.body || e.target.closest('#editor'))) {
    e.preventDefault();
    toggleActualZoom();
    return;
  }

  const k = e.key.toLowerCase();
  if (k === 'j' && !e.shiftKey && !e.altKey && !$('clipBtn')?.disabled) {
    e.preventDefault();
    $('clipBtn')?.click();
    return;
  }
  if (S.speed && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    adjustSpeed(e.key === 'ArrowRight' ? 1 : -1, e);
    e.preventDefault();
    return;
  }
  const speedControl = KEYS.speed?.[k];
  if (speedControl && !e.altKey && !e.shiftKey && !e.repeat && !S.speed &&
      $('speedKeysEnabled')?.checked !== false && cur() && S.viewMode === 'detail') {
    S.speed = { key: k, control: speedControl, used: false,
      undoCaptured: false, drag: null };
    showSpeedHud(speedControl);
    e.preventDefault();
    return;
  }
  if (S.speed?.key === k) { e.preventDefault(); return; }

  if (e.key === 'Escape' && SURVEY && SURVEY.isOpen) {
    e.preventDefault();
    SURVEY.close();
    return;
  }
  if ((S.compareActive || PHOTO_TOOL_PANES.includes(S.activePane)) &&
      (e.key === 'Enter' || e.key === 'Escape')) {
    e.preventDefault();
    if (e.key === 'Escape' && S.activePane === 'cropPane' && !S.compareActive) cancelCropSession();
    else exitPhotoTool();
    return;
  }
  if (S.cropping && S.crop && e.key.startsWith('Arrow')) {
    const layerRect = $('cropLayer').getBoundingClientRect();
    const amount = e.shiftKey ? 10 : 1;
    const dx = e.key === 'ArrowLeft' ? -amount / layerRect.width : e.key === 'ArrowRight' ? amount / layerRect.width : 0;
    const dy = e.key === 'ArrowUp' ? -amount / layerRect.height : e.key === 'ArrowDown' ? amount / layerRect.height : 0;
    pushUndo();
    S.crop = {
      ...S.crop,
      x: clamp(S.crop.x + dx, 0, 1 - S.crop.w),
      y: clamp(S.crop.y + dy, 0, 1 - S.crop.h),
    };
    applyCropVisual();
    saveState();
    e.preventDefault();
    return;
  }

  if (k === 'm') { e.target.blur?.(); selectPhotoTool('maskPane'); }
  else if (k === 'q') { e.target.blur?.(); selectPhotoTool('healPane'); }
  else if (k === 'o' && S.activePane === 'maskPane' && selectedMask()) {
    $('maskShowOverlay').checked = !$('maskShowOverlay').checked;
    drawEditOverlay();
  }
  else if (k === 'h' && (S.activePane === 'maskPane' || S.activePane === 'healPane')) {
    S.localPinsVisible = !S.localPinsVisible;
    drawEditOverlay();
    toast(S.localPinsVisible ? tr("Edit pins shown") : tr("Edit pins hidden"));
  }
  else if ((e.key === '[' || e.key === ']') &&
      (S.activePane === 'maskPane' || S.activePane === 'healPane')) {
    const direction = e.key === ']' ? 1 : -1;
    if (S.activePane === 'maskPane' && selectedMask()) {
      pushUndo();
      if (e.shiftKey) S.brushFeather = clamp(S.brushFeather + direction * 0.05, 0, 1);
      else S.brushSize = clamp(S.brushSize + direction * 0.01, 0.005, 0.3);
      syncMaskPanel(); drawEditOverlay(); saveState();
    } else {
      const spot = selectedHeal();
      if (spot) pushUndo();
      const brush = spot || S.healBrush;
      if (e.shiftKey) brush.feather = clamp(brush.feather + direction * 0.05, 0, 1);
      else brush.radius = clamp(brush.radius + direction * 0.01, 0.005, 0.25);
      syncHealPanel(); drawEditOverlay();
      if (spot) { saveState(); refreshBaseEdits(); }
    }
  }
  else if ((e.key === 'Backspace' || e.key === 'Delete') && S.activePane === 'maskPane') deleteMask();
  else if ((e.key === 'Backspace' || e.key === 'Delete') && S.activePane === 'healPane') deleteHeal();
  else if (e.key === 'ArrowRight') {
    if (SURVEY && SURVEY.isOpen) SURVEY.step(1); else goRelative(1);
  } else if (e.key === 'ArrowLeft') {
    if (SURVEY && SURVEY.isOpen) SURVEY.step(-1); else goRelative(-1);
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    const direction = e.key === 'ArrowDown' ? 1 : -1;
    if (SURVEY && SURVEY.isOpen) SURVEY.stepRow(direction);
    else if (!goGridRow(direction)) return;
  }
  else if (KEYS.pick.includes(k)) setStatus('approved');
  else if (KEYS.reject.includes(k)) setStatus('skipped');
  else if (KEYS.unflag.includes(k)) setStatus('pending');
  else if (k === KEYS.survey) openSurvey('survey');
  else if (k === 'g') setViewMode(e.shiftKey ? 'square' : 'photo');
  else if (k === KEYS.detail) setViewMode('detail');
  else if (k === KEYS.crop) {
    selectPhotoTool('cropPane');
  }
  else if (k === 'b') { if (!S.holdBefore) setBefore(true); }
  else if (e.key === KEYS.compare || (KEYS.compare === 'c' && k === 'c')) {
    setCompareActive(!S.compareActive);
  }
  else if (e.key === '/') { $('search').focus(); }
  else if (e.key >= '0' && e.key <= '5') setRating(+e.key, e.shiftKey);
  else if (LABEL_KEYS[e.key]) setLabel(LABEL_KEYS[e.key]);
  else if (e.key === '=' || e.key === '+') zoomCentre(1.25);
  else if (e.key === '-') zoomCentre(1 / 1.25);
  else if (k === 'f') zoomReset({animate: true});
  else if (e.key === '[') rotate(-90);
  else if (e.key === ']') rotate(90);
  else return;
  e.preventDefault();
});
document.addEventListener('keyup', (e) => {
  const key = e.key.toLowerCase();
  if (finishSpeedKey(key)) { e.preventDefault(); return; }
  if (key === 'b' && S.holdBefore) setBefore(false);
});

/* =========================================================================
   Metadata, presets, tone curve, colour mixer, multi-select, and the
   remaining desktop-style conveniences.
   ========================================================================= */

/* ------------------------------------------------------------- metadata */
const _exifCache = new Map();
function updateLoupeInfoOverlay() {
  const image = cur();
  if (!image || !$('loupeInfoOverlay')) return;
  const exif = S.exif || {};
  $('loupeInfoName').textContent = displayName(image);
  $('loupeInfoMetadata').textContent = [
    exif.Model,
    exif.FocalLength ? `${exif.FocalLength} mm` : '',
    exif.FNumber ? `f/${exif.FNumber}` : '',
    exif.ExposureTime ? `${exif.ExposureTime} s` : '',
    exif.ISO ? `ISO ${exif.ISO}` : '',
  ].filter(Boolean).join('  ·  ');
}
async function showExif(name) {
  const box = $('exif');
  const imageKey = JSON.stringify([name, cur()?.recoverySourceKey || null, cur()?.fileKey || null, cur()?.mtime || null]);
  if (!_exifCache.has(imageKey)) {
    try {
      _exifCache.set(imageKey, await fetch(
        '/api/exif?name=' + encodeURIComponent(name)).then((r) => r.json()));
    } catch { _exifCache.set(imageKey, {}); }
  }
  const e = _exifCache.get(imageKey) || {};
  if (cur()?.name !== name) return;
  if (imageKey !== JSON.stringify([name, cur()?.recoverySourceKey || null, cur()?.fileKey || null, cur()?.mtime || null])) return;
  S.exif = e;
  updateLoupeInfoOverlay();
  const im = cur();
  // These fields include camera orientation and describe decoded pixels. EXIF
  // ImageWidth/ImageHeight may instead describe a RAW's embedded JPEG.
  const width = +(e.SourceWidth || 0);
  const height = +(e.SourceHeight || 0);
  if (im && width > 0 && height > 0 && (im.width !== width || im.height !== height)) {
    im.width = width;
    im.height = height;
    const gridCell = _gridEls.get(im.name);
    if (gridCell) gridCell.style.setProperty('--photo-aspect-ratio', `${width} / ${height}`);
    layoutPhotoGrid();
    onViewportResize();
    scheduleAutomaticPreview();
    if (S.zoomMode === '100' && S.viewMode === 'detail') renderFilm(0);
  }
  const row = (k, v) => v ? `<div class="r"><span>${i18nHTML(k)}</span><b>${i18nHTML(v)}</b></div>` : '';
  const shutter = e.ExposureTime ? e.ExposureTime + ' s' : '';
  box.innerHTML =
    row(tr("File"), name) +
    row(tr("Camera"), e.Model) +
    row(tr("Lens"), e.LensModel || e.LensID) +
    row(tr("Focal"), e.FocalLength) +
    row(tr("Aperture"), e.FNumber ? 'f/' + e.FNumber : '') +
    row(tr("Shutter"), shutter) +
    row(tr("ISO"), e.ISO) +
    row(tr("Size"), (width && height) ? `${width}×${height}` : '') +
    row(tr("On disk"), e.FileSize) +
    row(tr("Taken"), e.DateTimeOriginal) || '—';
  if (e.DateTimeOriginal) {
    if (im) im.date = e.DateTimeOriginal;
  }
  broadcastToLoupe(im);
}

/* ------------------------------------------------------------ keywords */
function renderKeywords() {
  KEYWORD_BATCH?.sync();
  const box = $('keywordList');
  box.replaceChildren();
  const im = cur();
  if (!im) return;
  const suggestions = $('keywordSuggestions');
  if (suggestions) {
    const existing = [...suggestions.options].map((option) => option.value);
    const values = [...new Set([...existing,
      ...S.images.flatMap((image) => image.keywords || [])])]
      .sort((a, b) => a.localeCompare(b)).slice(0, 500);
    suggestions.replaceChildren(...values.map((value) => new Option(value, value)));
  }
  for (const keyword of im.keywords || []) {
    const chip = document.createElement('span');
    chip.className = 'keyword-chip';
    const label = document.createElement('span');
    label.textContent = keyword;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.title = tr("Remove {keyword}", {keyword: keyword});
    remove.setAttribute('aria-label', tr("Remove {keyword}", {keyword: keyword}));
    remove.textContent = '×';
    remove.onclick = () => {
      im.keywords = (im.keywords || []).filter(k => k !== keyword);
      for (const image of linkedMetadataTargets([im])) image.keywords = [...im.keywords];
      saveState(true); renderKeywords();
      refreshFilteredView();
    };
    chip.append(label, remove);
    box.appendChild(chip);
  }
}

function addKeyword() {
  const im = cur();
  const raw = $('keywordInput').value;
  if (!im || !raw.trim()) return;
  const separator = APP_PREFS.keywordSeparators === 'comma-semicolon'
    ? /[,;]/ : /,/;
  const values = raw.split(separator).map((value) => value.trim().replace(/\s+/g, ' '))
    .filter(Boolean).slice(0, 100);
  im.keywords = im.keywords || [];
  let changed = false;
  for (const value of values) {
    if (im.keywords.some((keyword) =>
      keyword.toLocaleLowerCase() === value.toLocaleLowerCase())) continue;
    im.keywords.push(value);
    changed = true;
  }
  if (changed) {
    im.keywords.sort((a, b) => a.localeCompare(b));
    for (const image of linkedMetadataTargets([im])) {
      if (image !== im) image.keywords = [...im.keywords];
    }
    saveState(true);
  }
  $('keywordInput').value = '';
  renderKeywords();
  refreshFilteredView();
}
$('keywordAdd').onclick = addKeyword;
$('keywordInput').addEventListener('keydown', (e) => {
  const separator = e.key === ',' ||
    (e.key === ';' && APP_PREFS.keywordSeparators === 'comma-semicolon');
  if (e.key === 'Enter' || separator) { e.preventDefault(); addKeyword(); }
});

function applyKeywordChanges(changes) {
  for (const item of changes) {
    const image = S.images.find(image => image.name === item.name);
    if (!image) continue;
    image.keywords = [...item.keywords];
    if (image.stateLoadEdits) image.stateLoadEdits.keywords = [...item.keywords];
    if (editSaveQueue.getPending(image.name)) enqueuePhotoPatch(image, {keywords: item.keywords});
  }
  renderKeywords(); refreshFilteredView();
}

KEYWORD_BATCH = installKeywordBatch({
  el: $, post: api, toast, enabled: () => S.catalogEnabled,
  names: () => [...S.msel], flush: flushEditSaves,
  values: () => $('keywordInput').value.split(APP_PREFS.keywordSeparators === 'comma-semicolon' ? /[,;]/ : /,/)
    .map(value => value.trim()).filter(Boolean),
  apply: changes => {
    applyKeywordChanges(changes);
    METADATA?.refreshKeywordTree();
  },
});

/* ------------------------------------------------------------- versions */
function renderVersions() {
  const box = $('versionList');
  box.replaceChildren();
  const versions = cur() ? (cur().versions || []) : [];
  if (!versions.length) {
    const empty = document.createElement('div');
    empty.className = 'version-empty';
    empty.textContent = tr("No named versions yet");
    box.appendChild(empty);
    return;
  }
  for (const version of versions) {
    const row = document.createElement('div');
    row.className = 'version-row';
    const apply = document.createElement('button');
    apply.className = 'version-main';
    const name = document.createElement('strong');
    name.textContent = version.name;
    const when = document.createElement('small');
    const date = new Date(version.created || 0);
    when.textContent = Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
    apply.append(name, when);
    apply.onclick = () => {
      pushUndo();
      S.params = normalizeFilmParams(cloneValue(version.params || {}));
      S.grade = { ...GRADE_DEFAULTS, ...cloneValue(version.grade || {}) };
      S.crop = cloneValue(version.crop) || null;
      S.masks = normalizeMasks(cloneValue(version.masks));
      S.heals = normalizeHeals(cloneValue(version.heals));
      S.optics = normalizeOptics(cloneValue(version.optics));
      S.maskTextureDirty = true;
      S.selectedMaskId = S.masks[0]?.id || null; S.selectedHealId = S.heals[0]?.id || null;
      syncControls(); syncGrade(); syncCurveFromGrade(); syncHsl();
      syncMaskPanel(); syncHealPanel(); syncOpticsPanel();
      drawGrade(); applyCropVisual(); saveState(true); renderFilm(0);
      toast(tr("Applied {versionName}", {versionName: version.name}));
    };
    const del = document.createElement('button');
    del.className = 'version-delete';
    del.title = tr("Delete {versionName}", {versionName: version.name});
    del.setAttribute('aria-label', tr("Delete {versionName}", {versionName: version.name}));
    del.textContent = '×';
    del.onclick = () => {
      const im = cur();
      im.versions = im.versions.filter((v) => v.id !== version.id);
      saveState(true); renderVersions(); toast(tr("Version deleted"));
    };
    row.append(apply, del);
    box.appendChild(row);
  }
}

$('versionCreate').onclick = async () => {
  const im = cur();
  if (!im) return;
  const proposed = tr("Version {value}", {value: (im.versions || []).length + 1});
  const name = await askName(tr("Create version"), proposed);
  if (!name || !name.trim()) return;
  readControls();
  const version = {
    id: (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`,
    name: name.trim(), created: new Date().toISOString(),
    params: cloneValue(S.params), grade: cloneValue(S.grade), crop: cloneValue(S.crop),
    masks: cloneValue(serializableMasks()), heals: cloneValue(S.heals), optics: cloneValue(S.optics),
  };
  im.versions = [version, ...(im.versions || [])].slice(0, 50);
  saveState(true); renderVersions(); toast(tr("Version created"));
};

/* -------------------------------------------------------------- presets */
let PRESETS = [];
let LAST_PRESET_APPLICATION = null;
let COMMUNITY_PRESETS = {};
let PRESETS_READY = Promise.resolve();
const COMMUNITY_RECIPES = new Map();
const PRESET_SOURCE_LABELS = {
  'lighttable': 'LightTable', lightroom: 'Lightroom / Camera Raw',
  'capture-one': 'Capture One',
};

function selectedPreset() {
  return PRESETS.find((preset) => presetKey(preset) === $('presetList').value) || null;
}

function presetHasApplicableSettings(preset) {
  return hasApplicablePresetSettings(preset, normalizeOptics, OPTICS_DEFAULTS);
}

function renderPresetSummary(resetChoices = false) {
  const box = $('presetSummary');
  const preset = selectedPreset();
  box.replaceChildren();
  $('presetApply').disabled = !cur() || !presetHasApplicableSettings(preset);
  $('presetReplace').disabled = $('presetApply').disabled;
  $('presetExport').disabled = !preset;
  $('presetDel').disabled = !preset || preset.collection === 'builtin';
  $('presetReplace').hidden = preset?.scope === 'look';
  if (!preset) {
    box.textContent = tr("Choose a preset to see its source and conversion coverage.");
    return;
  }
  if (resetChoices) {
    $('presetFilmOff').checked = false;
  }
  const source = PRESET_SOURCE_LABELS[preset.source] || preset.source || 'LightTable';
  const mapped = preset.conversion?.mapped ?? (preset.includedGrade || []).length;
  const ignored = preset.conversion?.ignored || [];
  const title = document.createElement('strong');
  title.textContent = trn("{source} · {count} mapped setting", "{source} · {count} mapped settings", mapped, {source: source, mapped: mapped});
  const detail = document.createElement('div');
  detail.textContent = preset.scope === 'look'
    ? tr('Film {mode}. Keeps exposure, white balance, crop and photo corrections.', {mode: preset.filmMode === 'off' ? tr('off') : preset.filmMode === 'apply' ? tr('applied') : tr('unchanged')})
    : preset.includeFilm ? tr('Includes the Film profile and physical stages.')
      : tr('Portable edit preset; Film can be kept on or turned off below.');
  box.append(title, detail);
  if (preset.recommendedFilmOff) {
    const recommendation = document.createElement('div');
    recommendation.textContent = tr("Turning Film off may give a closer match to this imported look.");
    box.append(recommendation);
  }
  if (!presetHasApplicableSettings(preset)) {
    const warning = document.createElement('div');
    warning.className = 'preset-warning';
    warning.textContent = tr("No compatible adjustments were found; this preset cannot be applied.");
    box.appendChild(warning);
  }
  if (ignored.length) {
    const warning = document.createElement('div');
    warning.className = 'preset-warning';
    const details = ignored.slice(0, 4).join(', ') + (ignored.length > 4 ? '…' : '');
    warning.textContent = trn('{count} unsupported setting skipped: {details}',
      '{count} unsupported settings skipped: {details}', ignored.length, {details});
    box.appendChild(warning);
  }
}

async function loadPresets(select) {
  PRESET_BROWSER?.refresh({ loading: true });
  try {
    const response = await fetch('/api/presets');
    const result = await response.json();
    if (!response.ok || !Array.isArray(result)) throw new Error(tr('Presets unavailable'));
    PRESETS = result;
  } catch { toast(tr('Could not load presets. Your saved presets are kept.')); }
  const migrated = migratePresetFavorites(Array.isArray(APP_PREFS.presetFavorites) ? APP_PREFS.presetFavorites : [], PRESETS);
  if (JSON.stringify(migrated) !== JSON.stringify(APP_PREFS.presetFavorites || [])) {
    APP_PREFS.presetFavorites = migrated; savePrefs();
  }
  const sel = $('presetList');
  const keep = select ?? sel.value;
  sel.replaceChildren();
  const empty = document.createElement('option');
  empty.value = ''; empty.textContent = tr("— none —");
  sel.appendChild(empty);
  for (const preset of PRESETS) {
    const option = document.createElement('option');
    option.value = presetKey(preset);
    option.textContent = preset.collection === 'builtin' ? tr('{name} · Built-in', {name: preset.name}) : preset.name;
    sel.appendChild(option);
  }
  if (keep) sel.value = presetKey(PRESETS.find((preset) => presetKey(preset) === keep || preset.name === keep) || { name: keep });
  renderPresetSummary(true);
  PRESET_BROWSER?.refresh({ loading: false });
}
$('presetList').addEventListener('change', () => {
  renderPresetSummary(true);
  PRESET_BROWSER?.select();
});

let presetAmountGesture = null;
function currentPresetAdjustment() {
  return reconcilePresetAdjustment(S.preset, presetEditState(S));
}

function presentPresetAdjustment(adjustment, immediate = true) {
  const previousFilm = filmRenderFingerprint(), previousBase = baseEditsFingerprint();
  S.preset = adjustment;
  Object.assign(S, blendPresetState(adjustment.base, adjustment.target,
    adjustment.enabled ? adjustment.amount : 0));
  S.maskTextureDirty = true;
  S.selectedMaskId = S.masks[0]?.id || null; S.selectedHealId = S.heals[0]?.id || null;
  syncControls(); syncGrade(); syncCurveFromGrade(); syncHsl();
  syncMaskPanel(); syncHealPanel(); syncOpticsPanel();
  drawGrade(); saveState(immediate);
  if (filmRenderFingerprint() !== previousFilm) renderFilm(immediate ? 0 : 120);
  else if (baseEditsFingerprint() !== previousBase) refreshBaseEdits();
}

function toggleBrowserPreset(preset, photo, identity = presetKey(preset)) {
  if (!photo?.name || cur()?.name !== photo.name || S.editingName !== photo.name) return;
  readControls();
  const previous = currentPresetAdjustment();
  if (previous?.id !== identity && !presetHasApplicableSettings(preset)) return;
  pushUndo(); presetAmountGesture = null;
  let adjustment;
  const controlled = typeof presetControlledSettings === 'function' ? presetControlledSettings(preset) : null;
  if (previous?.id === identity) {
    adjustment = { ...previous, enabled: !previous.enabled,
      controlled: previous.controlled || controlled };
    if (adjustment.enabled && adjustment.amount === 0) adjustment.amount = 100;
  } else {
    const base = previous ? previous.base : presetEditState(S);
    const target = composePresetState(base, preset, {
      normalizeFilmParams, mergeFilmParams, createId: editId,
    });
    adjustment = { id: identity, name: preset.name, amount: 100, enabled: true,
      base: cloneValue(base), target: presetEditState(target),
      controlled };
  }
  LAST_PRESET_APPLICATION = null;
  presentPresetAdjustment(adjustment);
  return true;
}

function changePresetAmount(id, photoName, amount, commit = false) {
  if (cur()?.name !== photoName || S.editingName !== photoName) return false;
  const adjustment = currentPresetAdjustment();
  if (!adjustment || adjustment.id !== id) return false;
  amount = Math.max(0, Math.min(100, Math.round(Number(amount) || 0)));
  if (amount !== adjustment.amount || adjustment.enabled !== (amount > 0)) {
    if (presetAmountGesture !== photoName) { pushUndo(); presetAmountGesture = photoName; }
    markContinuousInput();
    presentPresetAdjustment({ ...adjustment, amount, enabled: amount > 0 }, false);
  }
  if (commit) { presetAmountGesture = null; saveState(true); }
  return true;
}

function presetPhotoSnapshot() {
  const image = cur();
  if (!image || !S.params || S.editingName !== image.name) return null;
  return {
    name: image.name, label: displayName(image), engine: $('engine').value,
    state: JSON.parse(snapshot()),
  };
}

function presetApplicationMatches(state, preset, options, photoName) {
  return LAST_PRESET_APPLICATION?.name === photoName &&
    LAST_PRESET_APPLICATION.preset === JSON.stringify(preset) &&
    LAST_PRESET_APPLICATION.replace === !!options.replace &&
    LAST_PRESET_APPLICATION.filmOff === !!options.filmOff &&
    LAST_PRESET_APPLICATION.state === JSON.stringify(state);
}

function stateWithPreset(state, preset, options = {}, photoName) {
  // Consecutive public looks use the edit before the first application.
  // The saved Amount baseline also survives navigation and unrelated edits.
  // Management actions retain their explicit layering/replacement behavior.
  const adjustment = reconcilePresetAdjustment(state.preset, presetEditState(state));
  if (adjustment) return composePresetState({ ...state, ...adjustment.base }, preset, {
    ...options, normalizeFilmParams, mergeFilmParams, createId: editId,
  });
  if (presetApplicationMatches(state, preset, options, photoName)) return cloneValue(state);
  const base = preset.scope === 'look' && LAST_PRESET_APPLICATION?.scope === 'look' &&
    LAST_PRESET_APPLICATION.name === photoName &&
    LAST_PRESET_APPLICATION.state === JSON.stringify(state)
    ? LAST_PRESET_APPLICATION.base : state;
  return composePresetState(base, preset, {
    ...options, normalizeFilmParams, mergeFilmParams, createId: editId,
  });
}

function applyPreset(preset, photo, options = {}) {
  if (!photo?.name || cur()?.name !== photo.name || S.editingName !== photo.name) return toast(tr("Wait for the photo to finish loading before applying a preset"));
  if (!presetHasApplicableSettings(preset)) return toast(tr("No compatible settings to apply"));
  readControls();
  const currentState = JSON.parse(snapshot());
  if (presetApplicationMatches(currentState, preset, options, photo.name)) return toast(tr('{presetName} is already applied', {presetName: preset.name}));
  const base = preset.scope === 'look' && LAST_PRESET_APPLICATION?.scope === 'look' &&
    LAST_PRESET_APPLICATION.name === photo.name && LAST_PRESET_APPLICATION.state === JSON.stringify(currentState)
    ? LAST_PRESET_APPLICATION.base : currentState;
  const next = stateWithPreset(currentState, preset, options, photo.name);
  pushUndo();
  S.preset = null;
  S.params = next.params; S.grade = next.grade;
  S.masks = next.masks; S.heals = next.heals; S.optics = next.optics;
  S.maskTextureDirty = true;
  S.selectedMaskId = S.masks[0]?.id || null; S.selectedHealId = S.heals[0]?.id || null;
  syncControls(); syncGrade(); syncCurveFromGrade(); syncHsl();
  syncMaskPanel(); syncHealPanel(); syncOpticsPanel();
  drawGrade(); saveState(true); renderFilm(0);
  LAST_PRESET_APPLICATION = {
    name: photo.name, preset: JSON.stringify(preset), state: snapshot(),
    scope: preset.scope, base: cloneValue(base),
    replace: !!options.replace, filmOff: !!options.filmOff,
  };
  PRESET_BROWSER?.refresh();
  toast(options.replace ? tr('Replaced adjustments with {name}', {name: preset.name}) : tr('Applied {name}', {name: preset.name}), {
    label: tr("Undo"), run() {
      if (cur()?.name === photo.name && S.editingName === photo.name) undo();
      else toast(tr("Return to the edited photo to undo its preset"));
    },
  });
}

PRESET_BROWSER = createPresetBrowser({
  container: $('presetBrowser'),
  getPresets: () => PRESETS,
  getPhoto: presetPhotoSnapshot,
  getSelectedName: () => $('presetList').value,
  canApply: presetHasApplicableSettings,
  getFavorites: () => Array.isArray(APP_PREFS.presetFavorites) ? APP_PREFS.presetFavorites : [],
  onFavoritesChange(ids) { APP_PREFS.presetFavorites = ids; savePrefs(); },
  getHidden: () => Array.isArray(APP_PREFS.hiddenBuiltinPresets) ? APP_PREFS.hiddenBuiltinPresets : [],
  onHiddenChange(ids) { APP_PREFS.hiddenBuiltinPresets = ids; savePrefs(); },
  getOrganization: () => APP_PREFS.presetPacks || {},
  onOrganizationChange(value) { APP_PREFS.presetPacks = value; savePrefs(); },
  requestPackName: name => askName(name ? tr('Rename pack') : tr('New pack…'), name),
  onSelect(preset) { $('presetList').value = presetKey(preset); renderPresetSummary(true); },
  onApply: (preset, photo, identity) => toggleBrowserPreset(preset, photo, identity),
  getAdjustment: currentPresetAdjustment,
  onAmount: changePresetAmount,
  getCommunity: () => COMMUNITY_PRESETS,
  async loadCommunity(refresh) {
    try {
      const response = await fetch(`/api/presets/community${refresh ? '?refresh=1' : ''}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || tr('Community is unavailable'));
      COMMUNITY_PRESETS = result;
    } catch (error) {
      COMMUNITY_PRESETS = { ...COMMUNITY_PRESETS, offline: true, error: error.message };
    }
  },
  async getRecipe(preset, { signal } = {}) {
    if (preset.collection !== 'community') return preset;
    const key = `${preset.id}@${preset.version}:${preset.file?.sha256 || ''}`;
    if (COMMUNITY_RECIPES.has(key)) return COMMUNITY_RECIPES.get(key);
    const response = await fetch(`/api/presets/community/recipe?id=${encodeURIComponent(preset.id)}&version=${encodeURIComponent(preset.version)}`, { signal });
    const result = await response.json();
    if (!response.ok || !result.preset) throw new Error(result.error || tr('Recipe is unavailable'));
    COMMUNITY_RECIPES.set(key, result.preset);
    return result.preset;
  },
  async onInstall(preset) {
    const result = await api('/api/presets/community/install', { id: preset.id, version: preset.version });
    if (result.error) throw new Error(result.error);
    await loadPresets(result.installedId);
    toast(tr('Preset saved to Yours'));
  },
  async onDuplicate(preset) {
    const name = await askName(tr('Save a preset copy'), tr('{name} copy', {name: preset.name}));
    if (!name?.trim()) return;
    const result = await api('/api/presets', {
      ...preset, action: 'save', id: undefined, collection: undefined,
      name: name.trim(), version: '1.0.0', parentId: preset.id,
    });
    if (result.error) throw new Error(result.error);
    await loadPresets(name.trim());
    toast(tr('Copy saved to Yours'));
  },
  async getSubmission(preset, { signal } = {}) {
    const response = await fetch('/api/presets/submission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
      body: JSON.stringify({ id: preset.id }),
    });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || tr('Could not inspect submission settings'));
    return result;
  },
  async onDownloadExample(preset, value) {
    if (!(value instanceof Blob)) throw new Error(tr('Example image is unavailable'));
    const stem = preset.name.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 100) || 'preset';
    downloadPresetFile({ filename: `${stem}-example.jpg`, contentType: 'image/jpeg',
      encoding: 'base64', content: bytesToBase64(await value.arrayBuffer()) });
  },
  async onSubmit(preset) {
    const result = await api('/api/presets/submission', { id: preset.id, examples: true });
    if (result.error) throw new Error(result.error);
    downloadPresetFile(result);
    return result;
  },
  managementSection: $('presetManage'),
  async getPreview(preset, photo, { signal, width = 320 }) {
    const response = await fetch('/api/render/file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
      body: JSON.stringify({
        name: photo.name, state: preset ? stateWithPreset(photo.state, preset, {}, photo.name) :
          (LAST_PRESET_APPLICATION?.scope === 'look' && LAST_PRESET_APPLICATION.name === photo.name &&
            LAST_PRESET_APPLICATION.state === JSON.stringify(photo.state) ? LAST_PRESET_APPLICATION.base : photo.state),
        w: width, format: 'jpeg', engine: photo.engine, client: 'preset-browser',
      }),
    });
    if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) throw new Error(tr("Preview unavailable"));
    return response.blob();
  },
});
PRESET_BROWSER.setActive(S.activePane === 'presetsPane');
$('presetSaveScope').onchange = () => { $('presetSaveOptions').hidden = $('presetSaveScope').value === 'look'; };
$('presetSaveScope').onchange();

$('presetSave').onclick = async () => {
  if (!cur()) return toast(tr("Select a photo first"));
  const name = await askName(tr("Save preset"));
  if (!name) return;
  readControls();
  const result = await api('/api/presets', {
    action: 'save', name, params: S.params, grade: S.grade,
    scope: $('presetSaveScope').value,
    filmMode: $('presetIncludeFilm').checked ? (S.params.profile_enabled === false ? 'off' : 'on') : 'preserve',
    masks: serializableMasks(), heals: S.heals, optics: S.optics,
    presetType: $('presetSaveType').value,
    includeFilm: $('presetIncludeFilm').checked,
  });
  if (result.error) return toast(result.error);
  PRESETS = result;
  await loadPresets(name);
  toast(tr("Saved preset"));
};
$('presetApply').onclick = () => {
  const preset = selectedPreset();
  if (!preset) return toast(tr("Pick a preset"));
  applyPreset(preset, presetPhotoSnapshot(), { filmOff: $('presetFilmOff').checked });
};
$('presetReplace').onclick = () => {
  const preset = selectedPreset(), photo = presetPhotoSnapshot();
  if (!preset || !photo) return;
  applyPreset(preset, photo, { replace: true, filmOff: $('presetFilmOff').checked });
};
$('presetDel').onclick = async () => {
  const preset = selectedPreset();
  if (!preset || preset.collection === 'builtin') return;
  const result = await api('/api/presets', { action: 'delete', id: preset.id, name: preset.name });
  if (result.error) return toast(result.error);
  PRESETS = result;
  await loadPresets('');
  toast(tr("Deleted"));
};

async function importPresetUploads(uploads, nativeFailures = []) {
  if (!uploads.length) {
    if (nativeFailures.length) toast(trn("{count} preset file could not be read", "{count} preset files could not be read", nativeFailures.length, {nativeFailuresLength: nativeFailures.length}));
    return;
  }
  $('presetImport').disabled = true;
  try {
    const result = await api('/api/presets/import', { files: uploads });
    PRESETS = result.presets || PRESETS;
    await loadPresets();
    const failed = (result.failures?.length || 0) + nativeFailures.length;
    toast(failed ? tr("Imported {value}; {failed} could not be converted", {value: (result.imported || 0), failed: failed}) : trn("Imported {value} preset", "Imported {value} presets", result.imported, {value: (result.imported || 0)}));
  } catch {
    toast(tr("Preset import failed"));
  } finally {
    $('presetFiles').value = '';
    $('presetImport').disabled = false;
  }
}

$('presetImport').onclick = () => {
  const bridge = window.webkit?.messageHandlers?.lightTable;
  if (bridge) bridge.postMessage({ action: 'importPresets' });
  else $('presetFiles').click();
};
$('presetFiles').addEventListener('change', async () => {
  const files = [...$('presetFiles').files];
  if (!files.length) return;
  const uploads = [];
  for (const file of files) {
    if (file.size > 25 * 1024 * 1024) {
      uploads.push({ name: file.name, text: '' });
    } else {
      uploads.push({ name: file.name, base64: bytesToBase64(await file.arrayBuffer()) });
    }
  }
  await importPresetUploads(uploads);
});

function downloadPresetFile(result) {
  const bridge = nativeBridge();
  if (bridge) {
    bridge.postMessage({
      action: 'savePreset', filename: result.filename,
      content: result.content, encoding: result.encoding,
    });
    return true;
  }
  const content = result.encoding === 'base64'
    ? Uint8Array.from(atob(result.content), (character) => character.charCodeAt(0)) : result.content;
  const blob = new Blob([content], { type: result.contentType || 'text/plain' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = result.filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return false;
}

$('presetExport').onclick = async () => {
  const preset = selectedPreset();
  if (!preset) return toast(tr("Pick a preset"));
  const result = await api('/api/presets/export', {
    id: preset.id, name: preset.name, format: $('presetExportFormat').value,
  });
  if (result.error) return toast(result.error);
  const awaitingNativeSave = downloadPresetFile(result);
  if (!awaitingNativeSave) toast(tr("Exported {resultFilename}", {resultFilename: result.filename}));
};

/* ----------------------------------------------------------- tone curve */
// Control points live in S.curve[ch]; the 256-entry table they generate is
// what both the shader and the exporter consume.
S.curve = { L: [[0, 0], [1, 1]], R: [[0, 0], [1, 1]], G: [[0, 0], [1, 1]], B: [[0, 0], [1, 1]] };
S.curveCh = 'L';
S.curveMode = 'point';
S.paramCurve = { highlights: 0, lights: 0, darks: 0, shadows: 0, splitSD: 0.25, splitDL: 0.50, splitLH: 0.75 };

function readParamCurveControls() {
  S.paramCurve = {
    highlights: +$('paramCurveHighlights')?.value || 0,
    lights: +$('paramCurveLights')?.value || 0,
    darks: +$('paramCurveDarks')?.value || 0,
    shadows: +$('paramCurveShadows')?.value || 0,
    splitSD: (+$('paramCurveSplitSD')?.value || 25) / 100,
    splitDL: (+$('paramCurveSplitDL')?.value || 50) / 100,
    splitLH: (+$('paramCurveSplitLH')?.value || 75) / 100,
  };
  if ($('paramCurveHighlightsV')) $('paramCurveHighlightsV').textContent = String(S.paramCurve.highlights);
  if ($('paramCurveLightsV')) $('paramCurveLightsV').textContent = String(S.paramCurve.lights);
  if ($('paramCurveDarksV')) $('paramCurveDarksV').textContent = String(S.paramCurve.darks);
  if ($('paramCurveShadowsV')) $('paramCurveShadowsV').textContent = String(S.paramCurve.shadows);
  if ($('paramCurveSplitSDV')) $('paramCurveSplitSDV').textContent = Math.round(S.paramCurve.splitSD * 100) + '%';
  if ($('paramCurveSplitDLV')) $('paramCurveSplitDLV').textContent = Math.round(S.paramCurve.splitDL * 100) + '%';
  if ($('paramCurveSplitLHV')) $('paramCurveSplitLHV').textContent = Math.round(S.paramCurve.splitLH * 100) + '%';
}

function syncParamCurveControls() {
  const p = S.paramCurve || { highlights: 0, lights: 0, darks: 0, shadows: 0, splitSD: 0.25, splitDL: 0.50, splitLH: 0.75 };
  if ($('paramCurveHighlights')) $('paramCurveHighlights').value = String(p.highlights || 0);
  if ($('paramCurveLights')) $('paramCurveLights').value = String(p.lights || 0);
  if ($('paramCurveDarks')) $('paramCurveDarks').value = String(p.darks || 0);
  if ($('paramCurveShadows')) $('paramCurveShadows').value = String(p.shadows || 0);
  if ($('paramCurveSplitSD')) $('paramCurveSplitSD').value = String(Math.round((p.splitSD ?? 0.25) * 100));
  if ($('paramCurveSplitDL')) $('paramCurveSplitDL').value = String(Math.round((p.splitDL ?? 0.50) * 100));
  if ($('paramCurveSplitLH')) $('paramCurveSplitLH').value = String(Math.round((p.splitLH ?? 0.75) * 100));
  readParamCurveControls();
}

function drawParamCurve() {
  const cv = $('paramCurveCanvas');
  if (!cv) return;
  const x = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  x.clearRect(0, 0, W, H);
  x.strokeStyle = '#2e2e2e'; x.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    x.beginPath(); x.moveTo(W * i / 4, 0); x.lineTo(W * i / 4, H); x.stroke();
    x.beginPath(); x.moveTo(0, H * i / 4); x.lineTo(W, H * i / 4); x.stroke();
  }
  x.strokeStyle = '#444'; x.lineWidth = 1; x.setLineDash([2, 2]);
  for (const split of [S.paramCurve.splitSD, S.paramCurve.splitDL, S.paramCurve.splitLH]) {
    x.beginPath(); x.moveTo(W * split, 0); x.lineTo(W * split, H); x.stroke();
  }
  x.setLineDash([]);
  const lut = evalParametricLUT(S.paramCurve) || Array.from({length: 256}, (_, i) => i / 255);
  x.strokeStyle = '#e9e9e7';
  x.lineWidth = 1.5; x.beginPath();
  for (let i = 0; i < 256; i++) {
    const px = (i / 255) * W, py = H - lut[i] * H;
    i ? x.lineTo(px, py) : x.moveTo(px, py);
  }
  x.stroke();
}

function commitCurves() {
  if (S.curveMode === 'parametric') {
    S.grade.parametricCurve = {...S.paramCurve};
    const lut = evalParametricLUT(S.paramCurve);
    if (lut) S.grade.curveL = lut;
    else delete S.grade.curveL;
    drawParamCurve();
  } else {
    delete S.grade.parametricCurve;
    for (const [ch, key] of [['L', 'curveL'], ['R', 'curveR'], ['G', 'curveG'], ['B', 'curveB']]) {
      if (isIdentityPoints(S.curve[ch])) delete S.grade[key];
      else S.grade[key] = monotoneLUT(S.curve[ch]);
    }
    drawCurve();
  }
  drawGrade();
}
function syncCurveFromGrade() {
  S.paramCurve = {...(S.grade.parametricCurve || {
    highlights: 0, lights: 0, darks: 0, shadows: 0,
    splitSD: 0.25, splitDL: 0.50, splitLH: 0.75,
  })};
  syncParamCurveControls();
  // Persisted/imported curves are sampled tables. Rebuild representative
  // handles for editing while drawing the exact table until the first edit.
  for (const [ch, key] of [['L', 'curveL'], ['R', 'curveR'], ['G', 'curveG'], ['B', 'curveB']]) {
    const lut = S.grade[key];
    S.curve[ch] = Array.isArray(lut) && lut.length === 256
      ? [0, 32, 64, 96, 128, 160, 192, 224, 255]
        .map((index) => [index / 255, +lut[index]])
      : [[0, 0], [1, 1]];
  }
  drawCurve();
  drawParamCurve();
}
let refreshCurveCursor = () => {};
function drawCurve() {
  refreshCurveCursor();
  const cv = $('curve'), x = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  x.clearRect(0, 0, W, H);
  x.strokeStyle = '#2e2e2e'; x.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    x.beginPath(); x.moveTo(W * i / 4, 0); x.lineTo(W * i / 4, H); x.stroke();
    x.beginPath(); x.moveTo(0, H * i / 4); x.lineTo(W, H * i / 4); x.stroke();
  }
  const gradeKey = { L: 'curveL', R: 'curveR', G: 'curveG', B: 'curveB' }[S.curveCh];
  const stored = S.grade[gradeKey];
  const lut = Array.isArray(stored) && stored.length === 256
    ? stored : monotoneLUT(S.curve[S.curveCh]);
  x.strokeStyle = { L: '#e9e9e7', R: '#f87171', G: '#4ade80', B: '#6b8ff0' }[S.curveCh];
  x.lineWidth = 1.5; x.beginPath();
  for (let i = 0; i < 256; i++) {
    const px = (i / 255) * W, py = H - lut[i] * H;
    i ? x.lineTo(px, py) : x.moveTo(px, py);
  }
  x.stroke();
  x.fillStyle = '#fff';
  for (const [cx, cy] of S.curve[S.curveCh]) {
    x.fillRect(cx * W - 3, H - cy * H - 3, 6, 6);
  }
}
(function curveEvents() {
  const cv = $('curve');
  let drag = -1;
  const at = (e) => {
    const r = cv.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width, 0, 1),
            clamp(1 - (e.clientY - r.top) / r.height, 0, 1)];
  };
  const near = (p) => S.curve[S.curveCh]
    .findIndex(([x, y]) => Math.hypot(x - p[0], y - p[1]) < 0.05);
  cv.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const p = at(e); const i = near(p);
    pushUndo();
    if (i >= 0) drag = i;
    else { S.curve[S.curveCh].push(p); S.curve[S.curveCh].sort((a, b) => a[0] - b[0]); drag = near(p); }
    cv.setPointerCapture(e.pointerId);
    drawCurve(); commitCurves();
  });
  cv.addEventListener('pointermove', (e) => {
    if (drag < 0) return;
    const p = at(e);
    const pts = S.curve[S.curveCh];
    // endpoints keep their x so the curve always spans the full range
    if (drag === 0) p[0] = 0;
    else if (drag === pts.length - 1) p[0] = 1;
    pts[drag] = p;
    pts.sort((a, b) => a[0] - b[0]);
    drag = pts.indexOf(p);
    drawCurve(); commitCurves();
  });
  const finish = () => {
    if (drag >= 0) { drag = -1; saveState(); }
    refreshCurveCursor();
  };
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) cv.addEventListener(type, finish);
  refreshCurveCursor = installCanvasHandleCursor(cv, {
    hitCursor: point => {
      const index = near(at(point));
      return index < 0 ? 'crosshair' : index === 0 || index === S.curve[S.curveCh].length - 1 ? 'ns-resize' : 'grab';
    },
    dragCursor: () => drag < 0 ? null : drag === 0 || drag === S.curve[S.curveCh].length - 1 ? 'ns-resize' : 'grabbing',
  });
  cv.addEventListener('dblclick', (e) => {
    const i = near(at(e));
    const pts = S.curve[S.curveCh];
    if (i > 0 && i < pts.length - 1) {
      pushUndo(); pts.splice(i, 1); drawCurve(); commitCurves(); saveState();
    }
  });
  document.querySelectorAll('.curveCh').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('.curveCh').forEach((o) => o.classList.remove('on'));
      b.classList.add('on'); S.curveCh = b.dataset.ch; drawCurve();
    };
  });
}());

(function paramCurveEvents() {
  $('curveModePoint')?.addEventListener('click', () => {
    S.curveMode = 'point';
    syncCurveFromGrade();
    $('curveModePoint').classList.add('on');
    $('curveModeParametric').classList.remove('on');
    $('pointCurveWrap').hidden = false;
    $('parametricCurveWrap').hidden = true;
    drawCurve();
  });
  $('curveModeParametric')?.addEventListener('click', () => {
    S.curveMode = 'parametric';
    $('curveModePoint').classList.remove('on');
    $('curveModeParametric').classList.add('on');
    $('pointCurveWrap').hidden = true;
    $('parametricCurveWrap').hidden = false;
    syncParamCurveControls();
    drawParamCurve();
  });
  ['paramCurveHighlights', 'paramCurveLights', 'paramCurveDarks', 'paramCurveShadows',
   'paramCurveSplitSD', 'paramCurveSplitDL', 'paramCurveSplitLH'].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener('pointerdown', pushUndo);
    el.addEventListener('input', () => {
      readParamCurveControls();
      commitCurves();
    });
    el.addEventListener('change', () => saveState());
    el.addEventListener('dblclick', () => {
      pushUndo();
      if (id === 'paramCurveSplitSD') el.value = '25';
      else if (id === 'paramCurveSplitDL') el.value = '50';
      else if (id === 'paramCurveSplitLH') el.value = '75';
      else el.value = '0';
      readParamCurveControls();
      commitCurves();
      saveState();
    });
  });
}());

/* --------------------------------------------------------- colour mixer */
S.hslBand = 'red';
(function hslInit() {
  const bandNames = {red: tr('Red'), orange: tr('Orange'), yellow: tr('Yellow'), green: tr('Green'),
    aqua: tr('Aqua'), blue: tr('Blue'), purple: tr('Purple'), magenta: tr('Magenta')};
  $('hslBands').innerHTML = HSL_BANDS
    .map((b) => `<button data-band="${b}" title="${i18nHTML(bandNames[b])}" aria-label="${i18nHTML(bandNames[b])}"${b === 'red' ? ' class="on"' : ''}>${i18nHTML(Array.from(bandNames[b]).slice(0, 3).join(''))}</button>`)
    .join('');
  $('hslBands').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    $('hslBands').querySelectorAll('button').forEach((o) => o.classList.remove('on'));
    b.classList.add('on'); S.hslBand = b.dataset.band; syncHsl();
  });
  document.querySelectorAll('[data-hsl]').forEach((el) => {
    el.addEventListener('pointerdown', pushUndo);
    el.addEventListener('input', () => {
      S.grade.hsl = S.grade.hsl || {};
      const e2 = S.grade.hsl[S.hslBand] || { h: 0, s: 0, l: 0 };
      e2[el.dataset.hsl] = +el.value;
      S.grade.hsl[S.hslBand] = e2;
      document.querySelector(`[data-hslv="${el.dataset.hsl}"]`).textContent = fmtG(el.value);
      drawGrade();
    });
    el.addEventListener('change', () => saveState());
    const resetHsl = () => {
      pushUndo();
      S.grade.hsl = S.grade.hsl || {};
      const e2 = S.grade.hsl[S.hslBand] || { h: 0, s: 0, l: 0 };
      e2[el.dataset.hsl] = 0;
      S.grade.hsl[S.hslBand] = e2;
      el.value = '0';
      const out = document.querySelector(`[data-hslv="${el.dataset.hsl}"]`);
      if (out) out.textContent = fmtG(0);
      drawGrade();
      saveState();
    };
    el.addEventListener('dblclick', resetHsl);
    const row = el.closest('.row, .slider-row');
    row?.querySelector('.name')?.addEventListener('dblclick', resetHsl);
  });
}());
function syncHsl() {
  const e = (S.grade.hsl && S.grade.hsl[S.hslBand]) || { h: 0, s: 0, l: 0 };
  document.querySelectorAll('[data-hsl]').forEach((el) => {
    el.value = e[el.dataset.hsl] || 0;
    document.querySelector(`[data-hslv="${el.dataset.hsl}"]`).textContent = fmtG(el.value);
  });
  syncTreatmentControls();
}

function syncTreatmentControls() {
  const isBw = Boolean(S.grade?.monochrome);
  $('treatmentColor')?.classList.toggle('on', !isBw);
  $('treatmentColor')?.setAttribute('aria-selected', !isBw);
  $('treatmentBw')?.classList.toggle('on', isBw);
  $('treatmentBw')?.setAttribute('aria-selected', isBw);
  if ($('colorSectionLabel')) $('colorSectionLabel').textContent = isBw ? tr('B&W') : tr('Color');
  if ($('colorVibranceRow')) $('colorVibranceRow').hidden = isBw;
  if ($('colorSaturationRow')) $('colorSaturationRow').hidden = isBw;
  if ($('pointColorWrap')) $('pointColorWrap').hidden = isBw;
  if ($('colorMixerWrap')) $('colorMixerWrap').hidden = isBw;
  if ($('bwMixerWrap')) $('bwMixerWrap').hidden = !isBw;
  if (isBw) syncBwMixer();
}

function syncBwMixer() {
  HSL_BANDS.forEach((band) => {
    const val = S.grade.hsl?.[band]?.l ?? 0;
    const input = document.querySelector(`[data-bw-band="${band}"]`);
    if (input) input.value = val;
    const label = document.querySelector(`[data-bw-val="${band}"]`);
    if (label) label.textContent = fmtG(val);
  });
}

(function bwMixerInit() {
  $('treatmentColor')?.addEventListener('click', () => {
    if (!S.grade?.monochrome) return;
    pushUndo();
    S.grade.monochrome = 0;
    syncTreatmentControls();
    drawGrade();
    saveState();
  });
  $('treatmentBw')?.addEventListener('click', () => {
    if (S.grade?.monochrome) return;
    pushUndo();
    S.grade.monochrome = 1;
    syncTreatmentControls();
    drawGrade();
    saveState();
  });
  document.querySelectorAll('[data-bw-band]').forEach((el) => {
    const band = el.dataset.bwBand;
    el.addEventListener('pointerdown', pushUndo);
    el.addEventListener('input', () => {
      S.grade.hsl = S.grade.hsl || {};
      const e2 = S.grade.hsl[band] || { h: 0, s: 0, l: 0 };
      e2.l = +el.value;
      S.grade.hsl[band] = e2;
      const label = document.querySelector(`[data-bw-val="${band}"]`);
      if (label) label.textContent = fmtG(el.value);
      drawGrade();
    });
    el.addEventListener('change', () => saveState());
    const resetBw = () => {
      pushUndo();
      S.grade.hsl = S.grade.hsl || {};
      const e2 = S.grade.hsl[band] || { h: 0, s: 0, l: 0 };
      e2.l = 0;
      S.grade.hsl[band] = e2;
      el.value = '0';
      const label = document.querySelector(`[data-bw-val="${band}"]`);
      if (label) label.textContent = fmtG(0);
      drawGrade();
      saveState();
    };
    el.addEventListener('dblclick', resetBw);
    const row = el.closest('.row, .slider-row');
    row?.querySelector('.name')?.addEventListener('dblclick', resetBw);
  });
}());

/* ---------------------------------------------------------- point color */
function syncPointColor() {
  const select = $('pointColorSelect');
  if (!select) return;
  const points = Array.isArray(S.grade?.pointColor) ? S.grade.pointColor : [];
  S.pointColorIndex = clamp(S.pointColorIndex, 0, Math.max(0, points.length - 1));
  select.replaceChildren(...(points.length ? points.map((point, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = tr("Color {value} · {value2}°", {value: index + 1, value2: Math.round(point.hue)});
    return option;
  }) : [Object.assign(document.createElement('option'), {
    value: '', textContent: tr("No sampled colors"),
  })]));
  select.value = points.length ? String(S.pointColorIndex) : '';
  select.disabled = !points.length;
  $('pointColorDelete').disabled = !points.length;
  const current = points[S.pointColorIndex] || {
    range: 30, hueShift: 0, saturation: 0, luminance: 0,
    uniformHue: 0, uniformSaturation: 0, uniformLuminance: 0,
  };
  document.querySelectorAll('[data-point-color]').forEach((input) => {
    const key = input.dataset.pointColor;
    input.disabled = !points.length || (key.startsWith('uniform') &&
      (current.refSaturation == null || current.refLuminance == null));
    input.value = current[key] ?? 0;
    const output = document.querySelector(`[data-point-color-value="${key}"]`);
    if (output) output.textContent = ['range', 'hueShift'].includes(key) ? `${Math.round(current[key])}°` : fmtG(current[key]);
  });
  $('pointColorSample').classList.toggle('on', S.pointColorPick);
  $('cmp').classList.toggle('color-picking', S.pointColorPick);
}

$('pointColorSelect').onchange = () => {
  S.pointColorIndex = +$('pointColorSelect').value || 0;
  syncPointColor();
};
$('pointColorSample').onclick = () => {
  S.pointColorPick = !S.pointColorPick;
  if (S.pointColorPick) {
    S.wbPick = false; S.maskColorPick = false;
    $('maskColorSample').classList.remove('on');
    $('wbBtn').classList.remove('on');
    $('cmp').classList.remove('wb-picking');
    setCompareActive(false);
    toast(tr("Click a color in the photo"));
  }
  syncPointColor(); syncCompareControl(); syncPreviewBackend();
};
$('pointColorDelete').onclick = () => {
  if (!S.grade.pointColor?.length) return;
  pushUndo();
  S.grade.pointColor.splice(S.pointColorIndex, 1);
  if (!S.grade.pointColor.length) delete S.grade.pointColor;
  S.pointColorIndex = Math.max(0, S.pointColorIndex - 1);
  syncPointColor(); drawGrade(); saveState();
};
document.querySelectorAll('[data-point-color]').forEach((input) => {
  input.addEventListener('pointerdown', pushUndo);
  input.addEventListener('input', () => {
    const point = S.grade.pointColor?.[S.pointColorIndex];
    if (!point) return;
    point[input.dataset.pointColor] = +input.value;
    syncPointColor(); drawGrade();
  });
  input.addEventListener('change', () => saveState());
  const resetPointColor = () => {
    const point = S.grade.pointColor?.[S.pointColorIndex];
    if (!point) return;
    pushUndo();
    const def = input.dataset.pointColor === 'range' ? 30 : 0;
    point[input.dataset.pointColor] = def;
    input.value = String(def);
    syncPointColor(); drawGrade(); saveState();
  };
  input.addEventListener('dblclick', resetPointColor);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetPointColor);
});

/* ------------------------------------------------------- color grading */
const COLOR_GRADE_TONES = ['shadows', 'midtones', 'highlights', 'global'];
function emptyColorGrading() {
  return makeEmptyColorGrading(COLOR_GRADE_TONES);
}
function ensureColorGrading() {
  if (!S.grade.colorGrading) S.grade.colorGrading = emptyColorGrading();
  return S.grade.colorGrading;
}
function syncColorGrading() {
  if (!$('gradingWheels')) return;
  const settings = S.grade?.colorGrading || emptyColorGrading();
  document.querySelectorAll('[data-cg-tone]').forEach((button) => {
    const tone = button.dataset.cgTone;
    const selected = tone === S.colorGradeTone;
    button.classList.toggle('on', selected);
    const wheel = button.querySelector('i');
    if (wheel) {
      wheel.style.setProperty('--wheel-hue', `${settings[tone].hue}deg`);
      wheel.style.setProperty('--wheel-saturation', settings[tone].saturation);
    }
  });
  const current = settings[S.colorGradeTone];
  document.querySelectorAll('[data-color-grade]').forEach((input) => {
    const key = input.dataset.colorGrade;
    input.value = current[key];
    const output = document.querySelector(`[data-color-grade-value="${key}"]`);
    if (output) output.textContent = key === 'hue' ? `${Math.round(current[key])}°` : fmtG(current[key]);
  });
  document.querySelectorAll('[data-color-grade-master]').forEach((input) => {
    const key = input.dataset.colorGradeMaster;
    input.value = settings[key];
    const output = document.querySelector(`[data-color-grade-master-value="${key}"]`);
    if (output) output.textContent = fmtG(settings[key]);
  });
}
document.querySelectorAll('[data-cg-tone]').forEach((button) => {
  button.onclick = () => { S.colorGradeTone = button.dataset.cgTone; syncColorGrading(); };
  const wheel = button.querySelector('i');
  if (!wheel) return;
  let dragging = false;
  const update = (event) => {
    const rect = wheel.getBoundingClientRect();
    const dx = event.clientX - (rect.left + rect.width / 2);
    const dy = event.clientY - (rect.top + rect.height / 2);
    const item = ensureColorGrading()[button.dataset.cgTone];
    item.hue = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
    item.saturation = clamp(Math.hypot(dx, dy) / (rect.width * 0.42), 0, 1);
    S.colorGradeTone = button.dataset.cgTone;
    syncColorGrading(); drawGrade();
  };
  wheel.addEventListener('pointerdown', (event) => {
    pushUndo(); dragging = true; wheel.setPointerCapture(event.pointerId); update(event);
  });
  wheel.addEventListener('pointermove', (event) => { if (dragging) update(event); });
  wheel.addEventListener('pointerup', () => { if (dragging) saveState(); dragging = false; });
});
document.querySelectorAll('[data-color-grade]').forEach((input) => {
  input.addEventListener('pointerdown', pushUndo);
  input.addEventListener('input', () => {
    ensureColorGrading()[S.colorGradeTone][input.dataset.colorGrade] = +input.value;
    syncColorGrading(); drawGrade();
  });
  input.addEventListener('change', () => saveState());
  const resetColorGrade = () => {
    pushUndo();
    ensureColorGrading()[S.colorGradeTone][input.dataset.colorGrade] = 0;
    input.value = '0';
    syncColorGrading(); drawGrade(); saveState();
  };
  input.addEventListener('dblclick', resetColorGrade);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetColorGrade);
});
document.querySelectorAll('[data-color-grade-master]').forEach((input) => {
  input.addEventListener('pointerdown', pushUndo);
  input.addEventListener('input', () => {
    ensureColorGrading()[input.dataset.colorGradeMaster] = +input.value;
    syncColorGrading(); drawGrade();
  });
  input.addEventListener('change', () => saveState());
  const resetColorGradeMaster = () => {
    pushUndo();
    const def = input.dataset.colorGradeMaster === 'blending' ? 0.5 : 0;
    ensureColorGrading()[input.dataset.colorGradeMaster] = def;
    input.value = String(def);
    syncColorGrading(); drawGrade(); saveState();
  };
  input.addEventListener('dblclick', resetColorGradeMaster);
  const row = input.closest('.row, .slider-row');
  row?.querySelector('.name')?.addEventListener('dblclick', resetColorGradeMaster);
});

/* ------------------------------------------------- extra reset groups */
document.querySelectorAll('a.reset').forEach((a) => {
  if (!['curve', 'hsl', 'pointColor', 'colorGrading'].includes(a.dataset.reset)) return;
  a.addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation(); pushUndo();
    if (a.dataset.reset === 'curve') {
      for (const ch of ['L', 'R', 'G', 'B']) S.curve[ch] = [[0, 0], [1, 1]];
      S.paramCurve = { highlights: 0, lights: 0, darks: 0, shadows: 0, splitSD: 0.25, splitDL: 0.50, splitLH: 0.75 };
      syncParamCurveControls();
      drawCurve();
      drawParamCurve();
      commitCurves();
    } else if (a.dataset.reset === 'hsl') {
      delete S.grade.hsl; syncHsl(); drawGrade();
    } else if (a.dataset.reset === 'pointColor') {
      delete S.grade.pointColor; S.pointColorIndex = 0; syncPointColor(); drawGrade();
    } else {
      delete S.grade.colorGrading; S.colorGradeTone = 'shadows';
      syncColorGrading(); drawGrade();
    }
    saveState();
  });
});

/* --------------------------------------------------------- 1:1 and clip */
$('zoom1').onclick = toggleActualZoom;
S.clip = false;
$('clipBtn').onclick = () => {
  S.clip = !S.clip;
  $('clipBtn').classList.toggle('on', S.clip);
  syncPreviewBackend();
  refreshSpotVisualization();
  drawGrade();
  drawHistogram();
  toast(S.clip ? tr("Clipping warning active") : tr("Clipping warning off"));
};

/* ------------------------------------------------------------ auto tone */
$('autoBtn').onclick = (event) => {
  event.stopPropagation();
  if (!photoReadyForEditing()) return;
  refreshWebGLSamplingSurface();
  const s = S.gl && S.gl.sample();
  if (!s) {
    toast(tr("Auto tone: waiting for image preview…"));
    return;
  }
  let sum = 0, n = 0;
  const hist = new Uint32Array(256);
  for (let i = 0; i < s.px.length; i += 16) {
    const y = (s.px[i] * 0.2126 + s.px[i + 1] * 0.7152 + s.px[i + 2] * 0.0722);
    hist[Math.min(255, y | 0)]++; sum += y; n++;
  }
  const mean = sum / n / 255;
  let lo = 0, hi = 255, acc = 0;
  const total = n, cut = total * 0.005;
  for (let i = 0; i < 256; i++) { acc += hist[i]; if (acc > cut) { lo = i; break; } }
  acc = 0;
  for (let i = 255; i >= 0; i--) { acc += hist[i]; if (acc > cut) { hi = i; break; } }
  pushUndo();
  S.grade.exposure = clamp(Math.log2(0.45 / Math.max(mean, 0.02)), -1.5, 1.5);
  S.grade.blacks = clamp(-(lo / 255) * 1.2, -1, 0.4);
  S.grade.whites = clamp((1 - hi / 255) * 1.2, -0.4, 1);
  syncGrade(); drawGrade(); saveState();
  toast(tr("Auto tone applied"));
};

/* ---------------------------------------------------------- WB dropper */
S.wbPick = false;
$('wbBtn').onclick = (event) => {
  event.stopPropagation();
  S.wbPick = !S.wbPick;
  if (S.wbPick) {
    S.pointColorPick = false; S.maskColorPick = false;
    $('maskColorSample').classList.remove('on');
    syncPointColor();
    setCompareActive(false);
  }
  $('wbBtn').classList.toggle('on', S.wbPick);
  $('cmp').classList.toggle('wb-picking', S.wbPick);
  syncCompareControl();
  syncPreviewBackend();
  if (S.wbPick) toast(tr("Click a neutral grey area"));
};
function handleCanvasSample(e) {
  if (!S.maskColorPick && !S.pointColorPick && !S.wbPick) return;
  if (e.target.closest('#cropLayer, #editOverlay, .cmp-bar, #compareSnap')) return;
  const cv = $('cv'), r = cv.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const u = clamp((e.clientX - r.left) / r.width, 0, 1);
  const v = clamp((e.clientY - r.top) / r.height, 0, 1);
  if (S.maskColorPick) {
    sampleMaskColorAt(u, v);
    return;
  }
  if (S.pointColorPick) {
    refreshWebGLSamplingSurface();
    const px = S.gl && S.gl.samplePixel(u, v);
    if (!px) return toast(tr("Color sampling: waiting for image preview…"));
    const [red, green, blue] = [px[0] / 255, px[1] / 255, px[2] / 255];
    if (Math.max(red, green, blue) - Math.min(red, green, blue) < 0.015) {
      return toast(tr("Choose a more colorful area"));
    }
    pushUndo();
    S.grade.pointColor = Array.isArray(S.grade.pointColor)
      ? S.grade.pointColor.slice(0, 7) : [];
    const maximum = Math.max(red, green, blue);
    const sampledSaturation = maximum > 1e-5
      ? (maximum - Math.min(red, green, blue)) / maximum : 0;
    S.grade.pointColor.push({ hue: rgbHue(red, green, blue), range: 30,
      hueShift: 0, saturation: 0, luminance: 0,
      refSaturation: sampledSaturation, refLuminance: maximum,
      uniformHue: 0, uniformSaturation: 0, uniformLuminance: 0 });
    S.pointColorIndex = S.grade.pointColor.length - 1;
    S.pointColorPick = false;
    syncPointColor(); syncCompareControl(); syncPreviewBackend(); drawGrade(); saveState();
    toast(tr("Point color sampled"));
    return;
  }
  if (!S.wbPick) return;
  refreshWebGLSamplingSurface();
  const px = S.gl && S.gl.samplePixel(u, v);
  if (!px) return toast(tr("White balance: waiting for image preview…"));
  const [rr, gg, bb] = [px[0] / 255, px[1] / 255, px[2] / 255];
  if (rr + gg + bb < 0.06) return toast(tr("Too dark to sample"));
  pushUndo();
  // Undo the channel gains the temp/tint model applies.
  S.grade.temp = clamp(((bb - rr) / Math.max(rr + bb, 1e-3)) / 0.36, -1, 1);
  S.grade.tint = clamp(((gg - (rr + bb) / 2) / Math.max(gg, 1e-3)) / 0.18, -1, 1);
  syncGrade(); drawGrade(); saveState();
  S.wbPick = false; $('wbBtn').classList.remove('on');
  $('cmp').classList.remove('wb-picking');
  syncCompareControl();
  syncPreviewBackend();
  toast(tr("White balance set"));
}
$('cv').addEventListener('click', handleCanvasSample);
$('cmp').addEventListener('click', handleCanvasSample);

/* ------------------------------------------------------- crop ratio */
$('cropRatio').addEventListener('change', () => setCropRatio($('cropRatio').value));
$('cropLock').onclick = () => {
  if (!cur()) return;
  if (!S.cropLocked && S.cropRatio === 'free') {
    const ratio = S.crop ? cropImageAspect() * S.crop.w / S.crop.h : cropImageAspect();
    S.cropRatio = 'custom'; S.cropCustomWidth = +ratio.toFixed(4); S.cropCustomHeight = 1;
    S.cropAspectFlipped = false;
  }
  S.cropLocked = !S.cropLocked;
  // Lock the current frame without resizing it.
  if (S.cropLocked && S.crop) {
    const ratio = cropImageAspect() * S.crop.w / S.crop.h;
    if (Math.abs((cropOutputRatio() || ratio) - ratio) > 0.001) {
      S.cropRatio = 'custom'; S.cropCustomWidth = +ratio.toFixed(4); S.cropCustomHeight = 1;
      S.cropAspectFlipped = false;
    }
  }
  rememberCropChoices(); syncCropPanel();
};
$('cropSwap').onclick = () => {
  if (!cropOutputRatio()) return;
  S.cropAspectFlipped = !S.cropAspectFlipped;
  applyCropRatioChoice();
};
for (const id of ['cropCustomWidth', 'cropCustomHeight']) {
  $(id).addEventListener('change', () => {
    const width = +$('cropCustomWidth').value, height = +$('cropCustomHeight').value;
    if (!(width >= 0.01 && width <= 10000 && height >= 0.01 && height <= 10000)) {
      toast(tr("Enter a width and height between 0.01 and 10,000")); syncCropPanel(); return;
    }
    S.cropCustomWidth = width; S.cropCustomHeight = height;
    S.cropLocked = true; applyCropRatioChoice();
  });
}

/* ------------------------------------------------------- multi-select */
function paintSelectionState() {
  KEYWORD_BATCH?.sync();
  CAPTURE_TIME?.selectionChanged();
  const currentName = cur()?.name;
  document.querySelectorAll('.cell').forEach((c) => {
    c.classList.toggle('sel', c.dataset.name === currentName);
    c.classList.toggle('msel', S.msel.has(c.dataset.name));
    const mark = c.querySelector('.idx');
    if (mark) mark.textContent = S.msel.has(c.dataset.name) ? '✓' : '';
  });
  document.querySelectorAll('.thumb').forEach((thumb) => {
    thumb.classList.toggle('msel', S.msel.has(thumb.dataset.name));
  });
  $('counts').textContent = S.msel.size ? tr("{SMselSize} selected", {SMselSize: S.msel.size}) : `${S.idx + 1}/${S.images.length}`;
  $('counts').title = S.msel.size
    ? tr("{SMselSize} selected", {SMselSize: S.msel.size})
    : tr('Current photo / total photos');
  syncCullBars();
  updateTransferActions();
}

function toggleSel(name, additive) {
  SELECTION_REQUEST?.cancel();
  if (!additive) S.msel.clear();
  S.msel.has(name) ? S.msel.delete(name) : S.msel.add(name);
  selectionAnchorName = name;
  paintSelectionState();
}

async function selectPhotoFromPointer(image, event) {
  SELECTION_REQUEST?.cancel();
  const additive = event.metaKey || event.ctrlKey;
  if (event.shiftKey) {
    const list = visible();
    const anchorName = selectionAnchorName || cur()?.name || image.name;
    const anchorIndex = Math.max(0,
      list.findIndex((item) => item.name === anchorName));
    const targetIndex = list.findIndex((item) => item.name === image.name);
    if (!additive) S.msel.clear();
    if (targetIndex >= 0) {
      const first = Math.min(anchorIndex, targetIndex);
      const last = Math.max(anchorIndex, targetIndex);
      for (const item of list.slice(first, last + 1)) S.msel.add(item.name);
    }
  } else if (additive) {
    // A plain click represents one selection through cur(), until a modifier
    // click expands it into the explicit selection set.
    if (!S.msel.size && visible().includes(cur())) S.msel.add(cur().name);
    S.msel.has(image.name) ? S.msel.delete(image.name) : S.msel.add(image.name);
    selectionAnchorName = image.name;
  } else {
    S.msel.clear();
    selectionAnchorName = image.name;
  }
  // go() changes the current photo synchronously before it may wait for that
  // photo's catalog state. Repaint after starting navigation so a click never
  // leaves the grid looking unselected while the state request is in flight.
  const navigation = go(S.images.indexOf(image));
  paintSelectionState();
  await navigation;
  paintSelectionState();
}

/* -------------------------------------------------------------- prefs */
async function savePrefs() {
  const patch = {
    engine: $('engine').value,
    filter: $('filter').value, sort: $('sort').value,
    gridSize: $('gridSize').value, activePane: S.activePane,
    activeFolder: S.activeFolder, includeSubfolders: S.includeSubfolders,
    activeFolders: S.activeFolders, favoriteFolders: S.favoriteFolders,
    folderMode: S.folderMode, viewMode: S.viewMode,
    gridViewMode: S.gridViewMode,
    activeCollection: S.activeCollection,
    ratingFilter: $('ratingFilter').value, kindFilter: $('kindFilter').value,
    labelFilter: $('labelFilter').value, editFilter: $('editFilter').value,
    fileTypeFilters: LIBRARY_FILTERS.types(),
    metadataFilters: LIBRARY_FILTERS.metadata(),
    hideUndisplayablePhotos: LIBRARY_FILTERS.hideUndisplayable(),
    exWhich: $('exWhich').value, exFormat: $('exFormat').value,
    exQuality: $('exQuality').value, exSize: $('exSize').value,
    exColorSpace: $('exColorSpace').value,
    exDestination: $('exDestination').value,
    exFilenameTemplate: $('exFilenameTemplate').value,
    exCollision: $('exCollision').value,
    exRecipeExtras: EXPORT_RECIPE_EXTRAS,
    ...Object.fromEntries(EXPORT_EXTRA_FIELDS.map((key) => ['ex' + key, $('ex' + key).value])),
    filmstripHeight: currentFilmstripHeight(),
    softProof: S.softProof,
    presetFavorites: APP_PREFS.presetFavorites || [],
    hiddenBuiltinPresets: APP_PREFS.hiddenBuiltinPresets || [],
    presetPacks: APP_PREFS.presetPacks || {},
    cull: { on: S.cull.on, review: S.cull.review },
    leftCollapsed: $('appShell').classList.contains('left-collapsed'),
    filmstripHidden: document.querySelector('.workspace').classList.contains('filmstrip-hidden'),
  };
  Object.assign(APP_PREFS, patch);
  return api('/api/prefs', patch).catch(() => {});
}
['engine', 'filter', 'sort'].forEach((id) => {
  $(id).addEventListener('change', savePrefs);
});
fetch('/api/prefs').then((r) => r.json()).then((p) => {
  p = p && typeof p === 'object' ? p : {};
  FIRST_RUN?.setPrefs(p);
  const migrated = {};
  if (!Object.prototype.hasOwnProperty.call(p, 'keyScheme') &&
      localStorage.getItem('lt.keyScheme')) {
    migrated.keyScheme = localStorage.getItem('lt.keyScheme');
  }
  if (!Object.prototype.hasOwnProperty.call(p, 'speedKeys') &&
      localStorage.getItem('lt.speedKeys') != null) {
    migrated.speedKeys = localStorage.getItem('lt.speedKeys') !== '0';
  }
  if (!Object.prototype.hasOwnProperty.call(p, 'allowAutomation') &&
      localStorage.getItem('lt.allowAutomation') != null) {
    migrated.allowAutomation = localStorage.getItem('lt.allowAutomation') !== '0';
  }
  Object.assign(p, migrated);
  Object.assign(APP_PREFS, p);
  if (p.exRecipeExtras && typeof p.exRecipeExtras === 'object') {
    const { id: _id, name: _name, builtin: _builtin, ...settings } = p.exRecipeExtras;
    EXPORT_RECIPE_EXTRAS = settings;
  }
  KEY_SCHEME_NAME = p.keyScheme === 'classic' ? 'classic' : 'lighttable';
  KEYS = KEY_SCHEMES[KEY_SCHEME_NAME];
  if (Object.keys(migrated).length) {
    api('/api/prefs', migrated).then(() => {
      localStorage.removeItem('lt.keyScheme');
      localStorage.removeItem('lt.speedKeys');
      localStorage.removeItem('lt.allowAutomation');
    }).catch(() => {});
  }
  for (const [k, v] of Object.entries(p)) {
    if (k === 'pw') continue; // Retired preference; Advanced owns the override.
    const el = $(k);
    if (el && v != null) {
      // A catalog may carry export preferences from another platform.
      const unavailable = k === 'exFormat' && v === 'heif'
        && ['windows', 'linux'].includes(window.__LIGHTTABLE_PLATFORM__);
      el.value = unavailable ? 'jpeg' : v;
    }
  }
  $('pw').value = previewResolutionPreference(p);
  if (!$('kindFilter').value) $('kindFilter').value = 'all';
  if (!$('editFilter').value) $('editFilter').value = 'all';
  LIBRARY_FILTERS.setTypes(p.fileTypeFilters);
  LIBRARY_FILTERS.setMetadata(p.metadataFilters);
  LIBRARY_FILTERS.setHideUndisplayable(p.hideUndisplayablePhotos);
  if (p.gridSize) document.documentElement.style.setProperty('--cell', `${p.gridSize}px`);
  S.activeFolders = p.activeFolders && typeof p.activeFolders === 'object'
    ? p.activeFolders : {};
  S.favoriteFolders = Array.isArray(p.favoriteFolders) ? p.favoriteFolders : [];
  S.gridViewMode = ['photo', 'square'].includes(p.gridViewMode)
    ? p.gridViewMode : 'square';
  if (S.libraryLoaded) {
    S.activeCollection = typeof p.activeCollection === 'string' &&
      (S.library.collections || []).some((item) => item.id === p.activeCollection)
      ? p.activeCollection : '';
  } else {
    S.pendingActiveCollection = typeof p.activeCollection === 'string'
      ? p.activeCollection : null;
  }
  S.activeFolder = typeof S.activeFolders[S.rootFolder] === 'string'
    ? S.activeFolders[S.rootFolder]
    : (typeof p.activeFolder === 'string' ? p.activeFolder : '');
  if (S.folders.length && !S.folders.some((item) => item.path === S.activeFolder)) S.activeFolder = '';
  S.includeSubfolders = !!p.includeSubfolders;
  if (p.softProof && typeof p.softProof === 'object') {
    S.softProof = { enabled: !!p.softProof.enabled,
      profile: ['srgb', 'display_p3', 'matte', 'gloss'].includes(p.softProof.profile)
        ? p.softProof.profile : 'srgb',
      paper: p.softProof.paper !== false, gamut: p.softProof.gamut !== false };
    syncSoftProof();
  }
  if (p.cull && typeof p.cull === 'object') {
    const on = p.cull.on && typeof p.cull.on === 'object' ? p.cull.on : {};
    for (const name of [...CULL_SELECT, ...CULL_REJECT]) {
      S.cull.on[name] = !!on[name];
    }
    // Review always starts at "all": a filter that survives a restart and
    // hides most of the library is a bug report waiting to happen.
    S.cull.review = 'all';
  }
  syncCullPanel();
  $('includeSubfolders').checked = S.includeSubfolders;
  applyFilmstripHeight(p.filmstripHeight || FILMSTRIP_DEFAULT_HEIGHT);
  setFolderMode(p.folderMode === 'favorites' ? 'favorites' : 'browse', false);
  $('appShell').classList.toggle('left-collapsed', p.leftCollapsed === undefined
    ? window.__LIGHTTABLE_PLATFORM__ === 'linux' && window.innerWidth < 1100 : !!p.leftCollapsed);
  document.querySelector('.workspace').classList.toggle('filmstrip-hidden', !!p.filmstripHidden);
  $('leftPanelToggle').classList.toggle('on', isLibraryVisible());
  $('filmstripToggle').classList.toggle('on', !p.filmstripHidden);
  if (p.activePane) switchPane(p.activePane);
  syncEngineForProfile();
  setViewMode(['photo', 'square', 'detail'].includes(p.viewMode)
    ? p.viewMode : 'detail', false);
  refreshFilteredView();
  renderFolders();
}).catch(() => {});

PRESETS_READY = loadPresets().then(() => {
  const presetId = new URLSearchParams(location.search).get('preset');
  if (presetId) { switchPane('presetsPane'); void PRESET_BROWSER.openPreset(presetId); }
});
drawCurve();
renderVersions();

function scheduleAutomaticPreview() {
  clearTimeout(automaticPreviewTimer);
  if (zoomMotion.active) return;
  if ($('pw').value !== 'auto' || S.viewMode !== 'detail' || !cur()) return;
  automaticPreviewTimer = setTimeout(() => {
    viewFrameScheduler.flush();
    const width = requestedPreviewWidth();
    const detail = S.previewDetail;
    // Keep the largest useful surface while zooming out or back in. A view
    // change does not invalidate the recipe, and must not install a small
    // interactive texture over a sharper, already-presented result.
    if (!S.nativeViewport && S.presentedPhotoName === cur()?.name &&
        S.renderState === 'ready' && detail?.name === cur()?.name &&
        detail.refining === false && detail.renderedWidth >= width) return;
    if (automaticPreviewRequest?.name === cur()?.name &&
        automaticPreviewRequest?.width >= width) return;
    clearTimeout(renderTimer);
    clearTimeout(settleRenderTimer);
    doRender(performance.now(), { width, requestedWidth: width, phase: 'settled',
      background: S.presentedPhotoName === cur()?.name && S.renderState === 'ready' });
  }, 200);
}

function onViewportResize() {
  zoomMotion.cancel();
  const cv = $('cv');
  if (!cv || !cv.width) {
    scheduleNativeViewportLayout();
    return;
  }
  syncCropPresentationNow();
  const r = cv.getBoundingClientRect();
  const baseW = S.zoom > 0 ? r.width / S.zoom : r.width;
  if (baseW <= 0) {
    scheduleNativeViewportLayout();
    return;
  }
  if (S.cropping || S.cropTransition) {
    // The observer also fires for the frame's own animated resizes; only a
    // workspace change re-targets the cropping view.
    const state = cropViewState();
    const wrapKey = cropViewWrapKey();
    if (wrapKey === state.wrapKey) {
      scheduleNativeViewportLayout();
      return;
    }
    state.wrapKey = wrapKey;
    const target = cropViewTarget(S.crop, S.cropTransition ? 1 : state.bias);
    if (target) applyCropView(target);
    else applyViewNow();
    scheduleAutomaticPreview();
    return;
  }
  if (S.zoomMode === '100') {
    const sourceWidth = displaySourcePixelWidth();
    if (sourceWidth > 0) S.zoom = displaySourcePixelWidth() / baseW;
    S.targetPixelScale = 1;
    clampPan();
  } else if (S.zoomMode === 'custom' && S.targetPixelScale) {
    const sourceWidth = displaySourcePixelWidth();
    if (sourceWidth > 0) S.zoom = (S.targetPixelScale * sourceWidth) / baseW;
    clampPan();
  } else if (S.zoomMode !== 'custom') {
    S.zoom = 1;
    S.panX = 0;
    S.panY = 0;
  }
  applyViewNow();
  scheduleAutomaticPreview();
}

if (typeof ResizeObserver !== 'undefined') {
  const viewportObserver = new ResizeObserver(onViewportResize);
  viewportObserver.observe($('zoomwrap'));
  viewportObserver.observe($('cv'));
}
window.addEventListener('resize', onViewportResize);
function watchOverlayPixelRatio() {
  const display = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
  display.addEventListener('change', () => {
    drawEditOverlay();
    watchOverlayPixelRatio();
  }, { once: true });
}
watchOverlayPixelRatio();
document.addEventListener('visibilitychange', onViewportResize);
if (NATIVE_PREVIEW) scheduleNativeViewportLayout();


/* ============================================================== catalog,
 * culling, and the panes built on top of them.
 *
 * These are wired last so every function they close over already exists. Each
 * module owns its own markup and talks to the server directly; app.js only
 * supplies the shared helpers and the few callbacks that have to reach back
 * into the editor.
 */

const getJSON = (path) => fetch(path).then((response) => response.json());

SURVEY = createSurvey({
  el: $,
  images: () => S.images,
  onActivate: (name) => {
    const index = S.images.findIndex((image) => image.name === name);
    if (index >= 0) go(index);
  },
  onOpen: (name) => {
    const index = S.images.findIndex((image) => image.name === name);
    if (index >= 0) { go(index); setViewMode('detail'); }
  },
  onClose: () => {
    $('cullSimilarNavigation').hidden = true;
    $('surveyKeep').hidden = false;
    syncCullBars();
  },
});

HISTORY = createHistoryPanel({
  el: $,
  post: api,
  get: getJSON,
  toast,
  onStatus(status) { historySaveStatus = status; updateEditSaveStatus(); },
  enabled: () => S.catalogEnabled,
  onRestoreCaptureTime: async (name, historyId) => {
    if (!await saveState(true)) return;
    const result = await api('/api/metadata/capture-time', {action:'restore-history',name,historyId});
    if (!result?.ok || result.error) throw Error(((result?.error || tr("Could not restore capture time"))));
    _exifCache.clear(); await reloadLibrary();
    toast(tr("Capture time restored; photo edits are unchanged"));
  },
  onRestore: (state) => {
    pushUndo();
    restore(JSON.stringify(state));
    saveState(true);
    toast(tr("Restored that step"));
  },
});

CAPTURE_TIME = installCaptureTime({ el: $, post: api, toast,
  selection: () => (S.msel.size ? [...S.msel] : (cur() ? [cur().name] : [])),
  flush: () => saveState(true),
  changed: async () => { _exifCache.clear(); await reloadLibrary(); HISTORY?.refresh(cur()?.name, true); },
});

METADATA = createMetadataPanel({
  el: $,
  post: api,
  get: getJSON,
  toast,
  askName,
  flush: () => saveState(true),
  onKeywordsChanged: applyKeywordChanges,
  selection: () => (S.msel.size ? [...S.msel] : (cur() ? [cur().name] : [])),
  onKeywordFilter: (path) => {
    $('search').value = path.split(' > ').pop();
    refreshFilteredView();
  },
});

CATALOG_UI = createCatalogUI({
  el: $,
  post: api,
  get: getJSON,
  toast,
  sendNative,
  onCatalogImportCompleted: (result) => FIRST_RUN?.catalogCompleted(result),
  onCatalogImportClosed: () => FIRST_RUN?.catalogClosed(),
  selection: () => (S.msel.size ? [...S.msel] : (cur() ? [cur().name] : [])),
  onLibraryChanged: () => { reloadLibrary(); },
  onWatchArrival: async (status) => {
    const follow = status.follow
      && performance.now() - lastUserNavigationAt > 5000;
    await reloadLibrary();
    if (follow && status.latest) {
      const index = S.images.findIndex((image) => image.name === status.latest);
      if (index >= 0) await go(index);
    }
  },
});

/* Reload the library after an action that changed what is on disk. */
async function reloadLibrary() {
  try {
    const data = await getJSON('/api/images');
    if (!data || !data.images) return;
    const previous = cur() ? cur().name : null;
    clearEditedThumbnails();
    S.catalogEnabled = !!data.catalog?.enabled;
    S.catalogTotal = Number.isFinite(+data.total) ? +data.total : data.images.length;
    S.primarySourceId = catalogSourceId(data);
    S.folders = Array.isArray(data.folders) ? data.folders : S.folders;
    S.folderIds = data.folderIds && typeof data.folderIds === 'object'
      ? data.folderIds : S.folderIds;
    S.images = data.images.map((image) => normalizeLibraryImage(
      image, !S.catalogEnabled));
    PHOTO_DISPLAY_STATUS.retain(S.images);
    if (data.library) S.library = data.library;
    S.libraryLoaded = true;
    applyPendingActiveCollection();
    _stripKey = _gridKey = '';
    const index = previous
      ? S.images.findIndex((image) => image.name === previous) : -1;
    S.idx = index >= 0 ? index : (S.images.length ? 0 : -1);
    if (S.idx >= 0) await go(S.idx);
    else refreshLists();
    if (S.catalogEnabled && S.images.length < S.catalogTotal) {
      loadRemainingCatalogRows(S.catalogTotal).catch(() => {});
    }
    watchCatalogScan();
    renderFolders();
    if (METADATA) METADATA.refreshKeywordTree();
  } catch (error) {
    toast(tr("Could not refresh the library"));
  }
}

const isVideo = (im) => im?.kind === 'video';

/* Show the player for a clip and hide the editing surface behind it. The film
 * and grade pipelines are stills-only, so a video gets an honest player rather
 * than controls that would silently do nothing. */
function presentVideo(im) {
  const stage = $('videoStage');
  const player = $('videoPlayer');
  if (!stage || !player) return;
  const showing = isVideo(im);
  stage.hidden = !showing;
  document.body.classList.toggle('video-open', showing);
  if (showing) {
    const next = `/api/video?name=${encodeURIComponent(im.name)}`;
    if (player.getAttribute('src') !== next) player.setAttribute('src', next);
  } else if (player.getAttribute('src')) {
    player.pause();
    player.removeAttribute('src');
    player.load();
  }
}

function openSurvey(mode = 'survey') {
  $('cullSimilarNavigation').hidden = true;
  $('surveyKeep').hidden = false;
  const list = visible();
  let chosen = S.msel.size
    ? list.filter((image) => S.msel.has(image.name)).map((image) => image.name)
    : [];
  if (!chosen.length && list.length) {
    const currentIndex = Math.max(0, list.indexOf(cur()));
    const start = clamp(currentIndex - 2, 0, Math.max(0, list.length - 6));
    chosen = list.slice(start, start + 6).map((image) => image.name);
  }
  if (!chosen.length) { toast(tr("Select photos to survey")); return; }
  if (!SURVEY.open(chosen, mode)) toast(tr("Nothing to survey"));
}

/* Move every rejected photo in the current view to the Trash, through the
 * host. The server resolves and validates the paths but never unlinks. */
async function trashRejected() {
  if (!await saveState(true)) return;
  const rejected = visible().filter((image) => image.status === 'skipped' && !image.virtual);
  if (!rejected.length) { toast(tr("No rejected photos in this view")); return; }
  if (!window.confirm(
    trn("Move {count} rejected photo to the Trash? Sidecars go with them. Paired files hidden from this view stay in the library. Choose Both in pair settings to include them.", "Move {count} rejected photos to the Trash? Sidecars go with them. Paired files hidden from this view stay in the library. Choose Both in pair settings to include them.", rejected.length, {rejectedLength: rejected.length}))) return;
  let result;
  try {
    result = await api('/api/photos/trash',
                       { names: rejected.map((image) => image.name) });
    if (result.error) { toast(result.error); return; }
  } catch (error) { toast(error.message); return; }
  if (!result.paths?.length) { toast(tr("No rejected photos in this view")); return; }
  if (!sendNative('trashFiles', { paths: result.paths })) {
    toast(tr("Moving files to the Trash needs the desktop app"));
    return;
  }
  toast(tr("Moving {rejectedLength} photos to the Trash", {rejectedLength: result.photoCount ?? rejected.length}));
}

/* ------------------------------------------------------- survey controls */
if ($('surveyClose')) $('surveyClose').onclick = () => SURVEY.close();
if ($('surveySwap')) $('surveySwap').onclick = () => SURVEY.swap();
async function keepSurveySelection() {
  const keeper = SURVEY.active;
  if (!keeper) return;
  const kept = linkedMetadataTargets(S.images.filter(image => image.name === keeper));
  const keepNames = new Set(kept.map(image => image.name));
  const others = linkedMetadataTargets(S.images.filter(image =>
    SURVEY.names.includes(image.name) && !keepNames.has(image.name)))
    .filter(image => !keepNames.has(image.name));
  for (const image of kept) enqueuePhotoPatch(image, {status: 'approved'});
  for (const image of others) enqueuePhotoPatch(image, {status: 'skipped'});
  refreshLists();
  if (!await flushEditSaves()) return;
  toast(tr("Marked the select and rejected the rest"));
}
if ($('surveyKeep')) $('surveyKeep').onclick = keepSurveySelection;

/* --------------------------------------------------- lens: auto-straighten */
if ($('autoLevel')) {
  const runGeometry = async (mode) => {
    const im = cur();
    if (!im) return;
    const session = cropSession;
    $('autoLevelNote').textContent = tr("Looking for lines…");
    try {
      const result = await api('/api/geometry/auto',
                               { name: im.name, mode, rotate: S.params?.rotate || 0 });
      if (cur()?.name !== im.name || cropSession !== session) return;
      if (result.error) { $('autoLevelNote').textContent = result.error; return; }
      const patch = result.optics || {};
      if (!Object.keys(patch).length) {
        $('autoLevelNote').textContent =
          (((result.notes || []).join(' ') || tr("No usable lines found.")));
        return;
      }
      pushUndo();
      S.optics = { ...S.optics, ...patch };
      syncOpticsPanel();
      saveState(true);
      refreshBaseEdits();
      const confidence = Math.round((result.confidence || 0) * 100);
      $('autoLevelNote').textContent =
        tr("{value} lines · {confidence}% confidence{value2}", {value: (result.lines || 0), confidence: confidence, value2: (result.notes || []).length ? ` · ${result.notes.join(' ')}` : ''});
    } catch (error) {
      $('autoLevelNote').textContent = tr("Could not analyse this photo.");
    }
  };
  $('autoLevel').onclick = () => runGeometry('level');
  if ($('autoUpright')) $('autoUpright').onclick = () => runGeometry('full');
}

/* --------------------------------------------------------------- enhance */
let denoiseTimer = null;
if ($('learnedDenoiseApply')) {
  $('learned_denoise_strength').oninput = () => {
    S.params.learned_denoise_strength = +$('learned_denoise_strength').value;
    $('learnedDenoiseStrengthV').textContent = Number(
      $('learned_denoise_strength').value).toFixed(2);
  };
  getJSON('/api/enhance/capabilities').then((capabilities) => {
    const ready = !!capabilities.modes?.denoise;
    $('learnedDenoiseApply').dataset.available = ready ? '1' : '0';
    $('learnedDenoiseApply').disabled = !ready || !isRawInput();
    $('learnedDenoiseStatus').textContent = ready ? tr("Runs locally when Apply is pressed; the slider is not live.") : tr("The local denoise model is not installed.");
  }).catch(() => {
    $('learnedDenoiseStatus').textContent = tr("Denoise availability could not be checked.");
  });
  const finishDenoiseUI = () => {
    clearInterval(denoiseTimer); denoiseTimer = null;
    $('learnedDenoiseApply').disabled =
      $('learnedDenoiseApply').dataset.available !== '1' || !isRawInput();
    $('learnedDenoiseCancel').hidden = true;
    $('denoiseBadge').hidden = true;
  };
  $('learnedDenoiseCancel').onclick = async () => {
    await api('/api/denoise/cancel', {});
    $('learnedDenoiseStatus').textContent = tr("Cancelling…");
  };
  $('learnedDenoiseApply').onclick = async () => {
    const im = cur();
    if (!im || !isRawInput()) return;
    readControls();
    const next = { ...S.params, learned_denoise: true,
      learned_denoise_strength: +$('learned_denoise_strength').value };
    const result = await api('/api/denoise', { name: im.name, params: next });
    if (!result.ok) {
      $('learnedDenoiseStatus').textContent = ((result.error || tr("Denoise could not start.")));
      return;
    }
    pushUndo();
    S.params = normalizeFilmParams(result.params || next);
    if (!await saveState(true)) return;
    $('learnedDenoiseApply').disabled = true;
    $('learnedDenoiseCancel').hidden = false;
    $('learnedDenoiseStatus').textContent = tr("Preparing model…");
    $('denoiseBadge').textContent = tr("Denoising…");
    $('denoiseBadge').hidden = false;
    clearInterval(denoiseTimer);
    let polls = 0;
    denoiseTimer = setInterval(async () => {
      if (++polls > 1800) {
        finishDenoiseUI();
        $('learnedDenoiseStatus').textContent = tr("Denoise is still running; reopen this photo to check.");
        return;
      }
      const status = await getJSON('/api/denoise/status').catch(() => null);
      if (!status) return;
      const percent = status.total
        ? Math.round(status.progress * 100 / status.total) : 0;
      $('learnedDenoiseStatus').textContent = status.running ? status.total ? tr("Denoising… {percent}%", {percent: percent}) : tr("Preparing model…") : (status.error || tr("Denoise applied."));
      $('denoiseBadge').textContent = status.total ? tr("Denoising… {percent}%", {percent: percent}) : tr("Denoising…");
      if (!status.running) {
        finishDenoiseUI();
        if (!status.error && cur()?.name === im.name) renderFilm(0);
        if (!status.error) notifyCompletion(tr("Denoise complete"), displayName(im));
      }
    }, 750);
  };
}

if ($('enhanceRun')) {
  ENHANCE = createEnhancePanel({
    el: $, getPhoto: () => photoReadyForEditing() ? cur() : null,
    getCapabilities: () => getJSON('/api/enhance/capabilities'),
    run: (request) => api('/api/enhance', request),
    onComplete: async () => {
      setActionDialog('enhanceDialog', false);
      await reloadLibrary();
    },
  });
  void ENHANCE.refresh();
}

const PEOPLE = createPeoplePanel({
  api,
  onLabels: (labels) => {
    for (const image of S.images) image.people = labels[image.name.split('::lighttable-copy::')[0]] || [];
    invalidateVisibleCache();
    _stripKey = _gridKey = '';
    refreshLists();
  },
  onPhoto: async (name) => {
    await loadRemainingCatalogRows(S.catalogTotal);
    const index = S.images.findIndex(image => image.name === name);
    if (index < 0) throw new Error(tr('This photo is no longer in the catalog. Scan People again.'));
    setViewMode('detail');
    await go(index);
  },
});

/* ------------------------------------------------------- native messages */
const _origNativeEvent = window.lightTableNativeEvent;
window.lightTableNativeEvent = function (message) {
  DESKTOP_UPDATES.nativeEvent(message);
  FIRST_RUN?.nativeEvent(message);
  APPLE_PHOTOS?.nativeEvent(message);
  if (message?.type === 'presetLink' && typeof message.id === 'string') {
    switchPane('presetsPane'); void PRESET_BROWSER.openPreset(message.id);
    return;
  }
  if (message && message.type === 'openLibraryHealth') {
    RECOVERY?.open();
    return;
  }
  if (message && message.type === 'catalogFileSelected') {
    CATALOG_UI.setCatalogPath(message.path);
    return;
  }
  if (message && message.type === 'ingestFolderSelected') {
    CATALOG_UI.setIngestField(message.field || 'ingestSource', message.path);
    return;
  }
  if (message && message.type === 'volumes') {
    const source = $('ingestSource');
    if (source && !source.value && (message.volumes || []).length) {
      source.value = message.volumes[0].path;
    }
    return;
  }
  if (_origNativeEvent) _origNativeEvent(message);
};
PRESETS_READY.then(() => nativeBridge()?.postMessage({ action: 'requestPresetLinks' }));

/* ------------------------------------------------------------- selection */
if ($('renameOpen')) $('renameOpen').onclick = () => CATALOG_UI.openRename();
if ($('surveyOpen')) $('surveyOpen').onclick = () => openSurvey('survey');
if ($('unflagBtn')) $('unflagBtn').onclick = () => setStatus('pending');
if ($('labelFilter')) {
  $('labelFilter').onchange = () => { refreshFilteredView(); savePrefs(); };
}
if ($('keySchemeSelect')) {
  $('keySchemeSelect').value = KEY_SCHEME_NAME;
  $('keySchemeSelect').onchange = (event) => {
    APP_PREFS.keyScheme = event.target.value;
    api('/api/prefs', { keyScheme: event.target.value });
    toast(tr("Shortcut scheme applies after a reload"));
  };
}
if ($('speedKeysEnabled')) {
  $('speedKeysEnabled').checked = APP_PREFS.speedKeys !== false;
  $('speedKeysEnabled').onchange = (event) => {
    APP_PREFS.speedKeys = event.target.checked;
    api('/api/prefs', { speedKeys: event.target.checked });
  };
}

initMidi();
$('midiPill')?.addEventListener('click', () => toggleMidiLearn());
$('midiLearnBtn')?.addEventListener('click', () => toggleMidiLearn());
$('midiResetBtn')?.addEventListener('click', () => {
  resetMidiMappings();
  toast(tr("MIDI mappings reset to defaults"));
});
document.addEventListener('pointerdown', (event) => {
  const input = event.target?.closest?.('input[type="range"]');
  if (input) {
    const control = input.getAttribute('data-g') || input.id;
    const type = input.hasAttribute('data-g') ? 'grade' : 'params';
    const row = input.closest('.slider-row') || input.closest('.row');
    const label = row?.querySelector('.name')?.textContent?.trim() || control;
    setMidiLearnTarget({ type, control, min: Number(input.min), max: Number(input.max), label });
  }
});

$('secondaryLoupeBtn')?.addEventListener('click', () => {
  if (postNative('openSecondaryLoupe', {}, true)) return;
  const loupe = window.open('/web/loupe.html', 'LightTableLoupe', 'width=1920,height=1080');
  if (loupe) loupe.focus();
  else toast(tr("Allow pop-up windows to open the secondary loupe"));
});

for (const eventName of ['click', 'change', 'focusin', 'focusout', 'pointerup']) {
  document.addEventListener(eventName, () => {
    scheduleNativeMenuState();
    UI_BRIDGE?.schedule();
  });
}
installNativeWindowChrome();
CATALOG_UI.refresh();
METADATA.refreshKeywordTree();
if (cur()) { HISTORY.refresh(cur().name); METADATA.refresh(cur().name); }
presentVideo(cur());
scheduleNativeMenuState();

/* ------------------------------------------------------ external control */
function uiStateReport() {
  return {
    current: cur()?.name || null,
    selection: [...S.msel],
    visibleCount: visible().length,
    viewMode: S.viewMode,
    pane: S.activePane,
    zoom: S.zoom,
    zoomMode: S.zoomMode,
    render: {
      state: S.renderState,
      name: S.renderName,
      backend: S.presentedBackend,
    },
    compare: { active: S.compareActive, position: S.comparePosition },
    filter: {
      status: $('filter')?.value || 'all',
      rating: $('ratingFilter')?.value || 'all',
      kind: $('kindFilter')?.value || 'all',
      fileTypes: LIBRARY_FILTERS.types(), editState: $('editFilter')?.value || 'all',
      hideUndisplayablePhotos: LIBRARY_FILTERS.hideUndisplayable(),
      ...LIBRARY_FILTERS.metadata(),
      label: $('labelFilter')?.value || 'all',
      query: $('search')?.value || '',
    },
    sort: $('sort')?.value || 'capture',
    activeTool: S.cropping ? 'crop' : S.maskRefineMode || null,
    allowAutomation: APP_PREFS.allowAutomation !== false,
  };
}

async function reconcilePeerSave(image, recoveredSourceKey = null) {
  const generation = image.peerSyncGeneration = (image.peerSyncGeneration || 0) + 1;
  try {
    // Peer windows each have their own ordered queue. Echoing their accepted
    // edits back as new writes can make the two queues repair one another
    // forever. Finish our write, then read the authoritative saved recipe.
    await editSaveQueue.flush(image.name);
    const before = JSON.stringify(image);
    const editing = cur() === image && S.editingName === image.name;
    const editorBefore = editing ? snapshot() : null;
    const response = await fetch(`/api/state?name=${encodeURIComponent(image.name)}`);
    const state = await response.json();
    if (!state || state.error) throw new Error(((state?.error || tr("Could not refresh shared edits"))));
    if (generation !== image.peerSyncGeneration || editSaveQueue.getPending(image.name)
        || before !== JSON.stringify(image)
        || (editing && (cur() !== image || S.editingName !== image.name || editorBefore !== snapshot()))) return;
    const patch = recoveredSourceKey
      ? {...state, stateLoaded: true, hasEdits: true, recoverySourceKey: recoveredSourceKey} : state;
    await applyServerStateEvent({names: [image.name], patch, origin: 'window', reconciled: true});
    return true;
  } catch { /* A failed local save stays pending for the explicit Retry action. */ }
  return false;
}

async function applyServerStateEvent(event) {
  if (event.client === CLIENT_ID) return;
  const names = Array.isArray(event.names) ? event.names : [];
  const current = cur();
  const patchFor = name => {
    const patch = event.patches?.[name] || event.patch;
    if (!event.maskDelta || !patch) return patch;
    const image = S.images.find(image => image.name === name);
    const base = current?.name === name ? S.masks
      : editSaveQueue.getPending(name)?.state?.masks || image?.masks || patch.masks;
    return {...patch, masks: mergeMaskDelta(base, event.maskDelta)};
  };
  const patch = current ? patchFor(current.name) : event.patch;
  const hasPatches = names.some(name => patchFor(name) && typeof patchFor(name) === 'object');
  if (!hasPatches) {
    // IPTC updates do not change the edit recipe. A GET here can read an older
    // window save and wrongly restore it over newer local editor controls.
    if (current && names.includes(current.name)) METADATA?.refresh(current.name);
    return;
  }
  const deferredNames = new Set();
  if (!event.reconciled && event.origin?.startsWith('window')) {
    for (const image of S.images) {
      if (names.includes(image.name) && editSaveQueue.getPending(image.name)) {
        deferredNames.add(image.name);
        void reconcilePeerSave(image);
      }
    }
  }
  const currentChanged = current && names.includes(current.name)
    && !deferredNames.has(current.name) && S.editingName === current.name;
  const before = currentChanged ? snapshot() : null;
  for (const image of S.images) {
    if (!names.includes(image.name) || deferredNames.has(image.name)) continue;
    const patch = patchFor(image.name);
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) continue;
    if (image !== current) photoUndo.clear(image.name);
    // The event carries the accepted patch, even if an older browser request
    // subsequently overwrites it on disk. Repair after that in-flight request,
    // while keeping unrelated local fields and explicit retry after a failure.
    if (editSaveQueue.getPending(image.name)) enqueuePhotoPatch(image, patch);
    else {
      CULL_BATCH.noteFlagChange(image.name, patch);
      Object.assign(image, cloneValue(patch));
      if (image.stateLoadEdits) Object.assign(image.stateLoadEdits, cloneValue(patch));
    }
    invalidateEditedThumbnail(image);
  }
  if (currentChanged) {
    pushUndoState(before);
    restore(JSON.stringify({ ...JSON.parse(before), ...patch }), null, false);
    _lastHistorySnapshot = editHistorySnapshot();
    HISTORY?.refresh(current.name, true);
    const label = event.origin && event.origin !== 'window'
      ? tr('Updated by {eventOrigin}', {eventOrigin: event.origin}) : tr('Photo updated externally');
    if (event.origin !== 'batch-masks') toast(label, { label: tr('Undo'), run: undo });
  }
  invalidateVisibleCache();
  _stripKey = _gridKey = '';
  refreshLists();
  renderKeywords();
  renderVersions();
}

async function executeUICommand(command, args = {}, event = {}) {
  if (command === 'goto') {
    const index = S.images.findIndex((image) => image.name === args.name);
    if (index < 0) throw new Error(tr("Photo is not in the current library"));
    await go(index);
  } else if (command === 'select') {
    SELECTION_REQUEST?.cancel();
    const names = new Set(Array.isArray(args.names) ? args.names
      : String(args.names || '').split(',').filter(Boolean));
    if (args.action === 'clear') S.msel.clear();
    else if (args.action === 'set') S.msel = new Set(names);
    else if (args.action === 'add') names.forEach((name) => S.msel.add(name));
    else if (args.action === 'remove') names.forEach((name) => S.msel.delete(name));
    else throw new Error(tr("select action must be set, add, remove, or clear"));
    refreshLists();
  } else if (command === 'filter') {
    const fields = { status: 'filter', rating: 'ratingFilter', kind: 'kindFilter',
      label: 'labelFilter', query: 'search' };
    for (const [key, id] of Object.entries(fields)) {
      if (Object.prototype.hasOwnProperty.call(args, key) && $(id)) {
        $(id).value = String(args[key]);
      }
    }
    refreshFilteredView();
  } else if (command === 'sort') {
    $('sort').value = String(args.value || args.sort || 'capture');
    refreshFilteredView();
  } else if (command === 'slider') {
    const input = document.querySelector(`[data-g="${CSS.escape(String(args.key))}"]`)
      || $(String(args.key));
    if (!input || !['range', 'number'].includes(input.type)) {
      throw new Error(tr("Unknown slider"));
    }
    pushUndo();
    input.value = String(args.value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  } else if (command === 'mask.show') {
    const mask = S.masks.find((item) => item.id === args.id);
    if (!mask) throw new Error(tr("Unknown mask"));
    S.selectedMaskId = mask.id; switchPane('maskPane'); syncMaskPanel();
  } else if (command === 'overlay') {
    S.localPinsVisible = args.value !== false && args.value !== 'off';
    drawEditOverlay();
  } else if (command === 'toast') {
    toast(String(args.text || ''));
  } else if (command === 'reveal') {
    await revealCurrentPhoto();
  } else if (command === 'trash') {
    const paths = Array.isArray(args.paths) ? args.paths : [];
    if (!paths.length) throw new Error(tr("No files were supplied for Trash"));
    if (!sendNative('trashFiles', { paths })) {
      throw new Error(tr("Trash requires the native LightTable window"));
    }
  } else {
    const mutatesPhoto = /^(flag|rating|label):/.test(command)
      || ['rotateLeft', 'rotateRight', 'pasteSettings', 'resetEdit',
        'resetFilm', 'resetCrop', 'resetMasks', 'resetHealing', 'resetLens',
        'filmToggle'].includes(command);
    if (mutatesPhoto) pushUndo();
    await performNativeMenuCommand(command);
    if (mutatesPhoto) {
      const label = (event.origin ? tr("Updated by {eventOrigin}", {eventOrigin: event.origin}) : tr("Updated by automation"));
      toast(label, { label: tr("Undo"), run: undo });
    }
  }
  return uiStateReport();
}

if ($('allowAutomation')) {
  $('allowAutomation').checked = APP_PREFS.allowAutomation !== false;
  $('allowAutomation').addEventListener('change', (event) => {
    APP_PREFS.allowAutomation = event.target.checked;
    api('/api/prefs', { allowAutomation: event.target.checked });
    UI_BRIDGE?.schedule(0);
  });
}

SELECTION_REQUEST = createSelectionRequest({
  load: () => S.catalogEnabled && S.images.length < S.catalogTotal
    ? loadRemainingCatalogRows(S.catalogTotal) : Promise.resolve(),
  queryNames: async () => {
    if (!S.catalogEnabled) return null;
    const spec = buildCatalogQuerySpec({ namesOnly: true, limit: 100000 });
    const res = await api('/api/catalog/query', spec);
    // The loaded-row path already resolves the complete view. Retain it for
    // catalogs larger than a single names page rather than selecting a subset.
    if (res?.names?.length < res?.total) return null;
    return res?.names || null;
  },
  scope: selectionScope, visible, selection: () => S.msel,
  changed: () => refreshLists(), onError: error => toast(error.message),
});
MASK_BATCH = installMaskBatch({el: $, post: api, get: getJSON,
  flush: flushEditSaves, targets: transferTargets, toast});

UI_BRIDGE = installUIBridge({
  client: CLIENT_ID,
  post: api,
  report: uiStateReport,
  execute: executeUICommand,
  handlers: {
    'preview.progress': record => {
      if (record.client === CLIENT_ID && record.generation === S.seq && record.name === cur()?.name) {
        previewProgress.advance(record.completed, record.generation);
      }
    },
    job: record => MASK_BATCH?.update(record),
    state: (event) => applyServerStateEvent(event).catch(() => {}),
    library: () => reloadLibrary(),
    resync: () => reloadLibrary(),
  },
});
UI_BRIDGE.schedule(0);

RECOVERY = installRecovery({ el: $, post: api, get: getJSON, toast, sendNative });

installSettings({
  setPrefs(value) {
    Object.assign(APP_PREFS, value || {});
    KEY_SCHEME_NAME = APP_PREFS.keyScheme === 'classic' ? 'classic' : 'lighttable';
    KEYS = KEY_SCHEMES[KEY_SCHEME_NAME];
    if ($('keySchemeSelect')) $('keySchemeSelect').value = KEY_SCHEME_NAME;
    if ($('speedKeysEnabled')) $('speedKeysEnabled').checked = APP_PREFS.speedKeys !== false;
    if ($('allowAutomation')) $('allowAutomation').checked = APP_PREFS.allowAutomation !== false;
    updateLoupeInfoOverlay();
    syncCullBars();
  },
  refreshPreferences() {
    pairOverrides.clear(); syncPairControls(); syncCullBars();
    invalidateVisibleCache();
    _stripKey = _gridKey = '';
    refreshLists();
  },
  updateRuntimeControl(id, value) {
    const control = $(id);
    if (!control) return;
    control.value = value;
    control.dispatchEvent(new Event('change', { bubbles: true }));
  },
  async reloadDefaults() {
    const data = await getJSON('/api/images?limit=1').catch(() => null);
    if (!data) return;
    S.filmDefaults = data.defaults || S.filmDefaults;
    S.newPhotoGradeDefaults = data.gradeDefaults || S.newPhotoGradeDefaults;
    clearEditedThumbnails();
    _stripKey = _gridKey = '';
    refreshLists();
  },
  async reloadCurrentRawDefault() {
    const image = cur();
    if (!image?.raw) return;
    _rawDefaultCache.delete(image.name);
    await loadRawCameraDefault(image.name, true);
  },
  currentRawDefault() {
    return cur()?.raw ? S.rawDefault : null;
  },
  imageNames() {
    return S.images.filter((image) => !image.virtual && image.kind !== 'video')
      .map((image) => image.name);
  },
  toast,
});

FIRST_RUN = installFirstRunSetup({
  el: $, post: api, sendNative, nativeBridge, reloadLibrary,
  onComplete: () => {
    S.includeSubfolders = true;
    $('includeSubfolders').checked = true;
    refreshFilteredView();
  },
  openCatalog: () => $('importCatalogBtn').click(),
});

APPLE_PHOTOS = installApplePhotosBrowser({
  el: $, sendNative, nativeBridge,
  onImported: async (path) => {
    if (!await saveState(true)) throw new Error(tr('Could not save changes. Please try again.'));
    const result = await api('/api/catalog/sources', {action: 'add', path, importState: false});
    if (!result?.ok || result.error) throw new Error(result?.error || tr('Could not add that folder.'));
    await reloadLibrary();
  },
  onViewImported: async (path) => {
    if (!await saveState(true)) throw new Error(tr('Could not save changes. Please try again.'));
    S.includeSubfolders = true;
    $('includeSubfolders').checked = true;
    S.activeFolders[path] = '';
    await savePrefs();
    if (S.rootFolder === path) { await reloadLibrary(); selectFolder(''); }
    else sendNative('selectSource', {path});
  },
});
