"""Behavioral checks for display caching and input-to-presentation accounting."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node required')
class InteractionTests(unittest.TestCase):
    def run_js(self, script):
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                cwd=ROOT, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_cache_identity_and_lru_and_provisional_rejection(self):
        result = self.run_js("""
import {createPresentationCache, renderRequestKey} from './web/presentation-cache.js';
const im = {name:'a',fileKey:'v1',mtime:10};
const req = {w:1100,engine:'rs',native:true,params:{b:2,a:1},optics:{},heals:[]};
const key = renderRequestKey(im,req);
const same = renderRequestKey(im,{...req,params:{a:1,b:2},generation:99});
const changed = [ {...req,w:2200}, {...req,params:{a:2,b:2}},
 {...req,optics:{distortion:0.2}}, {...req,heals:[{id:'spot'}]}]
 .map(r=>renderRequestKey(im,r)!==key);
changed.push(renderRequestKey({...im,fileKey:'v2'},req)!==key);
const c=createPresentationCache(2); c.set('a',{key:'A'}); c.set('b',{key:'B'});
c.get('a'); c.set('c',{key:'C'}); c.set('draft',{key:'draft',refining:true});
c.set('failed',{error:'bad'});
console.log(JSON.stringify({same:key===same,changed,size:c.size,
 a:c.get('a').key,b:c.get('b')??null,c:c.get('c').key}));
""")
        self.assertTrue(result['same'])
        self.assertTrue(all(result['changed']))
        self.assertEqual(result['size'], 2)
        self.assertEqual((result['a'], result['b'], result['c']), ('A', None, 'C'))

    def test_navigation_reentry_commits_native_state_even_when_surface_is_identical(self):
        result = self.run_js("""
import {readFileSync} from 'node:fs';
const source = readFileSync('./web/app.js','utf8');
const body = source.slice(source.indexOf('async function setBaseImage('),
 source.indexOf('function setWebGLBaseImage('));
let S={seq:1, renderState:'pending', presentedRenderKey:'same',presentedBackend:'native-metal'};
let uploads=0,remembers=0;
const nativePreviewActive=()=>true,drawGrade=()=>{};
let setNativeBaseImage=async()=>{uploads++;return {presentation:'native-metal'}};
const rememberPresentedRender=()=>{remembers++};
const setBaseImage=eval('('+body.trim()+')');
await setBaseImage({key:'same'},1);
const reentry={uploads,remembers};
setNativeBaseImage=async()=>{S.seq=3;return {presentation:'native-metal'}};
await setBaseImage({key:'other'},2);
console.log(JSON.stringify({reentry,remembers}));
""")
        self.assertEqual(result['reentry'], {'uploads': 1, 'remembers': 1})
        self.assertEqual(result['remembers'], 1)

    def test_only_presented_inputs_are_measured_and_disabled_is_inert(self):
        result = self.run_js("""
import {createInteractionRecorder} from './web/interaction-perf.js';
let t=0;const r=createInteractionRecorder(true,()=>t);r.start('drag');
r.input(1);t=2;r.input(1);const packet=r.take();t=18;
r.presented(packet,'metal');const first=r.snapshot();
r.start('next');r.presented(packet,'metal');const next=r.snapshot();
const off=createInteractionRecorder(false,()=>t);off.start('off');off.input(1);
console.log(JSON.stringify({first,next,disabled:off.take()}));
""")
        self.assertEqual(result['first']['inputs'], 2)
        self.assertEqual(result['first']['presented'], 1)
        self.assertEqual(result['first']['coalescedInputs'], 1)
        self.assertEqual(result['first']['p95Ms'], 16)
        self.assertEqual(result['next']['presented'], 0)
        self.assertIsNone(result['disabled'])


if __name__ == '__main__':
    unittest.main()
