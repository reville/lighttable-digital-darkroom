"""Exercise the app's actual tool handlers with a small deterministic DOM."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
import {t as tr, tn as trn} from './web/i18n.js';
import {createAppState, cloneValue} from './web/state.js';
import {OPTICS_DEFAULTS, MAX_HEALS, normalizeMasks, normalizeHeals, normalizeOptics, localToolLabel} from './web/editor-panels.js';
import {cropGeometry, restoreCropGeometry} from './web/edit-transfer.js';
import {clampComparePosition, compareViewGeometry, comparePositionAtViewCenter} from './web/compare-view.js';
const S = createAppState({}, OPTICS_DEFAULTS);
Object.assign(S, {params:{rotate:0}, editingName:'a', viewMode:'detail'});
let photo = {name:'a', width:1200, height:800};
const cur = () => photo;
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const noop = () => {};
const counts = {undo:0, save:0, render:0, refresh:0, closeExport:0};
const notices = [];
const toast = message => notices.push(message);
const pushUndo = () => counts.undo++;
const saveState = () => counts.save++;
const renderFilm = () => counts.render++;
const refreshBaseEdits = () => counts.refresh++;
const syncCropPresentationNow = noop, zoomReset = noop, applyView = noop, applyViewNow = noop;
const syncOpticsPanel = noop, syncMaskPanel = noop, syncControls = noop;
const syncGrade = noop, drawGrade = noop, syncCurveFromGrade = noop, syncHsl = noop;
const renderKeywords = noop, renderVersions = noop, refreshLists = noop, updateUndoRedoButtons = noop;
const normalizeFilmParams = value => cloneValue(value);
const serializableMasks = () => cloneValue(S.masks);
const drawEditOverlay = noop, scheduleNativeMenuState = noop, savePrefs = noop;
const renderEditItems = noop, syncOverlayCursorClass = noop;
const nativePreviewActive = () => false;
const syncBrowserOriginal = noop, requestedPreviewWidth = () => 1200;
const PRESET_BROWSER = null, HISTORY = null, METADATA = null;
let SURVEY = null;
const KEYS = {speed:{}, pick:[], reject:[], unflag:[], crop:'r', compare:'\\'};
const LABEL_KEYS = {};
const selectedHeal = () => S.heals.find(spot => spot.id === S.selectedHealId) || null;
const selectedMask = () => null;
const overlayPoint = () => [0.5, 0.5];
const healHandleAt = () => null;
const editId = () => 'new-heal';
const automaticHealSource = () => [0.6, 0.6];
const frames = [];
const requestAnimationFrame = run => { frames.push(run); return frames.length; };
function drainFrames() { const next = frames.splice(0); next.forEach(run => run()); }
class HTMLDetailsElement {}
function makeNode(id, tagName = 'DIV', type = '') {
  const classes = new Set(), attributes = {};
  return {id, tagName, type, attributes, dataset:{}, handlers:{}, value:'',
    hidden:false, disabled:false, scrollTop:0, textContent:'', parentElement:null,
    style:{setProperty:noop, removeProperty:noop},
    classList:{contains:key => classes.has(key), add:key => classes.add(key),
      remove:key => classes.delete(key),
      toggle(key, on) { if (on === undefined) on = !classes.has(key);
        if (on) classes.add(key); else classes.delete(key); return on; }},
    setAttribute(key, value) { attributes[key] = value; }, removeAttribute:noop,
    addEventListener(type, run) { (this.handlers[type] ||= []).push(run); },
    dispatch(type, event={}) { for (const run of this.handlers[type] || []) run(event); },
    focus() { document.activeElement = this; },
    blur() { this.blurred = true; if (this.onblur) this.onblur(); document.activeElement = document.body; },
    scrollIntoView:noop, setPointerCapture:noop,
    getBoundingClientRect:() => ({width:1200,height:800,left:0,top:0}),
    closest(selector) {
      for (let node = this; node; node = node.parentElement) {
        if (selector.split(',').some(s => {
          s=s.trim();
          return s === '#' + node.id || (s.startsWith('.') &&
            s.slice(1).split('.').every(key => node.classList.contains(key)));
        })) return node;
      }
      return null;
    },
  };
}
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, makeNode(id)); return nodes.get(id); };
const paneNames = ['editPane','filmPane','cropPane','maskPane','healPane','historyPane','infoPane','presetsPane'];
const panes = paneNames.map($);
panes.forEach(pane => pane.classList.add('panel-pane'));
const buttons = paneNames.map(id => Object.assign(makeNode(id+'Button','BUTTON'), {dataset:{pane:id}}));
const backButtons = [makeNode('cropBack','BUTTON'),makeNode('maskBack','BUTTON'),makeNode('healBack','BUTTON')];
const document = {
  body:makeNode('body','BODY'), activeElement:null, handlers:{},
  addEventListener(type, run) { this.handlers[type] = run; },
  querySelectorAll(selector) {
    return ({'.panel-pane':panes,'.tool-btn':buttons,'[data-exit-tool]':backButtons})[selector] || [];
  },
  querySelector(selector) { return buttons.find(button => selector.includes('"'+button.dataset.pane+'"')) || null; },
};
for (const id of ['cropCustomWidth','cropCustomHeight']) {
  Object.assign($(id), {tagName:'INPUT',type:'number',parentElement:$('cropPane')});
}
for (const id of ['healRadius','healFeather','healOpacity']) {
  Object.assign($(id), {tagName:'INPUT',type:'range',parentElement:$('healPane')});
}
Object.assign($('cv'), {width:1200,height:800});
const closeExportModal = () => { counts.closeExport++; $('exportDialog').classList.remove('on'); };
function applyCropVisual() { syncCropPanel(); }
function clickPane(id) { buttons.find(button => button.dataset.pane === id).onclick(); drainFrames(); }
function press(key, target = document.body) {
  const event = {key, code:key, target, defaultPrevented:false, metaKey:false,
    ctrlKey:false, shiftKey:false, altKey:false, repeat:false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation:noop};
  document.handlers.keydown(event); drainFrames(); return event.defaultPrevented;
}
"""


@unittest.skipUnless(shutil.which('node'), 'Node required')
class PhotoToolFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / 'web/app.js').read_text()

        def function(name):
            match = re.search(rf'^function {name}\(.*?^\}}', source, re.M | re.S)
            if not match:
                raise AssertionError(f'Missing function {name}')
            return match.group(0)

        def section(start, end):
            begin = source.index(start)
            return source[begin:source.index(end, begin)]

        functions = [function(name) for name in (
            'snapshot', 'restore', 'filmRenderFingerprint', 'baseEditsFingerprint',
            'exitPhotoTool', 'selectPhotoTool', 'switchPane', 'setCropMode',
            'cropViewState', 'cropViewPrefersImmediate', 'cropViewFrame', 'cropFitScale',
            'cropViewTarget', 'cropViewBackgroundRGB', 'cropViewDimStyle', 'snapCropView',
            'stepCropView', 'applyCropView', 'syncCropView', 'finishCropViewExit',
            'cropViewTransition', 'cropViewWrapKey',
            'cropSourceSize', 'cropImageAspect', 'cropOutputRatio', 'cropLayerRatio',
            'clampCrop', 'previewCrop', 'previewSourceX', 'cropForRatio', 'syncCropPanel',
            'restoreCropChoices', 'rememberCropChoices', 'applyCropRatioChoice', 'setCropRatio',
            'renderedComparePosition', 'compareEditingBlocked', 'syncCompareControl',
            'syncCompareView', 'snapCompareToView', 'renderCompare', 'setCompareActive', 'syncHealPanel',
        )]
        cls.script = HARNESS + "\n" + section('const paneScrollPositions', 'function exitPhotoTool')
        cls.script += '\n' + '\n'.join(functions)
        cls.script += '\n' + section('let compareFrame = null;', '(function compareDrag()')
        cls.script += '\n' + section("document.querySelectorAll('.tool-btn').forEach((b)", "$('aiToggle').onclick")
        cls.script += '\n' + section("$('cropDone').onclick", 'function rotate(delta)')
        cls.script += '\n' + section("$('cropRatio').addEventListener", '/* ------------------------------------------------------- multi-select */')
        cls.script += '\n' + section("for (const id of ['healRadius', 'healFeather', 'healOpacity'])", "for (const id of ['lensProfileEnabled'")
        cls.script += '\n' + section("$('lensReset').onclick", 'function overlayPoint(')
        cls.script += '\n' + section("$('editOverlay').addEventListener('pointerdown'", "$('editOverlay').addEventListener('pointermove'")
        cls.script += '\n' + section("document.addEventListener('keydown', (e) => {", "document.addEventListener('keyup', (e) => {")

    def run_js(self, body):
        result = subprocess.run(['node', '--input-type=module', '-e', self.script + '\n' + body],
                                cwd=ROOT, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_tool_buttons_enter_directly_and_toggle_back_to_last_adjustments(self):
        result = self.run_js("""
const cases = [];
for (const home of ['editPane', 'filmPane']) {
  clickPane(home);
  for (const tool of ['cropPane', 'maskPane', 'healPane']) {
    clickPane(tool);
    const entry = {pane:S.activePane,crop:S.cropping,
      overlay:$('editOverlay').classList.contains('active')};
    clickPane(tool);
    cases.push({home,tool,entry,exit:S.activePane,cropAfter:S.cropping,
      back:backButtons[0].textContent});
  }
}
console.log(JSON.stringify(cases));
""")
        for case in result:
            self.assertEqual(case['entry']['pane'], case['tool'])
            self.assertEqual(case['entry']['crop'], case['tool'] == 'cropPane')
            self.assertEqual(case['entry']['overlay'], case['tool'] in ('maskPane', 'healPane'))
            self.assertEqual(case['exit'], case['home'])
            self.assertFalse(case['cropAfter'])
            self.assertEqual(case['back'], 'Back to Film' if case['home'] == 'filmPane' else 'Back to Edit')

    def test_cancel_restores_entry_geometry_and_keeps_other_edits_and_history(self):
        result = self.run_js("""
S.crop={x:.1,y:.2,w:.7,h:.6}; S.params.rotate=90; S.optics.rotate=2;
S.grade.exposure=.5; photo.rating=4; counts.undo=2;
const before=JSON.stringify(cropGeometry(JSON.parse(snapshot())));
clickPane('cropPane'); S.crop={x:.3,y:.3,w:.4,h:.4}; S.params.rotate=180;
S.optics.rotate=-4; S.optics.distortion=.25; S.grade.exposure=1.2;
// Temporarily showing the original must not reset the crop session entry.
setCompareActive(true); setCompareActive(false);
press('Escape');
console.log(JSON.stringify({same:before===JSON.stringify(cropGeometry(JSON.parse(snapshot()))),
  exposure:S.grade.exposure,distortion:S.optics.distortion,rating:photo.rating,
  undo:counts.undo,save:counts.save,pane:S.activePane}));
""")
        self.assertTrue(result['same'])
        self.assertEqual(result['exposure'], 1.2)
        self.assertEqual(result['distortion'], .25)
        self.assertEqual(result['rating'], 4)
        self.assertEqual(result['undo'], 3)
        self.assertGreater(result['save'], 0)
        self.assertEqual(result['pane'], 'editPane')

    def test_done_and_enter_accept_but_cancel_button_restores(self):
        result = self.run_js("""
const values=[];
for (const action of ['done','enter','cancel']) {
  S.crop=null; S.params.rotate=0; clickPane('cropPane');
  S.crop={x:.2,y:.2,w:.6,h:.6}; S.params.rotate=90;
  if(action==='done') $('cropDone').onclick();
  else if(action==='cancel') $('cropCancel').onclick();
  else press('Enter');
  values.push({action,crop:S.crop,rotate:S.params.rotate,pane:S.activePane});
}
console.log(JSON.stringify(values));
""")
        for case in result:
            self.assertEqual(case['pane'], 'editPane')
            if case['action'] == 'cancel':
                self.assertIsNone(case['crop'])
                self.assertEqual(case['rotate'], 0)
            else:
                self.assertEqual(case['crop']['x'], .2)
                self.assertEqual(case['rotate'], 90)

    def test_compare_temporarily_suspends_and_restores_each_tool(self):
        result = self.run_js("""
const cases=[];
clickPane('filmPane');
for (const tool of ['cropPane','maskPane','healPane']) {
  clickPane(tool); S.crop={x:0.2,y:0.1,w:0.5,h:0.6};
  const original=JSON.stringify(S.crop);
  setCompareActive(true); drainFrames();
  const during={pane:S.activePane,crop:S.cropping,compare:S.compareActive,
    overlay:$('editOverlay').classList.contains('active'),back:!$('compareReturn').hidden};
  setCompareActive(false); drainFrames();
  cases.push({tool,during,after:S.activePane,compare:S.compareActive,
    unchanged:original===JSON.stringify(S.crop)});
  clickPane('filmPane');
}
console.log(JSON.stringify(cases));
""")
        for case in result:
            self.assertEqual(case['during'], {'pane': 'filmPane', 'crop': False, 'compare': True,
                                               'overlay': False, 'back': True})
            self.assertEqual(case['after'], case['tool'])
            self.assertFalse(case['compare'])
            self.assertTrue(case['unchanged'])

    def test_selecting_a_pane_while_comparing_discards_the_tool_return(self):
        result = self.run_js("""
clickPane('cropPane'); setCompareActive(true); clickPane('healPane');
const chosen={pane:S.activePane,compare:S.compareActive,back:$('compareReturn').hidden};
clickPane('filmPane'); setCompareActive(true); setCompareActive(false);
console.log(JSON.stringify({chosen,final:S.activePane}));
""")
        self.assertEqual(result['chosen'], {'pane': 'healPane', 'compare': False, 'back': True})
        self.assertEqual(result['final'], 'filmPane')

    def test_enter_and_escape_exit_tools_and_compare_returns_before_exiting(self):
        result = self.run_js("""
const exits=[];
for (const key of ['Enter','Escape']) for (const tool of ['cropPane','maskPane','healPane']) {
  clickPane('filmPane'); clickPane(tool);
  exits.push({key,tool,handled:press(key),pane:S.activePane});
}
clickPane('cropPane'); setCompareActive(true);
press('Escape'); const first={pane:S.activePane,compare:S.compareActive};
press('Escape');
console.log(JSON.stringify({exits,first,second:S.activePane}));
""")
        for case in result['exits']:
            self.assertTrue(case['handled'])
            self.assertEqual(case['pane'], 'filmPane')
        self.assertEqual(result['first'], {'pane': 'cropPane', 'compare': False})
        self.assertEqual(result['second'], 'filmPane')

    def test_numeric_and_range_keys_commit_before_exit_but_text_is_untouched(self):
        result = self.run_js("""
const cases=[];
for (const [pane,id] of [['cropPane','cropCustomWidth'],['healPane','healRadius']]) {
  for (const key of ['Enter','Escape']) {
    clickPane(pane); const input=$(id); let committedInPane=null;
    input.onblur=()=>{committedInPane=S.activePane;};
    const handled=press(key,input);
    cases.push({key,handled,committedInPane,pane:S.activePane});
  }
}
clickPane('maskPane'); const text=makeNode('maskName','INPUT','text');
const textHandled=press('Escape',text);
console.log(JSON.stringify({cases,textHandled,textPane:S.activePane}));
""")
        for case in result['cases']:
            self.assertTrue(case['handled'])
            self.assertIn(case['committedInPane'], ['cropPane', 'healPane'])
            self.assertEqual(case['pane'], 'editPane')
        self.assertFalse(result['textHandled'])
        self.assertEqual(result['textPane'], 'maskPane')

    def test_escape_closes_modal_and_survey_before_the_active_tool(self):
        result = self.run_js("""
clickPane('cropPane'); $('exportDialog').classList.add('on');
const exportSize=makeNode('exportSize','INPUT','number'); exportSize.parentElement=$('exportDialog');
const numeric={handled:press('Escape',exportSize),pane:S.activePane,
  modalStillOpen:$('exportDialog').classList.contains('on')};
press('Escape'); const modal={closed:counts.closeExport,pane:S.activePane};
let closedSurvey=0; SURVEY={isOpen:true,close(){closedSurvey++;this.isOpen=false;}};
press('Escape');
console.log(JSON.stringify({numeric,modal,survey:{closed:closedSurvey,pane:S.activePane}}));
""")
        self.assertEqual(result['numeric'], {'handled': False, 'pane': 'cropPane', 'modalStillOpen': True})
        self.assertEqual(result['modal'], {'closed': 1, 'pane': 'cropPane'})
        self.assertEqual(result['survey'], {'closed': 1, 'pane': 'cropPane'})

    def test_crop_reset_keeps_tool_open_and_resets_composition_only(self):
        result = self.run_js("""
clickPane('cropPane'); S.crop={x:0.1,y:0.1,w:0.8,h:0.8}; S.params.rotate=90;
Object.assign(S.optics,{rotate:4,vertical:5,horizontal:6,scale:1.1,
  flipHorizontal:true,flipVertical:true,distortion:0.25,profileEnabled:true});
syncCropPanel(); $('cropReset').onclick();
console.log(JSON.stringify({pane:S.activePane,cropping:S.cropping,crop:S.crop,
  rotate:S.params.rotate,optics:S.optics,ratio:S.cropRatio,locked:S.cropLocked,counts}));
""")
        self.assertEqual(result['pane'], 'cropPane')
        self.assertTrue(result['cropping'])
        self.assertIsNone(result['crop'])
        self.assertEqual(result['rotate'], 0)
        self.assertEqual(result['optics'], {'rotate': 0, 'vertical': 0, 'horizontal': 0, 'scale': 1,
                                             'flipHorizontal': False, 'flipVertical': False,
                                             'distortion': 0.25, 'profileOverride': None, 'profileEnabled': True,
                                             'profileDistortion': True, 'profileVignette': True,
                                             'vignette': 0})
        self.assertEqual((result['ratio'], result['locked']), ('free', False))
        self.assertEqual(result['counts']['save'], 1)

    def test_lens_reset_preserves_crop_geometry_and_other_grade_adjustments(self):
        result = self.run_js("""
clickPane('editPane');
S.crop={x:0.1,y:0.2,w:0.7,h:0.6}; S.params.rotate=90;
Object.assign(S.optics,{rotate:4,vertical:5,horizontal:6,scale:1.1,
  flipHorizontal:true,flipVertical:true,distortion:0.25,vignette:0.3,
  profileEnabled:true,profileDistortion:false,profileVignette:false});
Object.assign(S.grade,{exposure:0.8,contrast:0.2,
  chromaticAberrationRedCyan:0.4,chromaticAberrationBlueYellow:-0.3});
let propagationStopped=false;
$('lensReset').onclick({stopPropagation(){propagationStopped=true;}});
console.log(JSON.stringify({pane:S.activePane,crop:S.crop,rotate:S.params.rotate,
  optics:S.optics,grade:S.grade,propagationStopped,counts}));
""")
        self.assertEqual(result['pane'], 'editPane')
        self.assertEqual(result['crop'], {'x': 0.1, 'y': 0.2, 'w': 0.7, 'h': 0.6})
        self.assertEqual(result['rotate'], 90)
        self.assertEqual(result['optics'], {
            'rotate': 4, 'vertical': 5, 'horizontal': 6, 'scale': 1.1,
            'flipHorizontal': True, 'flipVertical': True,
            'distortion': 0, 'vignette': 0, 'profileOverride': None, 'profileEnabled': False,
            'profileDistortion': True, 'profileVignette': True,
        })
        self.assertEqual(result['grade'], {'exposure': 0.8, 'contrast': 0.2})
        self.assertTrue(result['propagationStopped'])
        self.assertEqual(result['counts']['undo'], 1)
        self.assertEqual(result['counts']['save'], 1)
        self.assertEqual(result['counts']['refresh'], 1)

    def test_custom_ratio_orientation_and_lock_preserve_valid_geometry(self):
        result = self.run_js("""
clickPane('cropPane'); S.crop={x:0.3,y:0.3,w:0.4,h:0.4};
S.cropRatio='custom'; $('cropCustomWidth').value='3'; $('cropCustomHeight').value='2';
$('cropCustomWidth').dispatch('change');
const original={crop:{...S.crop},ratio:cropOutputRatio()};
$('cropSwap').onclick(); const swapped={crop:{...S.crop},ratio:cropOutputRatio()};
$('cropLock').onclick();
S.crop={x:0.2,y:0.25,w:0.5,h:0.35}; const free=JSON.stringify(S.crop);
$('cropLock').onclick();
const locked={unchanged:free===JSON.stringify(S.crop),ratio:cropOutputRatio()};
const beforeInvalid=JSON.stringify(S.crop); $('cropCustomWidth').value='0';
$('cropCustomWidth').dispatch('change');
console.log(JSON.stringify({original,swapped,locked,
  invalidUnchanged:beforeInvalid===JSON.stringify(S.crop),notices}));
""")
        self.assertAlmostEqual(result['original']['ratio'], 1.5)
        self.assertAlmostEqual(result['swapped']['ratio'], 2 / 3)
        for view in ('original', 'swapped'):
            crop = result[view]['crop']
            self.assertAlmostEqual(1.5 * crop['w'] / crop['h'], result[view]['ratio'])
            self.assertAlmostEqual(crop['x'] + crop['w'] / 2, 0.5)
            self.assertAlmostEqual(crop['y'] + crop['h'] / 2, 0.5)
            self.assertAlmostEqual(crop['w'] * crop['h'], 0.16)
            self.assertGreaterEqual(crop['x'], 0)
            self.assertLessEqual(crop['x'] + crop['w'], 1)
        self.assertTrue(result['locked']['unchanged'])
        self.assertAlmostEqual(result['locked']['ratio'], 1.5 * 0.5 / 0.35, places=4)
        self.assertTrue(result['invalidUnchanged'])
        self.assertEqual(len(result['notices']), 1)

    def test_portrait_source_ignores_stale_canvas_for_original_and_square(self):
        result = self.run_js("""
photo.width=800; photo.height=1200;
$('cv').width=1200; $('cv').height=800;
clickPane('cropPane'); setCropRatio('original');
const original={ratio:cropOutputRatio(),crop:{...S.crop}};
setCropRatio('1');
console.log(JSON.stringify({original,square:{...S.crop},size:cropSourceSize()}));
""")
        self.assertAlmostEqual(result['original']['ratio'], 2 / 3)
        self.assertEqual(result['original']['crop'], {'x': 0, 'y': 0, 'w': 1, 'h': 1})
        self.assertAlmostEqual((2 / 3) * result['square']['w'] / result['square']['h'], 1)
        self.assertEqual(result['size'], {'width': 800, 'height': 1200})

    def test_requested_rotation_sets_crop_geometry_before_the_canvas_updates(self):
        result = self.run_js("""
photo.width=800; photo.height=1200; S.params.rotate=90;
$('cv').width=800; $('cv').height=1200;
clickPane('cropPane'); setCropRatio('original');
console.log(JSON.stringify({ratio:cropOutputRatio(),crop:S.crop,size:cropSourceSize()}));
""")
        self.assertAlmostEqual(result['ratio'], 1.5)
        self.assertEqual(result['crop'], {'x': 0, 'y': 0, 'w': 1, 'h': 1})
        self.assertEqual(result['size'], {'width': 1200, 'height': 800})

    def test_brush_defaults_are_editable_before_first_correction_and_used_on_photo(self):
        result = self.run_js("""
clickPane('healPane');
const before={radius:$('healRadius').value,feather:$('healFeather').value,
  opacity:$('healOpacity').value,selectionHidden:$('healControls').hidden};
for (const [id,value] of [['healRadius','0.12'],['healFeather','0.25'],['healOpacity','0.7']]) {
  $(id).value=value; $(id).dispatch('pointerdown'); $(id).dispatch('input'); $(id).dispatch('change');
}
const configured={...S.healBrush}; const savesBefore=counts.save;
$('editOverlay').dispatch('pointerdown',{button:0,pointerId:1,
  preventDefault:noop,stopPropagation:noop});
console.log(JSON.stringify({before,configured,savesBefore,spot:S.heals[0],undo:counts.undo}));
""")
        self.assertEqual(result['before'], {'radius': 0.04, 'feather': 0.65, 'opacity': 1, 'selectionHidden': True})
        self.assertEqual(result['configured'], {'radius': 0.12, 'feather': 0.25, 'opacity': 0.7})
        self.assertEqual(result['savesBefore'], 0)
        for key, value in result['configured'].items():
            self.assertEqual(result['spot'][key], value)
        self.assertEqual(result['undo'], 1)


if __name__ == '__main__':
    unittest.main()
