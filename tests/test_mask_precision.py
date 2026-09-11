# SPDX-License-Identifier: GPL-3.0-only
import base64
import json
from pathlib import Path
import subprocess
import unittest

import numpy as np

import edits


ROOT = Path(__file__).resolve().parents[1]


def stroke(**values):
    return dict(dict(size=.3, feather=.4, flow=.2, density=1,
                     buildUp=True, points=[[.2, .5], [.8, .5]]), **values)


class MaskPrecisionTests(unittest.TestCase):
    def test_flow_builds_per_stroke_and_density_caps_coverage(self):
        for count, expected in ((1, .2), (2, .4), (5, 1), (8, 1)):
            mask = edits.raster_mask({'type': 'brush', 'strokes': [stroke()] * count}, 101, 151)
            self.assertAlmostEqual(float(mask[50, 75]), expected, places=5)
        mask = edits.raster_mask({'type': 'brush', 'strokes': [stroke(density=.35)] * 5}, 101, 151)
        self.assertAlmostEqual(float(mask[50, 75]), .35, places=5)

    def test_stroke_coverage_does_not_depend_on_pointer_event_frequency(self):
        def render(points):
            return edits.raster_mask({'type': 'brush', 'strokes': [stroke(points=points)]}, 101, 151)
        a = render([[.2, .5], [.8, .5]])
        b = render([[float(x), .5] for x in np.linspace(.2, .8, 100)])
        np.testing.assert_allclose(a, b, atol=2e-6)

    def test_legacy_strokes_keep_their_saved_strength(self):
        old = stroke(); del old['buildUp']
        one = edits.raster_mask({'type': 'brush', 'strokes': [old]}, 101, 151)
        many = edits.raster_mask({'type': 'brush', 'strokes': [old] * 5}, 101, 151)
        np.testing.assert_array_equal(one, many)

    def test_saved_auto_mask_restricts_paint_and_survives_roundtrip(self):
        gate = np.zeros((101, 151), dtype=np.uint8); gate[:, :75] = 255
        import io
        from PIL import Image
        output = io.BytesIO(); Image.fromarray(gate).save(output, format='PNG')
        bitmap = {'width': 151, 'height': 101, 'encoding': 'png',
                  'data': base64.b64encode(output.getvalue()).decode()}
        mask = {'type': 'brush', 'strokes': [stroke(flow=1, edgeMask=bitmap)]}
        saved = edits.clean_masks(edits.clean_masks([mask]))[0]
        weight = edits.raster_mask(saved, 101, 151)
        self.assertGreater(float(weight[50, 65]), .9)
        self.assertEqual(float(weight[:, 75:].max()), 0)
        self.assertEqual(saved['strokes'][0]['edgeMask'], bitmap)

    def test_ellipse_rotation_and_legacy_circle(self):
        mask = {'type': 'radial', 'center': [.5, .5], 'radius': .2,
                'radiusX': .4, 'radiusY': .1, 'feather': 0}
        horizontal = edits.raster_mask(mask, 101, 101)
        vertical = edits.raster_mask(dict(mask, angle=90), 101, 101)
        self.assertEqual(horizontal[50, 75], 1)
        self.assertEqual(horizontal[75, 50], 0)
        np.testing.assert_array_equal(horizontal.T, vertical)
        legacy = {k: v for k, v in mask.items() if k not in ('radiusX', 'radiusY')}
        circle = edits.raster_mask(legacy, 101, 101)
        np.testing.assert_array_equal(circle, circle.T)
        saved = edits.clean_masks(edits.clean_masks([mask]))[0]
        self.assertEqual(saved['radiusX'], .4)
        self.assertEqual(saved['radiusY'], .1)

    def test_damaged_auto_mask_never_becomes_unrestricted_paint(self):
        bad = stroke(edgeMask={'width': 2, 'height': 1, 'data': 'damaged'})
        for field in ('strokes', 'addStrokes', 'subtractStrokes', 'intersectStrokes'):
            mask = {'type': 'brush', field: [bad]}
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, 'Saved Auto Mask data'):
                    edits.clean_masks([mask])
                with self.assertRaisesRegex(ValueError, 'Saved Auto Mask data'):
                    edits.require_saved_mask_assets([mask])

    def test_local_curve_is_retained_and_affects_only_the_mask(self):
        ramp = np.linspace(0, 1, 151, dtype=np.float32)
        image = np.tile(ramp[None, :, None], (101, 1, 3))
        lut = (np.linspace(0, 1, 256) ** .7).tolist()
        mask = {'type': 'radial', 'radius': .25, 'feather': 0,
                'grade': {'curveL': lut, 'whites': .2, 'blacks': -.1}}
        saved = edits.clean_masks(edits.clean_masks([mask]))[0]
        self.assertEqual(saved['grade']['curveL'], lut)
        self.assertEqual(saved['grade']['whites'], .2)
        self.assertEqual(saved['grade']['blacks'], -.1)
        output = edits.apply_masks(image, [mask])
        self.assertGreater(float(output[50, 75, 0]), float(image[50, 75, 0]) + .05)
        np.testing.assert_array_equal(output[:, :20], image[:, :20])
        curve_only = dict(mask, grade={'curveL': lut})
        actual = edits.apply_masks(image, [curve_only])[50, 75, 0]
        self.assertAlmostEqual(float(actual), float(np.interp(.5, np.linspace(0, 1, 256), lut)), places=6)

    def test_python_browser_analytic_brush_agree(self):
        strokes = [stroke(points=[[.13, .22], [.38, .76], [.9, .44]], flow=.17),
                   stroke(points=[[.55, .5]], density=.4), stroke(flow=.33)]
        script = '''
import {strokeCoverage, accumulateStroke} from './web/mask-raster.js';
const strokes = JSON.parse(process.argv[1]);
const values = new Float32Array(151*101);
for (const stroke of strokes) accumulateStroke(values, strokeCoverage(stroke,151,101),stroke);
process.stdout.write(JSON.stringify(Array.from(values)));
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(strokes)],
                                cwd=ROOT, capture_output=True, text=True, check=True, timeout=20)
        browser = np.array(json.loads(result.stdout)).reshape(101, 151)
        python = edits.raster_mask({'type': 'brush', 'strokes': strokes}, 101, 151)
        np.testing.assert_allclose(browser, python, atol=2e-6)
