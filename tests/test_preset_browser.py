"""Preset preview scheduling stays bounded and cannot display an old photo."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required")
class PresetBrowserTests(unittest.TestCase):
    def run_js(self, script):
        # App functions exercised below share the live localization dependency.
        script = "import {t as tr, tn as trn} from './web/i18n.js';\n" + script
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], cwd=ROOT,
            text=True, capture_output=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_replacement_waits_for_old_request_and_discards_its_pixels(self):
        result = self.run_js("""
import {createPresetPreviewQueue} from './web/preset-browser.js';
const pending = [], started = [], delivered = [], errors = [];
let inFlight = 0, peak = 0;
const queue = createPresetPreviewQueue({
  render: (item, {signal}) => {
    inFlight++; peak = Math.max(peak, inFlight); started.push(item);
    // Deliberately ignore cancellation to represent a render already in flight.
    return new Promise(resolve => pending.push({signal, resolve: value => {
      inFlight--; resolve(value);
    }}));
  },
  onResult: (item, pixels) => delivered.push([item, pixels]),
  onError: (item) => errors.push(item),
});
const tick = () => new Promise(resolve => setImmediate(resolve));
queue.replace(['photo-A/preset-1', 'photo-A/preset-2']);
const old = pending.shift();
queue.replace(['photo-B/preset-1', 'photo-B/preset-2']);
const beforeOldFinishes = [...started];
old.resolve('old pixels'); await tick();
pending.shift().resolve('B1 pixels'); await tick();
pending.shift().resolve('B2 pixels'); await tick();
console.log(JSON.stringify({peak, oldAborted:old.signal.aborted,
  beforeOldFinishes, started, delivered, errors}));
""")
        self.assertEqual(result["peak"], 1)
        self.assertTrue(result["oldAborted"])
        self.assertEqual(result["beforeOldFinishes"], ["photo-A/preset-1"])
        self.assertEqual(result["started"], [
            "photo-A/preset-1", "photo-B/preset-1", "photo-B/preset-2",
        ])
        self.assertEqual(result["delivered"], [
            ["photo-B/preset-1", "B1 pixels"], ["photo-B/preset-2", "B2 pixels"],
        ])
        self.assertEqual(result["errors"], [])

    def test_close_discards_results_and_failure_does_not_block_other_previews(self):
        result = self.run_js("""
import {createPresetPreviewQueue} from './web/preset-browser.js';
const started = [], delivered = [], errors = [];
let resolveClosing;
const queue = createPresetPreviewQueue({
  render: async item => {
    started.push(item);
    if (item === 'failed') throw new Error('Unavailable');
    if (item === 'closing') return new Promise(resolve => { resolveClosing = resolve; });
    return item + ' pixels';
  },
  onResult: (item, pixels) => delivered.push([item, pixels]),
  onError: item => errors.push(item),
});
const tick = () => new Promise(resolve => setImmediate(resolve));
queue.replace(['failed', 'good']); await tick();
queue.replace(['closing', 'never-start']);
queue.cancel(); resolveClosing('stale pixels'); await tick();
queue.replace(['reopened']); await tick();
console.log(JSON.stringify({started, delivered, errors}));
""")
        self.assertEqual(result["started"], ["failed", "good", "closing", "reopened"])
        self.assertEqual(result["delivered"], [["good", "good pixels"], ["reopened", "reopened pixels"]])
        self.assertEqual(result["errors"], ["failed"])

    def test_search_and_favorites_intersect_without_mutating_inventory(self):
        result = self.run_js("""
import {filterPresets} from './web/preset-browser.js';
const presets = [
  {name:'Warm Portrait',source:'lighttable',presetType:'style'},
  {name:'Cool Portrait',source:'lightroom',presetType:'style'},
  {name:'Lens Correction',source:'capture-one',presetType:'tool'},
];
const before = JSON.stringify(presets), names = options => filterPresets(presets, options).map(p => p.name);
console.log(JSON.stringify({
  search:names({query:' PORTRAIT '}), source:names({query:'Camera Raw'}),
  type:names({query:'tool'}),
  favorites:names({query:'portrait',favoritesOnly:true,favorites:['Cool Portrait','Lens Correction']}),
  missing:names({favoritesOnly:true,favorites:['Deleted']}), unchanged:before===JSON.stringify(presets),
}));
""")
        self.assertEqual(result["search"], ["Warm Portrait", "Cool Portrait"])
        self.assertEqual(result["source"], ["Cool Portrait"])
        self.assertEqual(result["type"], ["Lens Correction"])
        self.assertEqual(result["favorites"], ["Cool Portrait"])
        self.assertEqual(result["missing"], [])
        self.assertTrue(result["unchanged"])

    def test_one_click_preserves_unmapped_edits_and_both_inputs(self):
        result = self.run_js("""
import {composePresetState} from './web/presets.js';
const state = {
  params:{profile_enabled:true,stock:'existing'},
  grade:{exposure:1,contrast:0.4,hsl:{red:{h:0.2,s:0.3,l:0.1},blue:{s:0.2}}},
  crop:{x:0.1,y:0.1,w:0.8,h:0.8},
  optics:{vertical:0.3}, masks:[{id:'original-mask',type:'radial'}],
  heals:[{id:'original-heal',radius:0.02}],
};
const preset = {name:'Imported',presetType:'style',recommendedFilmOff:true,
  includedGrade:['exposure','hsl'],grade:{exposure:0.2,contrast:0,hsl:{red:{h:-0.1}}},
  optics:{distortion:0.2},masks:[{id:'original-mask',type:'radial'}],heals:[],
};
const before = JSON.stringify([state,preset]);
const next = composePresetState(state,preset,{createId:prefix=>prefix+'-new'});
console.log(JSON.stringify({next,unchanged:before===JSON.stringify([state,preset])}));
""")
        state = result["next"]
        self.assertEqual(state["grade"]["exposure"], 0.2)
        self.assertEqual(state["grade"]["contrast"], 0.4)
        self.assertEqual(state["grade"]["hsl"]["red"], {"h": -0.1, "s": 0.3, "l": 0.1})
        self.assertEqual(state["grade"]["hsl"]["blue"], {"s": 0.2})
        self.assertEqual(state["params"], {"profile_enabled": True, "stock": "existing"})
        self.assertEqual(state["crop"], {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8})
        self.assertEqual(state["optics"]["vertical"], 0.3)
        self.assertEqual(state["optics"]["distortion"], 0.2)
        self.assertEqual([mask["id"] for mask in state["masks"]], ["original-mask", "mask-new"])
        self.assertEqual(state["heals"][0]["id"], "original-heal")
        self.assertTrue(result["unchanged"])

    def test_only_explicit_replacement_resets_unspecified_adjustments(self):
        result = self.run_js("""
import {composePresetState} from './web/presets.js';
const state = {params:{profile_enabled:true,stock:'existing'},
  grade:{exposure:1,contrast:0.4,curveL:[0,1]},crop:{x:0.1,y:0.1,w:0.8,h:0.8},
  optics:{vertical:0.3},masks:[{id:'mask',type:'radial'}],heals:[{id:'heal'}],
};
const preset={name:'Exposure only',includedGrade:['exposure'],grade:{exposure:0.2}};
const next=composePresetState(state,preset,{replace:true});
const filmOff=composePresetState(state,preset,{filmOff:true});
console.log(JSON.stringify({next,filmOff:filmOff.params.profile_enabled}));
""")
        state = result["next"]
        self.assertEqual(state["grade"]["exposure"], 0.2)
        self.assertEqual(state["grade"]["contrast"], 0)
        self.assertNotIn("curveL", state["grade"])
        self.assertEqual(state["masks"], [])
        self.assertEqual(state["heals"], [])
        self.assertEqual(state["optics"]["vertical"], 0)
        self.assertEqual(state["crop"], {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8})
        self.assertEqual(state["params"], {"profile_enabled": True, "stock": "existing"})
        self.assertFalse(result["filmOff"])

    def test_film_changes_require_inclusion_and_replace_only_when_requested(self):
        result = self.run_js("""
import {composePresetState} from './web/presets.js';
const state = {params:{profile_enabled:false,stock:'base',grain_amount:0.4},
  grade:{exposure:0.2},optics:{},masks:[],heals:[]};
const preset={includeFilm:true,params:{stock:'new',profile_enabled:true},grade:{contrast:0.3}};
const included=composePresetState(state,preset);
const excluded=composePresetState(state,{...preset,includeFilm:false});
const replaced=composePresetState(state,preset,{
  replace:true, normalizeFilmParams:params=>({grain_amount:1,...params}),
});
console.log(JSON.stringify({included:included.params,excluded:excluded.params,replaced:replaced.params}));
""")
        self.assertEqual(result["included"], {
            "profile_enabled": True, "stock": "new", "grain_amount": 0.4,
        })
        self.assertEqual(result["excluded"], {
            "profile_enabled": False, "stock": "base", "grain_amount": 0.4,
        })
        self.assertEqual(result["replaced"], {
            "profile_enabled": True, "stock": "new", "grain_amount": 1,
        })

    def test_look_preserves_photo_corrections_even_with_malformed_recipe(self):
        result = self.run_js("""
import {composePresetState} from './web/presets.js';
const state={params:{stock:'base',profile_enabled:true,wb_mode:'custom',wb_temperature:5100,
  raw_profile:'camera',film_format:'120',linear_input:true,input_color_space:'camera',rotate:90},
  grade:{exposure:1.2,temp:0.2,tint:-0.1,sharpness:0.6,luminanceNoise:0.3,
    chromaticAberrationRedCyan:0.4,contrast:0.1},
  crop:{x:0.1,y:0.2,w:0.7,h:0.6},masks:[{id:'mask'}],heals:[{id:'heal'}],optics:{vertical:0.2}};
const preset={scope:'look',filmMode:'off',includeFilm:true,
  includedFilm:['stock','grain_amount','wb_temperature','film_format','raw_profile','rotate'],
  params:{stock:'new',grain_amount:0.3,wb_temperature:2000,film_format:'35mm',
    raw_profile:'smooth',rotate:0,halation_amount:0.9},
  grade:{contrast:0.4,exposure:-2,temp:1,tint:1,sharpness:0,luminanceNoise:0,
    chromaticAberrationRedCyan:-0.4},
  masks:[{id:'bad'}],heals:[{id:'bad'}],optics:{vertical:-0.5}};
const before=JSON.stringify([state,preset]);
const next=composePresetState(state,preset,{replace:true});
const preserve=composePresetState(state,{...preset,filmMode:'preserve'});
console.log(JSON.stringify({state,next,preserveEnabled:preserve.params.profile_enabled,
  unchanged:before===JSON.stringify([state,preset])}));
""")
        state, next_state = result["state"], result["next"]
        for key in ["crop", "masks", "heals", "optics"]:
            self.assertEqual(next_state[key], state[key])
        for key in ["exposure", "temp", "tint", "sharpness", "luminanceNoise", "chromaticAberrationRedCyan"]:
            self.assertEqual(next_state["grade"][key], state["grade"][key])
        for key in ["wb_mode", "wb_temperature", "raw_profile", "film_format", "linear_input", "input_color_space", "rotate"]:
            self.assertEqual(next_state["params"][key], state["params"][key])
        self.assertEqual(next_state["grade"]["contrast"], 0.4)
        self.assertEqual(next_state["params"]["stock"], "new")
        self.assertEqual(next_state["params"]["grain_amount"], 0.3)
        self.assertNotIn("halation_amount", next_state["params"])
        self.assertFalse(next_state["params"]["profile_enabled"])
        self.assertTrue(result["preserveEnabled"])
        self.assertTrue(result["unchanged"])

    def test_ids_migrate_favorites_and_distinguish_duplicate_names(self):
        result = self.run_js("""
import {migratePresetFavorites,filterPresets,presetWebURL} from './web/preset-browser.js';
const presets=[{id:'lighttable/warm',name:'Warm',collection:'builtin',tags:['Portrait']},
  {id:'mine/1',name:'Warm',collection:'yours',author:{name:'Alice'}},
  {id:'mine/2',name:'Evening',collection:'yours',description:'Blue shadows'}];
const ids=migratePresetFavorites(['Warm','deleted/id','mine/2','Warm'],presets);
const names=opts=>filterPresets(presets,opts).map(p=>p.id);
console.log(JSON.stringify({ids,
  exact:names({favoritesOnly:true,favorites:['mine/1']}),
  collection:names({collection:'builtin',tag:'Portrait'}),
  author:names({query:'alice'}),description:names({query:'blue shadows'}),
  unsafe:[presetWebURL('javascript:alert(1)'),presetWebURL('file:///etc/passwd'),presetWebURL('http://bad.test')],
  safe:presetWebURL('https://example.com/preset')}));
""")
        self.assertEqual(result["ids"], ["lighttable/warm", "mine/1", "deleted/id", "mine/2"])
        self.assertEqual(result["exact"], ["mine/1"])
        self.assertEqual(result["collection"], ["lighttable/warm"])
        self.assertEqual(result["author"], ["mine/1"])
        self.assertEqual(result["description"], ["mine/2"])
        self.assertEqual(result["unsafe"], [None, None, None])
        self.assertEqual(result["safe"], "https://example.com/preset")

    def test_successive_looks_share_baseline_and_manual_edits_reset_it(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {composePresetState} from './web/presets.js';
const source=readFileSync('./web/app.js','utf8');
const section=(first,last)=>source.slice(source.indexOf(first),source.indexOf(last));
let LAST_PRESET_APPLICATION=null;
const S={editingName:'A',params:{profile_enabled:false,stock:'base'},
  grade:{exposure:0.7,contrast:0.1,saturation:0},masks:[],heals:[],optics:{}};
const cur=()=>({name:'A'}),cloneValue=v=>structuredClone(v);
let undos=0; const history=[];
const editId=p=>p,normalizeFilmParams=p=>p,mergeFilmParams=(a,b)=>({...a,...b});
const snapshot=()=>JSON.stringify({params:S.params,grade:S.grade,masks:S.masks,heals:S.heals,optics:S.optics});
const readControls=()=>{},pushUndo=()=>{undos++;history.push(JSON.parse(snapshot()))},presetHasApplicableSettings=()=>true;
const syncControls=()=>{},syncGrade=()=>{},syncCurveFromGrade=()=>{},syncHsl=()=>{};
const syncMaskPanel=()=>{},syncHealPanel=()=>{},syncOpticsPanel=()=>{},drawGrade=()=>{};
const saveState=()=>{},renderFilm=()=>{},toast=()=>{},PRESET_BROWSER=null;
const presetApplicationMatches=eval('('+section('function presetApplicationMatches(', 'function stateWithPreset(').trim()+')');
const stateWithPreset=eval('('+section('function stateWithPreset(', 'function applyPreset(').trim()+')');
const applyPreset=eval('('+section('function applyPreset(', 'PRESET_BROWSER = createPresetBrowser(').trim()+')');
const a={name:'Film',scope:'look',filmMode:'on',includedFilm:['stock'],params:{stock:'film'},grade:{saturation:0.5}};
const b={name:'Natural',scope:'look',filmMode:'off',grade:{contrast:0.3}};
const original=JSON.parse(snapshot());
applyPreset(a,{name:'A'});
const preview=stateWithPreset(JSON.parse(snapshot()),b,{},'A');
const beforePreview=snapshot();
stateWithPreset(JSON.parse(snapshot()),b,{},'A');
const previewReadOnly=beforePreview===snapshot();
applyPreset(b,{name:'A'});
const switched=JSON.parse(snapshot());
S.grade.exposure=1.1;
applyPreset(a,{name:'A'});
const manual=JSON.parse(snapshot());
const otherPhoto=stateWithPreset(original,b,{},'B');
console.log(JSON.stringify({preview,previewReadOnly,switched,manual,otherPhoto,undos,history}));
""")
        for label in ["preview", "switched"]:
            self.assertEqual(result[label]["grade"]["saturation"], 0)
            self.assertEqual(result[label]["grade"]["contrast"], 0.3)
            self.assertEqual(result[label]["grade"]["exposure"], 0.7)
            self.assertEqual(result[label]["params"]["stock"], "base")
            self.assertFalse(result[label]["params"]["profile_enabled"])
        self.assertTrue(result["previewReadOnly"])
        self.assertEqual(result["manual"]["grade"]["exposure"], 1.1)
        self.assertEqual(result["manual"]["grade"]["contrast"], 0.3)
        self.assertEqual(result["otherPhoto"]["grade"]["exposure"], 0.7)
        self.assertEqual(result["undos"], 3)
        self.assertEqual(result["history"][1]["grade"]["saturation"], 0.5)
        self.assertEqual(result["history"][2]["grade"]["exposure"], 1.1)

    def test_catalog_calendar_date_does_not_shift_across_time_zones(self):
        result = self.run_js("""
import {formatPresetCatalogDate} from './web/preset-browser.js';
const format=value=>formatPresetCatalogDate(value,'en-US');
process.env.TZ='America/New_York';
const west={calendar:format('2026-09-08'),timestamp:format('2026-09-08T00:30:00Z')};
process.env.TZ='Pacific/Kiritimati';
const east={calendar:format('2026-09-08'),timestamp:format('2026-09-08T23:30:00Z')};
console.log(JSON.stringify({west,east,invalid:format('invalid date')}));
""")
        self.assertEqual(result["west"], {"calendar": "9/8/2026", "timestamp": "9/7/2026"})
        self.assertEqual(result["east"], {"calendar": "9/8/2026", "timestamp": "9/9/2026"})
        self.assertEqual(result["invalid"], "")

    def test_binary_exports_preserve_zip_and_jpeg_bytes_in_browser_and_native(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {bytesToBase64} from './web/presets.js';
const source=readFileSync('./web/app.js','utf8');
const start=source.indexOf('function downloadPresetFile(');
const end=source.indexOf("$('presetExport').onclick",start);
let bridge=null,blob,clicked=0,message;
const nativeBridge=()=>bridge;
const URL={createObjectURL:value=>{blob=value;return 'blob:example'},revokeObjectURL:()=>{}};
const document={createElement:()=>({click:()=>{clicked++}})};
const setTimeout=fn=>fn();
const downloadPresetFile=eval('('+source.slice(start,end).trim()+')');
const bytes=Uint8Array.from([80,75,3,4,0,255,128,10]);
const payload={filename:'submission.zip',contentType:'application/zip',
  encoding:'base64',content:bytesToBase64(bytes)};
const browserResult=downloadPresetFile(payload);
const restored=[...new Uint8Array(await blob.arrayBuffer())];
bridge={postMessage:value=>{message=value}};
const nativeResult=downloadPresetFile(payload);
console.log(JSON.stringify({restored,clicked,browserResult,nativeResult,message,type:blob.type}));
""")
        self.assertEqual(result["restored"], [80, 75, 3, 4, 0, 255, 128, 10])
        self.assertEqual(result["clicked"], 1)
        self.assertFalse(result["browserResult"])
        self.assertTrue(result["nativeResult"])
        self.assertEqual(result["type"], "application/zip")
        self.assertEqual(result["message"]["encoding"], "base64")
        self.assertEqual(result["message"]["filename"], "submission.zip")

    def test_loading_photo_cannot_supply_preview_state_or_receive_a_preset(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
const source=readFileSync('./web/app.js','utf8');
const section=(first,last)=>source.slice(source.indexOf(first),source.indexOf(last));
const S={editingName:'A',params:{stock:'old'}};
let image={name:'B'}, reads=0, snapshots=0;
const cur=()=>image, displayName=im=>im.name, $=()=>({value:'rs'});
const snapshot=()=>{snapshots++;return JSON.stringify({params:S.params})};
const readControls=()=>{reads++},toast=()=>{};
const presetPhotoSnapshot=eval('('+section('function presetPhotoSnapshot()', 'function presetApplicationMatches(').trim()+')');
const applyPreset=eval('('+section('function applyPreset(', 'PRESET_BROWSER = createPresetBrowser(').trim()+')');
const pending=presetPhotoSnapshot();
applyPreset({name:'Test'},{name:'B'});
const beforeReady={reads,snapshots};
S.editingName='B';
const ready=presetPhotoSnapshot();
console.log(JSON.stringify({pending,beforeReady,ready}));
""")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["beforeReady"], {"reads": 0, "snapshots": 0})
        self.assertEqual(result["ready"]["name"], "B")

    def test_repeated_click_is_inert_but_further_edits_keep_normal_layering(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
import {composePresetState} from './web/presets.js';
const source=readFileSync('./web/app.js','utf8');
const section=(first,last)=>source.slice(source.indexOf(first),source.indexOf(last));
let LAST_PRESET_APPLICATION=null;
const S={editingName:'A',params:{profile_enabled:true},grade:{exposure:0},masks:[],heals:[],optics:{}};
const cur=()=>({name:'A'}), cloneValue=v=>structuredClone(v);
let counter=0,undos=0;
const editId=prefix=>prefix+'-'+(++counter),normalizeFilmParams=p=>p,mergeFilmParams=(a,b)=>({...a,...b});
const snapshot=()=>JSON.stringify({params:S.params,grade:S.grade,masks:S.masks,heals:S.heals,optics:S.optics});
const readControls=()=>{},pushUndo=()=>{undos++},presetHasApplicableSettings=()=>true;
const syncControls=()=>{},syncGrade=()=>{},syncCurveFromGrade=()=>{},syncHsl=()=>{};
const syncMaskPanel=()=>{},syncHealPanel=()=>{},syncOpticsPanel=()=>{},drawGrade=()=>{};
const saveState=()=>{},renderFilm=()=>{},toast=()=>{},PRESET_BROWSER=null;
const presetApplicationMatches=eval('('+section('function presetApplicationMatches(', 'function stateWithPreset(').trim()+')');
const stateWithPreset=eval('('+section('function stateWithPreset(', 'function applyPreset(').trim()+')');
const applyPreset=eval('('+section('function applyPreset(', 'PRESET_BROWSER = createPresetBrowser(').trim()+')');
const preset={name:'Local effect',masks:[{id:'source',type:'radial',grade:{exposure:0.3}}]};
applyPreset(preset,{name:'A'});
applyPreset(preset,{name:'A'});
const twice={masks:S.masks.length,undos};
const preview=stateWithPreset(JSON.parse(snapshot()),preset,{},'A');
S.grade.exposure=0.7;
applyPreset(preset,{name:'A'});
console.log(JSON.stringify({twice,previewMasks:preview.masks.length,afterEdit:{masks:S.masks.length,undos}}));
""")
        self.assertEqual(result["twice"], {"masks": 1, "undos": 1})
        self.assertEqual(result["previewMasks"], 1)
        self.assertEqual(result["afterEdit"], {"masks": 2, "undos": 2})

    def test_explicit_replace_action_does_not_require_a_browser_confirm_dialog(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
const source=readFileSync('./web/app.js','utf8');
const replaceButton={}, filmOff={checked:false};
const $=id=>id==='presetReplace'?replaceButton:filmOff;
const selectedPreset=()=>({name:'Selected'}),presetPhotoSnapshot=()=>({name:'A'});
let applied;
const applyPreset=(preset,photo,options)=>{applied={preset,photo,options}};
const window={confirm:()=>{throw new Error('Native app has no confirm delegate')}};
eval(source.slice(source.indexOf("$('presetReplace').onclick"),source.indexOf("$('presetDel').onclick")));
replaceButton.onclick();
console.log(JSON.stringify(applied));
""")
        self.assertEqual(result["photo"]["name"], "A")
        self.assertEqual(result["options"], {"replace": True, "filmOff": False})


if __name__ == "__main__":
    unittest.main()
