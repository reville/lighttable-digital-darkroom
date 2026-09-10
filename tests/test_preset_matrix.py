"""Exhaustive test matrix for preset composition and Film Simulation interactions."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import server
import validation

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required")
class PresetMatrixTests(unittest.TestCase):
    def run_js(self, script):
        script = "import {t as tr, tn as trn} from './web/i18n.js';\n" + script
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_film_sim_matrix_on_off_preserve(self):
        """Test all combinations of Base Film (ON/OFF) and Preset filmMode (on/off/preserve) at 100%, 50%, 0%."""
        result = self.run_js("""
import {blendPresetState, reconcilePresetAdjustment, presetControlledSettings} from './web/preset-amount.js';
import {composePresetState} from './web/presets.js';

const presets = [
  {
    id: 'film-on', name: 'Film On Look', scope: 'look', filmMode: 'on', includeFilm: true,
    grade: {contrast: 0.25}, includedGrade: ['contrast'],
    params: {stock: 'ilford_hp5_plus', grain_amount: 1.2}, includedFilm: ['stock', 'grain_amount']
  },
  {
    id: 'film-off', name: 'Digital Clean Look', scope: 'look', filmMode: 'off', includeFilm: false,
    grade: {contrast: 0.15}, includedGrade: ['contrast'],
    params: {}
  },
  {
    id: 'film-preserve', name: 'Grade Only Look', scope: 'look', filmMode: 'preserve', includeFilm: false,
    grade: {contrast: 0.30}, includedGrade: ['contrast'],
    params: {}
  }
];

const matrix = {};

for (const baseFilmOn of [true, false]) {
  for (const preset of presets) {
    const key = `${baseFilmOn ? 'baseFilmOn' : 'baseFilmOff'}_${preset.id}`;
    const base = {
      params: {stock: 'kodak_portra_400', grain_amount: 0.8, profile_enabled: baseFilmOn},
      grade: {exposure: 0.0, contrast: 0.05},
      masks: [], heals: [], optics: {}
    };
    const target = composePresetState(base, preset);
    const controlled = presetControlledSettings(preset);
    const adj = {
      id: preset.id, name: preset.name, amount: 100, enabled: true,
      base: structuredClone(base), target: structuredClone(target),
      controlled
    };

    const at100 = blendPresetState(adj.base, adj.target, 100);
    const at50 = blendPresetState(adj.base, adj.target, 50);
    const at0 = blendPresetState(adj.base, adj.target, 0);

    const reconciled100 = reconcilePresetAdjustment(adj, at100);
    const reconciled50 = reconcilePresetAdjustment({...adj, amount: 50}, at50);
    const reconciled0 = reconcilePresetAdjustment({...adj, amount: 0}, at0);

    matrix[key] = {
      targetFilmEnabled: target.params.profile_enabled,
      at100FilmEnabled: at100.params.profile_enabled,
      at50FilmEnabled: at50.params.profile_enabled,
      at0FilmEnabled: at0.params.profile_enabled,
      reconciled100Valid: !!reconciled100,
      reconciled50Valid: !!reconciled50,
      reconciled0Valid: !!reconciled0,
      controlled
    };
  }
}

console.log(JSON.stringify(matrix));
""")
        # 1. Base Film OFF -> Preset Film ON
        m = result["baseFilmOff_film-on"]
        self.assertTrue(m["targetFilmEnabled"])
        self.assertTrue(m["at100FilmEnabled"])
        self.assertTrue(m["at50FilmEnabled"])
        self.assertFalse(m["at0FilmEnabled"])  # At 0%, restored to base film OFF
        self.assertTrue(m["reconciled100Valid"])
        self.assertTrue(m["reconciled50Valid"])
        self.assertTrue(m["reconciled0Valid"])

        # 2. Base Film ON -> Preset Film ON
        m = result["baseFilmOn_film-on"]
        self.assertTrue(m["targetFilmEnabled"])
        self.assertTrue(m["at100FilmEnabled"])
        self.assertTrue(m["at50FilmEnabled"])
        self.assertTrue(m["at0FilmEnabled"])  # At 0%, restored to base film ON
        self.assertTrue(m["reconciled100Valid"])
        self.assertTrue(m["reconciled50Valid"])
        self.assertTrue(m["reconciled0Valid"])

        # 3. Base Film ON -> Preset Film OFF
        m = result["baseFilmOn_film-off"]
        self.assertFalse(m["targetFilmEnabled"])
        self.assertFalse(m["at100FilmEnabled"])
        self.assertFalse(m["at50FilmEnabled"])
        self.assertTrue(m["at0FilmEnabled"])  # At 0%, restored to base film ON
        self.assertTrue(m["reconciled100Valid"])
        self.assertTrue(m["reconciled50Valid"])
        self.assertTrue(m["reconciled0Valid"])

        # 4. Base Film OFF -> Preset Film OFF
        m = result["baseFilmOff_film-off"]
        self.assertFalse(m["targetFilmEnabled"])
        self.assertFalse(m["at100FilmEnabled"])
        self.assertFalse(m["at50FilmEnabled"])
        self.assertFalse(m["at0FilmEnabled"])
        self.assertTrue(m["reconciled100Valid"])

        # 5. Base Film ON/OFF -> Preset Preserve
        self.assertTrue(result["baseFilmOn_film-preserve"]["targetFilmEnabled"])
        self.assertFalse(result["baseFilmOff_film-preserve"]["targetFilmEnabled"])

    def test_manual_edits_matrix_controlled_vs_uncontrolled(self):
        """Controlled edits retire preset even when baseline matches; uncontrolled edits preserve preset and amount."""
        result = self.run_js("""
import {blendPresetState, reconcilePresetAdjustment, presetControlledSettings} from './web/preset-amount.js';
import {composePresetState} from './web/presets.js';

const preset = {
  id: 'style-test', name: 'Style Test', scope: 'look', filmMode: 'on', includeFilm: true,
  grade: {contrast: 0.20, highlights: -0.10}, includedGrade: ['contrast', 'highlights'],
  params: {stock: 'ilford_hp5_plus', grain_amount: 1.0}, includedFilm: ['stock', 'grain_amount']
};
const controlled = presetControlledSettings(preset);

// Scenario 1: Photo baseline contrast DIFFERENT from preset (contrast: 0.0)
const baseDiff = {
  params: {stock: 'kodak_portra_400', grain_amount: 0.5, profile_enabled: true},
  grade: {exposure: 0.0, contrast: 0.0, highlights: 0.0},
  masks: [], heals: [], optics: {}
};
const targetDiff = composePresetState(baseDiff, preset);
const adjDiff = {
  id: preset.id, name: preset.name, amount: 100, enabled: true,
  base: structuredClone(baseDiff), target: structuredClone(targetDiff), controlled
};

// Scenario 2: Photo baseline contrast MATCHES preset (contrast: 0.20)
const baseMatch = {
  params: {stock: 'kodak_portra_400', grain_amount: 0.5, profile_enabled: true},
  grade: {exposure: 0.0, contrast: 0.20, highlights: 0.0},
  masks: [], heals: [], optics: {}
};
const targetMatch = composePresetState(baseMatch, preset);
const adjMatch = {
  id: preset.id, name: preset.name, amount: 100, enabled: true,
  base: structuredClone(baseMatch), target: structuredClone(targetMatch), controlled
};

// Test A: Edit controlled slider (contrast) on both
const editDiffContrast = structuredClone(targetDiff); editDiffContrast.grade.contrast = 0.35;
const editMatchContrast = structuredClone(targetMatch); editMatchContrast.grade.contrast = 0.35;
const retiredDiffContrast = reconcilePresetAdjustment(adjDiff, editDiffContrast);
const retiredMatchContrast = reconcilePresetAdjustment(adjMatch, editMatchContrast);

// Test B: Edit uncontrolled slider (exposure) on both
const editDiffExposure = structuredClone(targetDiff); editDiffExposure.grade.exposure = 0.5;
const keptDiffExposure = reconcilePresetAdjustment(adjDiff, editDiffExposure);

// Test C: Edit controlled film slider (grain_amount)
const editFilmControlled = structuredClone(targetDiff); editFilmControlled.params.grain_amount = 1.8;
const retiredFilmControlled = reconcilePresetAdjustment(adjDiff, editFilmControlled);

// Test D: Change stock
const editStock = structuredClone(targetDiff); editStock.params.stock = 'kodak_tri_x';
const retiredStock = reconcilePresetAdjustment(adjDiff, editStock);

console.log(JSON.stringify({
  diffContrastRetired: retiredDiffContrast === null,
  matchContrastRetired: retiredMatchContrast === null,
  exposureRetained: keptDiffExposure !== null,
  exposureBase: keptDiffExposure?.base.grade.exposure,
  exposureTarget: keptDiffExposure?.target.grade.exposure,
  filmControlledRetired: retiredFilmControlled === null,
  stockRetired: retiredStock === null
}));
""")
        self.assertTrue(result["diffContrastRetired"])
        # Crucial bug fix verification: baseline matching preset contrast MUST still retire on contrast edit!
        self.assertTrue(result["matchContrastRetired"])
        self.assertTrue(result["exposureRetained"])
        self.assertEqual(result["exposureBase"], 0.5)
        self.assertEqual(result["exposureTarget"], 0.5)
        self.assertTrue(result["filmControlledRetired"])
        self.assertTrue(result["stockRetired"])

    def test_film_simulation_toggle_matrix(self):
        """Toggling Film Sim retires on 'on' or 'off' presets, but preserves on 'preserve' presets."""
        result = self.run_js("""
import {reconcilePresetAdjustment, presetControlledSettings} from './web/preset-amount.js';
import {composePresetState} from './web/presets.js';

const filmOnPreset = {id: 'on', name: 'On', scope: 'look', filmMode: 'on', includeFilm: true,
  grade: {contrast: 0.1}, params: {stock: 'ilford_hp5_plus'}, includedGrade: ['contrast'], includedFilm: ['stock']};
const filmOffPreset = {id: 'off', name: 'Off', scope: 'look', filmMode: 'off', includeFilm: false,
  grade: {contrast: 0.1}, params: {}, includedGrade: ['contrast'], includedFilm: []};
const filmPreservePreset = {id: 'pres', name: 'Preserve', scope: 'look', filmMode: 'preserve', includeFilm: false,
  grade: {contrast: 0.1}, params: {}, includedGrade: ['contrast'], includedFilm: []};

// Case 1: Photo had film ON -> apply filmOnPreset -> user turns film OFF
const base1 = {params: {profile_enabled: true}, grade: {}, masks: [], heals: [], optics: {}};
const target1 = composePresetState(base1, filmOnPreset);
const adj1 = {id: 'on', name: 'On', amount: 100, enabled: true,
  base: base1, target: target1, controlled: presetControlledSettings(filmOnPreset)};
const userTurnedFilmOff1 = structuredClone(target1); userTurnedFilmOff1.params.profile_enabled = false;
const res1 = reconcilePresetAdjustment(adj1, userTurnedFilmOff1);

// Case 2: Photo had film OFF -> apply filmOnPreset -> user turns film OFF
const base2 = {params: {profile_enabled: false}, grade: {}, masks: [], heals: [], optics: {}};
const target2 = composePresetState(base2, filmOnPreset);
const adj2 = {id: 'on', name: 'On', amount: 100, enabled: true,
  base: base2, target: target2, controlled: presetControlledSettings(filmOnPreset)};
const userTurnedFilmOff2 = structuredClone(target2); userTurnedFilmOff2.params.profile_enabled = false;
const res2 = reconcilePresetAdjustment(adj2, userTurnedFilmOff2);

// Case 3: Apply filmOffPreset -> user turns film ON
const base3 = {params: {profile_enabled: true}, grade: {}, masks: [], heals: [], optics: {}};
const target3 = composePresetState(base3, filmOffPreset);
const adj3 = {id: 'off', name: 'Off', amount: 100, enabled: true,
  base: base3, target: target3, controlled: presetControlledSettings(filmOffPreset)};
const userTurnedFilmOn3 = structuredClone(target3); userTurnedFilmOn3.params.profile_enabled = true;
const res3 = reconcilePresetAdjustment(adj3, userTurnedFilmOn3);

// Case 4: Apply filmPreservePreset -> user turns film ON or OFF
const base4 = {params: {profile_enabled: true}, grade: {}, masks: [], heals: [], optics: {}};
const target4 = composePresetState(base4, filmPreservePreset);
const adj4 = {id: 'pres', name: 'Preserve', amount: 100, enabled: true,
  base: base4, target: target4, controlled: presetControlledSettings(filmPreservePreset)};
const userTurnedFilmOff4 = structuredClone(target4); userTurnedFilmOff4.params.profile_enabled = false;
const res4 = reconcilePresetAdjustment(adj4, userTurnedFilmOff4);

console.log(JSON.stringify({
  case1Retired: res1 === null,
  case2Retired: res2 === null,
  case3Retired: res3 === null,
  case4Retained: res4 !== null,
  case4BaseFilm: res4?.base.params.profile_enabled,
  case4TargetFilm: res4?.target.params.profile_enabled
}));
""")
        # User toggling film OFF on a film-on preset retires whether baseline was film ON or OFF
        self.assertTrue(result["case1Retired"])
        self.assertTrue(result["case2Retired"])
        # User toggling film ON on a digital look retires preset
        self.assertTrue(result["case3Retired"])
        # User toggling film ON/OFF on a preserve preset retains preset and updates film state
        self.assertTrue(result["case4Retained"])
        self.assertFalse(result["case4BaseFilm"])
        self.assertFalse(result["case4TargetFilm"])

    def test_paper_compatibility_normalization_prevents_synthetic_retirement(self):
        """Switching from color to b&w stock normalizes paper so UI paper sync does not retire preset."""
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {composePresetState} from './web/presets.js';
import {reconcilePresetAdjustment, presetControlledSettings} from './web/preset-amount.js';
import {normalizeFilmTuning, mergeFilmTuning} from './web/film-browser.js';

const profiles = [
  {id: 'kodak_portra_400', stage: 'filming', channelModel: 'rgb', type: 'negative'},
  {id: 'kodak_trix', stage: 'filming', channelModel: 'cmy', type: 'positive', targetPrint: 'kodak_2302'},
  {id: 'kodak_portra_endura', stage: 'printing', channelModel: 'rgb'},
  {id: 'kodak_2302', stage: 'printing', channelModel: 'cmy'}
];

const appSource = readFileSync('./web/app.js', 'utf8');
const runtime = vm.createContext({
  normalizeFilmTuning, mergeFilmTuning,
  S: {profiles, filmDefaults: {stock: 'kodak_portra_400', paper: 'kodak_portra_endura'}},
  grainBaseline: () => 0.2,
  $: () => ({value: ''}),
  tr: (s) => s,
});
vm.runInContext(appSource.match(/^function normalizeFilmParams\\([^]*?^}/m)[0], runtime);
vm.runInContext(appSource.match(/^function mergeFilmParams\\([^]*?^}/m)[0], runtime);

const baseColor = {
  params: {stock: 'kodak_portra_400', paper: 'kodak_portra_endura', profile_enabled: true},
  grade: {contrast: 0.1}, masks: [], heals: [], optics: {}
};

const bwPreset = {
  id: 'bw', name: 'BW Look', scope: 'look', filmMode: 'on', includeFilm: true,
  grade: {contrast: 0.2}, includedGrade: ['contrast'],
  params: {stock: 'kodak_trix'}, includedFilm: ['stock']
};

const targetBw = composePresetState(baseColor, bwPreset, {
  normalizeFilmParams: runtime.normalizeFilmParams,
  mergeFilmParams: runtime.mergeFilmParams
});

const adj = {
  id: 'bw', name: 'BW Look', amount: 100, enabled: true,
  base: structuredClone(baseColor), target: structuredClone(targetBw),
  controlled: presetControlledSettings(bwPreset)
};

// Simulate what populatePaperOptions does:
const normalizedPaper = targetBw.params.paper;
const reconciled = reconcilePresetAdjustment(adj, targetBw);

console.log(JSON.stringify({
  normalizedPaper,
  reconciledValid: !!reconciled
}));
""")
        self.assertEqual(result["normalizedPaper"], "kodak_2302")
        self.assertTrue(result["reconciledValid"])

    def test_validation_clean_state_preserves_controlled_metadata(self):
        """Validation cleanly preserves 'controlled' metadata and survives reload."""
        state = {
            "preset": {
                "id": "lighttable/classic-bw",
                "name": "Classic B&W",
                "amount": 75,
                "enabled": True,
                "controlled": {
                    "grade": ["contrast", "highlights"],
                    "film": ["stock", "grain_amount"],
                    "filmMode": "on"
                },
                "base": {"params": {}, "grade": {"contrast": 0.1}, "masks": [], "heals": [], "optics": {}},
                "target": {"params": {}, "grade": {"contrast": 0.3}, "masks": [], "heals": [], "optics": {}}
            }
        }
        cleaned, warnings = server.cleaned_state_request(state, strict=True)
        self.assertEqual(warnings, [])
        preset = cleaned["preset"]
        self.assertEqual(preset["controlled"]["grade"], ["contrast", "highlights"])
        self.assertEqual(preset["controlled"]["film"], ["stock", "grain_amount"])
        self.assertEqual(preset["controlled"]["filmMode"], "on")

    def test_sequential_presets_and_baseline_restoration(self):
        """Applying Preset A then Preset B isolates baseline cleanly without stacking and toggles off cleanly."""
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {composePresetState} from './web/presets.js';
import {presetEditState, blendPresetState, reconcilePresetAdjustment, presetControlledSettings} from './web/preset-amount.js';

const source = readFileSync('./web/app.js', 'utf8');

const S = {
  editingName: 'photo1',
  params: {stock: 'kodak_portra_400', profile_enabled: false},
  grade: {exposure: 0.2, contrast: 0.05, saturation: 0.0},
  masks: [], heals: [], optics: {}, preset: null
};

let photoName = 'photo1', LAST_PRESET_APPLICATION = null;
const cur = () => ({name: photoName}), cloneValue = structuredClone, presetKey = p => p.id;
const history = []; const pushUndo = () => history.push(structuredClone(S));
const readControls = () => {};
const presetHasApplicableSettings = () => true;
const editId = p => p;
const normalizeFilmParams = p => p;
const mergeFilmParams = (a, b) => ({...a, ...b});
const filmRenderFingerprint = () => JSON.stringify(S.params);
const baseEditsFingerprint = () => JSON.stringify([S.optics, S.heals]);
const syncControls = () => {};
const syncGrade = () => {};
const syncCurveFromGrade = () => {};
const syncHsl = () => {};
const syncMaskPanel = () => {};
const syncHealPanel = () => {};
const syncOpticsPanel = () => {};
const drawGrade = () => {};
const saveState = () => {};
const renderFilm = () => {};
const refreshBaseEdits = () => {};
const markContinuousInput = () => {};

const presetA = {
  id: 'preset-a', name: 'Preset A', scope: 'look', filmMode: 'on', includeFilm: true,
  grade: {contrast: 0.4}, includedGrade: ['contrast'],
  params: {stock: 'ilford_hp5_plus'}, includedFilm: ['stock']
};

const presetB = {
  id: 'preset-b', name: 'Preset B', scope: 'look', filmMode: 'off', includeFilm: false,
  grade: {contrast: -0.2}, includedGrade: ['contrast'],
  params: {}, includedFilm: []
};

const exercise = source.slice(source.indexOf('let presetAmountGesture ='), source.indexOf('function presetPhotoSnapshot()')) + `
// Apply A
toggleBrowserPreset(presetA, {name: 'photo1'});
const stateAfterA = structuredClone(S);

// Apply B while A is active
toggleBrowserPreset(presetB, {name: 'photo1'});
const stateAfterB = structuredClone(S);

// Reconcile and drag Amount to 50%
changePresetAmount('preset-b', 'photo1', 50);
const stateAfterB50 = structuredClone(S);

// Toggle B off
toggleBrowserPreset(presetB, {name: 'photo1'});
const stateAfterBOff = structuredClone(S);

console.log(JSON.stringify({
  aFilm: stateAfterA.params.profile_enabled,
  aStock: stateAfterA.params.stock,
  aContrast: stateAfterA.grade.contrast,
  bFilm: stateAfterB.params.profile_enabled,
  bStock: stateAfterB.params.stock,
  bContrast: stateAfterB.grade.contrast,
  b50Contrast: stateAfterB50.grade.contrast,
  offFilm: stateAfterBOff.params.profile_enabled,
  offStock: stateAfterBOff.params.stock,
  offContrast: stateAfterBOff.grade.contrast,
  offExposure: stateAfterBOff.grade.exposure
}));`;
eval(exercise);
""")
        # A turned film on, stock hp5, contrast 0.4
        self.assertTrue(result["aFilm"])
        self.assertEqual(result["aStock"], "ilford_hp5_plus")
        self.assertAlmostEqual(result["aContrast"], 0.4)

        # B switched film off, base stock portra preserved (not contaminated by A), contrast -0.2
        self.assertFalse(result["bFilm"])
        self.assertEqual(result["bStock"], "kodak_portra_400")
        self.assertAlmostEqual(result["bContrast"], -0.2)

        # B at 50% amounts correctly: midpoint between base (0.05) and target (-0.2) is -0.075
        self.assertAlmostEqual(result["b50Contrast"], -0.075)

        # Toggling B off returns cleanly to original photo baseline
        self.assertFalse(result["offFilm"])
        self.assertEqual(result["offStock"], "kodak_portra_400")
        self.assertAlmostEqual(result["offContrast"], 0.05)
        self.assertAlmostEqual(result["offExposure"], 0.2)

    def test_preset_card_dots_svg_and_uniform_hover_styling(self):
        """Card styling enforces transparent child buttons on hover/active and info button uses vertically centered SVG."""
        result = self.run_js("""
import {readFileSync} from 'node:fs';

const css = readFileSync('./web/preset-browser.css', 'utf8');
const js = readFileSync('./web/preset-browser.js', 'utf8');

const hasTransparentRule = css.includes('.preset-browser-card button:is(.preset-browser-apply, .preset-browser-favorite, .preset-browser-info)') &&
  css.includes('background:transparent !important');

const hasUnifiedActiveHover = css.includes('.preset-browser-card.is-active, .preset-browser-card.is-active:hover');

const hasCenteredSvg = js.includes('viewBox="0 0 16 16"') &&
  js.includes('cy="8"');

console.log(JSON.stringify({
  hasTransparentRule,
  hasUnifiedActiveHover,
  hasCenteredSvg
}));
""")
        self.assertTrue(result["hasTransparentRule"])
        self.assertTrue(result["hasUnifiedActiveHover"])
        self.assertTrue(result["hasCenteredSvg"])


if __name__ == "__main__":
    unittest.main()
