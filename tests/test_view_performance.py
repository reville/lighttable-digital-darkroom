"""Exercise viewport geometry and bounded work, rather than implementation strings."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class ViewPerformanceTests(unittest.TestCase):
    def test_layout_covers_viewport_without_overlap_and_bounds_nodes(self):
        self.run_js('''
const images = Array.from({length: 10000}, (_, index) => ({
  name: String(index), width: [6000,4000,6000,0][index % 4], height: [4000,6000,2000,0][index % 4]
}));
for (const photo of [false, true]) for (const width of [320, 800, 1900]) {
  const layout = createGridLayout(images, {width, cell: 180, photo});
  assert.equal(layout.positions.length, images.length);
  for (const lane of layout.lanes) {
    for (let i = 1; i < lane.length; i++) assert.ok(lane[i].top > lane[i-1].bottom);
    for (const p of lane) {
      assert.ok(p.left >= 0 && p.left + p.width <= width + .001);
      const im = images[p.index];
      const expected = photo ? p.width * (im.width ? im.height / im.width : 1) : p.width + 44;
      assert.ok(Math.abs(p.height - expected) < .001);
    }
  }
  for (const fraction of [0, .15, .5, .99]) {
    const top = layout.height * fraction, height = 1000;
    const window = visibleGridPositions(layout, top, height);
    assert.ok(window.length < 250, `${photo} ${width}: ${window.length} live nodes`);
    const ids = new Set(window.map(p => p.index));
    const expected = layout.positions.filter(p => p.bottom >= top && p.top <= top + height);
    for (const p of expected) assert.ok(ids.has(p.index), 'visible cell missing');
  }
}
assert.equal(createGridLayout([{thumbnailAspectRatio:1.5}], {width:180,photo:true}).positions[0].height, 270);
assert.equal(createGridLayout([], {width:800}).height, 0);
assert.deepEqual(visibleGridPositions(createGridLayout([], {width:800}), 0, 1000), []);
''')

    def test_auto_quality_tracks_display_density_zoom_crop_and_source_limit(self):
        self.run_js('''
const base = {sourceWidth:6000, sourceHeight:4000, viewportWidth:1000, viewportHeight:800};
assert.equal(automaticPreviewWidth(base), 1100);
assert.equal(automaticPreviewWidth({...base, deviceScale:2}), 2200);
assert.equal(automaticPreviewWidth({...base, viewportWidth:1001}), 1100);
assert.equal(automaticPreviewWidth({...base, zoom:2}), 2200);
assert.equal(automaticPreviewWidth({...base, crop:{w:.5,h:.5}}), 2200);
assert.equal(automaticPreviewWidth({...base, actualSize:true}), 6000);
assert.equal(automaticPreviewWidth({...base, deviceScale:2, zoom:20}), 6000);
assert.equal(automaticPreviewWidth({...base, sourceWidth:600,sourceHeight:400}), 600);
assert.equal(automaticPreviewWidth({...base, viewportWidth:0}), 1100);
assert.equal(automaticPreviewWidth({...base, sourceWidth:4000,sourceHeight:6000}), 900);
''')

    def test_selection_cache_hits_never_scan_catalog_and_mutations_refresh(self):
        self.run_js('''
const cached = createSummaryCache(), rows = Array(10000).fill({status:'pending'});
let visits = 0;
const compute = () => ({pending: rows.reduce((n,r) => { visits++; return n + (r.status === 'pending'); },0)});
for(let selection=0;selection<200;selection++) assert.equal(cached('revision:1|all', rows, compute).pending,10000);
assert.equal(visits,10000);
rows[0] = {status:'approved'};
assert.equal(cached('revision:2|all',rows,compute).pending,9999);
assert.equal(visits,20000);
cached('revision:2|folder',rows,compute);
assert.equal(visits,30000);
cached('revision:2|folder',[...rows],compute);
assert.equal(visits,40000);
''')

    def test_preview_preferences_start_automatic_and_only_retain_explicit_overrides(self):
        self.run_js('''
assert.equal(previewResolutionPreference(), 'auto');
assert.equal(previewResolutionPreference(null), 'auto');
assert.equal(previewResolutionPreference({pw: '900'}), 'auto');
assert.equal(previewResolutionPreference({pw: '5000'}), 'auto');
for (const value of ['auto', '900', '2600', '5000']) {
  const saved = JSON.parse(JSON.stringify({pw: '1100', previewResolution: value}));
  assert.equal(previewResolutionPreference(saved), value);
}
assert.equal(previewResolutionPreference({previewResolution: 2600}), '2600');
for (const value of ['', 'full', '999999', {}, -1]) {
  assert.equal(previewResolutionPreference({previewResolution: value}), 'auto');
}
''')

    def run_js(self, program):
        helper = (ROOT / 'web/view-performance.js').read_text()
        helper += '\n' + (ROOT / 'web/preview-preferences.js').read_text()
        script = "import assert from 'node:assert/strict';\n" + helper + '\n' + program
        result = subprocess.run(['node','--input-type=module','-e',script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
