"""Preset amounts preserve a baseline and survive real state serialization."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import catalog
import catalog_scan
import server
import preset_library

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required")
class PresetAmountTests(unittest.TestCase):
    def run_js(self, script):
        script = "import {t as tr, tn as trn} from './web/i18n.js';\n" + script
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_endpoints_midpoint_and_input_immutability(self):
        result = self.run_js("""
import {blendPresetState} from './web/preset-amount.js';
const base={params:{profile_enabled:false,stock:'base',grain_amount:0.2},
  grade:{exposure:0.7,contrast:0.1,hsl:{red:{h:0.1,s:0.2}}},
  masks:[{id:'existing',opacity:0.6}],heals:[],optics:{scale:1}};
const target=structuredClone(base);
target.params={profile_enabled:true,stock:'new',grain_amount:0.8};
Object.assign(target.grade,{contrast:0.5,curveL:Array.from({length:256},(_,i)=>(i/255)**2),
  hsl:{red:{h:0.3,s:0.6}},colorGrading:{shadows:{hue:240,saturation:0.4}},
  pointColor:[{hue:150,range:30,hueShift:20,saturation:0.6}]});
target.masks.push({id:'added',opacity:0.8});
const before=JSON.stringify([base,target]),half=blendPresetState(base,target,50);
console.log(JSON.stringify({base,target,half,zero:blendPresetState(base,target,0),
  full:blendPresetState(base,target,100),unchanged:before===JSON.stringify([base,target])}));
""")
        self.assertEqual(result["zero"], result["base"])
        self.assertEqual(result["full"], result["target"])
        self.assertTrue(result["unchanged"])
        half = result["half"]
        self.assertAlmostEqual(half["grade"]["contrast"], 0.3)
        self.assertEqual(half["grade"]["exposure"], 0.7)
        self.assertAlmostEqual(half["grade"]["hsl"]["red"]["s"], 0.4)
        self.assertAlmostEqual(half["grade"]["curveL"][128], (128/255+(128/255)**2)/2)
        self.assertEqual(half["grade"]["colorGrading"]["shadows"], {"hue": 240, "saturation": 0.2})
        self.assertEqual(half["grade"]["pointColor"][0]["hue"], 150)
        self.assertEqual(half["grade"]["pointColor"][0]["hueShift"], 10)
        self.assertEqual(half["masks"][0]["opacity"], 0.6)
        self.assertEqual(half["masks"][1]["opacity"], 0.4)
        self.assertEqual(half["params"]["stock"], "new")

    def test_unrelated_manual_edits_survive_but_controlled_edits_retire_amount(self):
        result = self.run_js("""
import {blendPresetState,reconcilePresetAdjustment} from './web/preset-amount.js';
const base={params:{profile_enabled:false},grade:{exposure:0.4,contrast:0.1},masks:[],heals:[],optics:{}};
const target=structuredClone(base); target.grade.contrast=0.6;
const preset={id:'a',name:'A',base,target,amount:33,enabled:true};
const current=blendPresetState(base,target,33); current.grade.exposure=0.9;
current.grade.contrast=Number(current.grade.contrast.toFixed(4));
const kept=reconcilePresetAdjustment(preset,current);
current.grade.contrast=0.8;
console.log(JSON.stringify({kept,retired:reconcilePresetAdjustment(preset,current)}));
""")
        self.assertEqual(result["kept"]["base"]["grade"]["exposure"], 0.9)
        self.assertEqual(result["kept"]["target"]["grade"]["exposure"], 0.9)
        self.assertIsNone(result["retired"])

    def test_development_variants_are_fixed_choices_at_partial_amounts(self):
        result = self.run_js("""
import {blendPresetState,reconcilePresetAdjustment} from './web/preset-amount.js';
const base={params:{development_time:0,print_development_time:0,print_exposure:.8},
  grade:{},masks:[],heals:[],optics:{}};
const target={...base,params:{development_time:1,print_development_time:2,print_exposure:1}};
const partial=blendPresetState(base,target,55);
console.log(JSON.stringify({partial,off:blendPresetState(base,target,0),
  retained:!!reconcilePresetAdjustment({base,target,amount:55,enabled:true},partial)}));
""")
        self.assertEqual(result["partial"]["params"]["development_time"], 1)
        self.assertEqual(result["partial"]["params"]["print_development_time"], 2)
        self.assertAlmostEqual(result["partial"]["params"]["print_exposure"], .91)
        self.assertEqual(result["off"]["params"]["development_time"], 0)
        self.assertTrue(result["retained"])

    def test_classic_bw_and_film_development_presets_retain_amount_over_differing_base_film(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {composePresetState} from './web/presets.js';
import {blendPresetState, reconcilePresetAdjustment, presetEditState} from './web/preset-amount.js';
import {normalizeFilmTuning, mergeFilmTuning} from './web/film-browser.js';

const builtin = JSON.parse(readFileSync('./presets/builtin.json', 'utf8'));
const classicBw = builtin.presets.find(p => p.id === 'lighttable/classic-bw');
const silverNoir = builtin.presets.find(p => p.id === 'lighttable/silver-noir');

const profiles = [
  {id: 'kodak_portra_400', stage: 'filming', type: 'negative', developmentTimes: []},
  {id: 'kodak_doublex', stage: 'filming', type: 'negative', developmentTimes: [4.0, 5.0, 6.5, 9.0, 12.0], defaultDevelopmentTime: 6.5},
  {id: 'kodak_trix', stage: 'filming', type: 'positive', developmentTimes: [1.0], defaultDevelopmentTime: 1.0},
  {id: 'kodak_2302', stage: 'printing', channelModel: 'cmy', developmentTimes: [2.0, 3.5, 5.0, 7.0, 9.0], defaultDevelopmentTime: 5.0},
  {id: 'kodak_portra_endura', stage: 'printing', channelModel: 'cmy', developmentTimes: []}
];

const appSource = readFileSync('./web/app.js', 'utf8');
const runtime = vm.createContext({
  normalizeFilmTuning, mergeFilmTuning,
  S: {profiles, filmDefaults: {stock: 'kodak_portra_400', grain_amount: 1}},
  grainBaseline: () => 0.2,
  $: () => ({value: ''}),
  tr: (s, params) => s,
});
for (const name of ['normalizeFilmParams', 'mergeFilmParams']) {
  vm.runInContext(appSource.match(new RegExp(`^function ${name}\\\\([^]*?^}`, 'm'))[0], runtime);
}

const baseDoubleX = {
  params: {stock: 'kodak_doublex', paper: 'kodak_2302', development_time: 6.5, print_development_time: 5.0, profile_enabled: true},
  grade: {contrast: 0.1}, masks: [], heals: [], optics: {}
};
const targetClassic = composePresetState(baseDoubleX, classicBw, {
  normalizeFilmParams: runtime.normalizeFilmParams,
  mergeFilmParams: runtime.mergeFilmParams,
});
const adjClassic = {
  id: classicBw.id, name: classicBw.name, amount: 100, enabled: true,
  base: structuredClone(baseDoubleX), target: presetEditState(targetClassic)
};
const reconciledClassic = reconcilePresetAdjustment(adjClassic, targetClassic);
const blendedClassicHalf = blendPresetState(reconciledClassic.base, reconciledClassic.target, 50);
const blendedClassicZero = blendPresetState(reconciledClassic.base, reconciledClassic.target, 0);

const basePortra = {
  params: {stock: 'kodak_portra_400', paper: 'kodak_portra_endura', development_time: 0, print_development_time: 0, profile_enabled: true},
  grade: {contrast: 0.1}, masks: [], heals: [], optics: {}
};
const targetSilver = composePresetState(basePortra, silverNoir, {
  normalizeFilmParams: runtime.normalizeFilmParams,
  mergeFilmParams: runtime.mergeFilmParams,
});
const adjSilver = {
  id: silverNoir.id, name: silverNoir.name, amount: 100, enabled: true,
  base: structuredClone(basePortra), target: presetEditState(targetSilver)
};
const reconciledSilver = reconcilePresetAdjustment(adjSilver, targetSilver);
const blendedSilverHalf = blendPresetState(reconciledSilver.base, reconciledSilver.target, 50);
const blendedSilverZero = blendPresetState(reconciledSilver.base, reconciledSilver.target, 0);

console.log(JSON.stringify({
  classicReconciled: !!reconciledClassic,
  classicTargetDev: targetClassic.params.development_time,
  classicHalfDev: blendedClassicHalf.params.development_time,
  classicZeroDev: blendedClassicZero.params.development_time,
  silverReconciled: !!reconciledSilver,
  silverTargetDev: targetSilver.params.development_time,
  silverTargetPrintDev: targetSilver.params.print_development_time,
  silverHalfDev: blendedSilverHalf.params.development_time,
  silverZeroDev: blendedSilverZero.params.development_time,
}));
""")
        self.assertTrue(result["classicReconciled"])
        self.assertEqual(result["classicTargetDev"], 1.0)
        self.assertEqual(result["classicHalfDev"], 1.0)
        self.assertEqual(result["classicZeroDev"], 6.5)
        self.assertTrue(result["silverReconciled"])
        self.assertEqual(result["silverTargetDev"], 6.5)
        self.assertEqual(result["silverTargetPrintDev"], 5.0)
        self.assertEqual(result["silverHalfDev"], 6.5)
        self.assertEqual(result["silverZeroDev"], 0.0)

    def test_all_builtin_amounts_survive_production_state_cleaning(self):
        base, _ = server.cleaned_state_request({
            "params": {"profile_enabled": False, "print_exposure": .8, "grain_amount": .35},
            "grade": {"exposure": .37, "contrast": .08, "saturation": -.04},
            "masks": [], "heals": [], "optics": {},
        }, strict=False)
        # Include non-grid film values, grading colors and curves from the real
        # shipped catalog, then pass both baseline and current state through the
        # production cleaners exactly as a save/reopen does.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "states.json"
            path.write_text(json.dumps({"base": base, "presets": preset_library.builtin_presets()}))
            states = self.run_js("""
import {readFileSync} from 'node:fs';
import {composePresetState} from './web/presets.js';
import {blendPresetState} from './web/preset-amount.js';
const {base,presets}=JSON.parse(readFileSync(PATH,'utf8')), states=[];
for (const preset of presets) {
  const target=composePresetState(base,preset);
  for (let amount=0;amount<=100;amount++) {
    states.push({...blendPresetState(base,target,amount),
      preset:{id:preset.id,name:preset.name,base,target,amount,enabled:amount>0}});
  }
}
console.log(JSON.stringify(states));
""".replace("PATH", json.dumps(str(path))))
            path.write_text(json.dumps([server.cleaned_state_request(state, strict=False)[0] for state in states]))
            failures = self.run_js("""
import {readFileSync} from 'node:fs';
import {presetEditState,reconcilePresetAdjustment} from './web/preset-amount.js';
const states=JSON.parse(readFileSync(PATH,'utf8'));
console.log(JSON.stringify(states.filter(state=>
  !reconcilePresetAdjustment(state.preset,presetEditState(state)))
  .map(state=>[state.preset.id,state.preset.amount])));
""".replace("PATH", json.dumps(str(path))))
        self.assertEqual(len(states), len(preset_library.builtin_presets()) * 101)
        self.assertEqual(failures, [])

    def test_browser_toggle_amount_switch_undo_and_photo_guard(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {composePresetState} from './web/presets.js';
import {presetEditState,blendPresetState,reconcilePresetAdjustment,presetControlledSettings} from './web/preset-amount.js';
const source=readFileSync('./web/app.js','utf8');
const S={editingName:'A',params:{profile_enabled:false},grade:{exposure:0.7,contrast:0.1,saturation:0},masks:[],heals:[],optics:{},preset:null};
let photoName='A', LAST_PRESET_APPLICATION=null;
const cur=()=>({name:photoName}), cloneValue=structuredClone, presetKey=p=>p.id;
const history=[]; const pushUndo=()=>history.push(structuredClone(S));
const readControls=()=>{},presetHasApplicableSettings=()=>true,editId=p=>p,normalizeFilmParams=p=>p,mergeFilmParams=(a,b)=>({...a,...b});
const filmRenderFingerprint=()=>JSON.stringify(S.params),baseEditsFingerprint=()=>JSON.stringify([S.optics,S.heals]);
const syncControls=()=>{},syncGrade=()=>{},syncCurveFromGrade=()=>{},syncHsl=()=>{},syncMaskPanel=()=>{},syncHealPanel=()=>{},syncOpticsPanel=()=>{},drawGrade=()=>{},saveState=()=>{},renderFilm=()=>{},refreshBaseEdits=()=>{},markContinuousInput=()=>{};
const a={id:'a',name:'A look',scope:'look',grade:{contrast:0.5}},b={id:'b',name:'B look',scope:'look',grade:{saturation:0.6}};
const exercise=source.slice(source.indexOf('let presetAmountGesture ='),source.indexOf('function presetPhotoSnapshot()')) + `
toggleBrowserPreset(a,{name:'A'}); const applied=S.grade.contrast;
changePresetAmount('a','A',25); changePresetAmount('a','A',50); changePresetAmount('a','A',50,true);
const half=S.grade.contrast,amountUndoSteps=history.length;
toggleBrowserPreset(a,{name:'A'}); const off=S.grade.contrast;
toggleBrowserPreset(a,{name:'A'}); const on=S.grade.contrast;
S.grade.exposure=0.9;
toggleBrowserPreset(b,{name:'A'}); const switched=structuredClone(S);
const beforeWrong=JSON.stringify(S);photoName='B';changePresetAmount('b','A',10,true);
const wrongPhotoUnchanged=beforeWrong===JSON.stringify(S);
Object.assign(S,history.pop()); const undoAmount=S.preset.amount;
console.log(JSON.stringify({applied,half,amountUndoSteps,off,on,switched,wrongPhotoUnchanged,undoAmount}));`;
eval(exercise);
""")
        self.assertEqual(result["applied"], 0.5)
        self.assertAlmostEqual(result["half"], 0.3)
        self.assertEqual(result["amountUndoSteps"], 2)
        self.assertEqual(result["off"], 0.1)
        self.assertAlmostEqual(result["on"], 0.3)
        self.assertEqual(result["switched"]["grade"], {"exposure": 0.9, "contrast": 0.1, "saturation": 0.6})
        self.assertTrue(result["wrongPhotoUnchanged"])
        self.assertEqual(result["undoAmount"], 50)

    def test_clean_save_reload_retains_amount_and_baseline(self):
        state = {"params": {}, "grade": {"contrast": 0.1}, "masks": [], "heals": [], "optics": {}}
        preset = {"id": "test/look", "name": "Test", "amount": 37, "enabled": True,
                  "base": state, "target": {**state, "grade": {"contrast": 0.6}}}
        cleaned, warnings = server.cleaned_state_request({"preset": preset}, strict=True)
        self.assertEqual(warnings, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / "photos"
            photos.mkdir()
            (photos / "a.jpg").write_bytes(b"sample" * 64)
            path = root / "catalog.sqlite3"
            cat = catalog.Catalog(path)
            source = cat.add_source(photos)
            catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
            image_id = cat.image_id_for(source, "a.jpg")
            cat.save_state(image_id, cleaned)
            cat.close()
            cat = catalog.Catalog(path)
            loaded = cat.state_for(image_id)["preset"]
            cat.close()
        self.assertEqual(loaded, cleaned["preset"])
        result = self.run_js("""
import {blendPresetState,reconcilePresetAdjustment} from './web/preset-amount.js';
const preset=PRESET;
const current=blendPresetState(preset.base,preset.target,preset.amount);
console.log(JSON.stringify(reconcilePresetAdjustment(preset,current)));
""".replace("PRESET", json.dumps(loaded)))
        self.assertEqual(result["amount"], 37)

    def test_slow_recipe_cannot_apply_after_another_click_or_photo_change(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {presetKey} from './web/preset-browser.js';
const source=readFileSync('./web/preset-browser.js','utf8');
const start=source.indexOf('  async function activatePreset('),end=source.indexOf('  async function renderDetail(',start);
let active=true,destroyed=false,selected=null,activationGeneration=0,activationController=null;
let photo={name:'A'}, requests=[],applied=[];
const getPhoto=()=>structuredClone(photo),getAdjustment=()=>null;
const photoUnchanged=p=>p.name===photo.name,showDetail=p=>{selected=p};
const getRecipe=(preset,{signal})=>new Promise(resolve=>requests.push({preset,signal,resolve}));
const canApply=()=>true,onApply=p=>applied.push(p.id),render=()=>{},cards=new Map(),status={};
const activate=eval('('+source.slice(start,end).trim()+')');
const a=activate({id:'a'}),b=activate({id:'b'});
requests[0].resolve({id:'a'});await a;
const firstAborted=requests[0].signal.aborted;
requests[1].resolve({id:'b'});await b;
const c=activate({id:'c'});photo={name:'B'};requests[2].resolve({id:'c'});await c;
console.log(JSON.stringify({applied,firstAborted}));
""")
        self.assertEqual(result["applied"], ["b"])
        self.assertTrue(result["firstAborted"])

    def test_schema_six_migration_keeps_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(catalog._SCHEMA.replace("    preset_json TEXT,\n", ""))
            connection.execute("INSERT INTO meta(key,value) VALUES('schema_version','6')")
            connection.commit()
            connection.close()
            cat = catalog.Catalog(path)
            self.assertIn("preset_json", {row[1] for row in cat.connection.execute("PRAGMA table_info(image_state)")})
            self.assertEqual(cat.stats()["schema"], catalog.SCHEMA_VERSION)
            self.assertTrue(cat.integrity_ok())
            self.assertEqual(len(list((Path(directory) / "Backups").glob("*.zip"))), 1)
            cat.close()

    def test_invalid_metadata_is_rejected(self):
        for value in [{}, {"id": "a", "name": "A", "amount": -1, "enabled": True}, "bad"]:
            with self.assertRaises(server.ValidationError):
                server.cleaned_state_request({"preset": value}, strict=True)
        self.assertIsNone(server.cleaned_state_request({"preset": None}, strict=True)[0]["preset"])
