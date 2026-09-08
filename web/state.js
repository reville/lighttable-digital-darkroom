export const cloneValue = (value) => value == null
  ? value : JSON.parse(JSON.stringify(value));

export function createAppState(gradeDefaults, opticsDefaults) {
  return {
    images: [], idx: -1, defaults: null, filmDefaults: null,
    newPhotoGradeDefaults: null,
    params: null, preset: null, grade: { ...gradeDefaults }, crop: null,
    masks: [], heals: [], optics: { ...opticsDefaults }, lensProfile: null,
    selectedMaskId: null, selectedHealId: null, editGesture: null,
    maskTextureDirty: true, maskCreateOpen: true, maskRefineMode: null,
    brushSize: 0.08, brushFeather: 0.65, brushFlow: 1,
    brushDensity: 1, brushAutoMask: false, brushTolerance: 0.18,
    healToolMode: 'remove', overlayHoverPoint: null, overlayHoverClientPoint: null, localPinsVisible: true,
    seq: 0, matchFactor: 1, clipboard: null,
    undo: [], redo: [], baseImg: null, gl: null,
    presentedRenderKey: null, presentedBackend: null,
    presentedGeometryKey: null,
    hasPresentedImage: false,
    renderState: 'empty', renderName: null, exif: {},
    zoom: 1, zoomMode: 'fit', targetPixelScale: null, panX: 0, panY: 0, cropping: false, cropRatio: 'free', cropLocked: false, cropAspectFlipped: false, cropCustomWidth: 3, cropCustomHeight: 2, holdBefore: false,
    compareActive: false, comparePosition: 0.5,
    activePane: 'editPane',
    healBrush: { radius: 0.04, feather: 0.65, opacity: 1 },
    rootFolder: '', folders: [], sources: [], activeFolder: '',
    catalogEnabled: false, catalogTotal: 0,
    activeFolders: {}, favoriteFolders: [],
    includeSubfolders: false, folderMode: 'browse',
    library: { collections: [], stacks: [], virtualCopies: [] },
    activeCollection: '',
    viewMode: 'detail', gridViewMode: 'square',
    msel: new Set(),
    grainBaselines: {},
    profiles: [], profileById: {}, rustAvailable: false,
    engineCapabilityKnown: false,
    provenance: null,
    referenceUrl: null, referenceStats: null,
    originalImageName: null,
    ai: {
      enabled: false, running: false, indexed: 0, errors: 0,
      completed: 0, total: 0, scanComplete: false, capabilities: {},
    },
    cull: {
      // Which culling criteria are ticked, which result set is being
      // reviewed, and a counter that retires the visible-list cache when
      // fresh verdicts arrive.
      on: {}, review: 'all', revision: 0,
    },
    rawDefault: null,
    pointColorPick: false, pointColorIndex: 0, colorGradeTone: 'shadows',
    maskColorPick: false,
    speed: null,
    softProof: { enabled: false, profile: 'srgb', paper: true, gamut: true },
  };
}
