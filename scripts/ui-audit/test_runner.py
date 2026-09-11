# SPDX-License-Identifier: GPL-3.0-only
"""Harness self-checks, run explicitly; these do not audit application visuals."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run as audit


class RunnerTests(unittest.TestCase):
    def test_full_matrix_uses_every_shipped_locale_pane_zoom_and_viewport(self):
        cases, panes = audit.matrix('full')
        manifest = json.loads((audit.ROOT/'web/locales/manifest.json').read_text())
        self.assertEqual({c['locale'] for c in cases}, {l['code'] for l in manifest['locales']})
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        for p in panes:
            self.assertEqual({c['zoom'] for c in cases if c.get('pane') == p}, {'fit','actual','in','out'})
        self.assertEqual({(c['viewport']['width'],c['viewport']['height']) for c in cases}, {(1280,720),(1440,900),(1728,1117)})
        self.assertTrue(all(c['direction']=='rtl' for c in cases if c['locale']=='ar'))

    def test_unknown_state_and_locale_fail(self):
        with self.assertRaises(ValueError): audit.matrix('quick',states=['typo'])
        with self.assertRaises(ValueError): audit.matrix('quick',locales=['typo'])

    def test_pixel_comparison_detects_shift_and_wrong_size(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);a=Image.new('RGB',(100,100),'white');ImageDraw.Draw(a).rectangle((5,5,30,30),fill='black');a.save(d/'a.png')
            self.assertEqual(audit.compare(d/'a.png',d/'a.png',d/'diff.png')['status'],'matched')
            b=Image.new('RGB',(100,100),'white');ImageDraw.Draw(b).rectangle((15,5,40,30),fill='black');b.save(d/'b.png')
            self.assertEqual(audit.compare(d/'a.png',d/'b.png',d/'diff.png')['status'],'changed')
            b.resize((90,100)).save(d/'b.png')
            self.assertEqual(audit.compare(d/'a.png',d/'b.png',d/'diff.png')['status'],'different-size')

    def test_baselines_require_review_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);source=d/'run';source.mkdir();dest=d/'baseline'
            audit.write(source/'review.json', {'reviewStatus':'not-reviewed','inspectedEvidence':[]})
            audit.write(source/'result.json', {'executionStatus':'passed','records':[{'id':'case','screenshot':'a.png','masked':'b.png'}]})
            audit.write(source/'provenance.json', {'mode':'snapshot','comparison':{}})
            (source/'a.png').write_bytes(b'original');(source/'b.png').write_bytes(b'masked')
            with self.assertRaises(ValueError): audit.approve(source,dest,'test reviewer')
            audit.write(source/'review.json', {'reviewStatus':'reviewed','inspectedEvidence':['a.png']})
            with self.assertRaises(ValueError): audit.approve(source,dest,'test reviewer')
            audit.write(source/'review.json', {'reviewStatus':'reviewed','inspectedEvidence':['a.png','b.png']})
            audit.approve(source,dest,'test reviewer')
            self.assertEqual(json.loads((dest/'baseline.json').read_text())['images']['case']['sha256'],audit.digest(source/'b.png'))
            with self.assertRaises(FileExistsError): audit.approve(source,dest,'test reviewer')

    def test_transients_reuse_native_detector_and_real_timestamps(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);frames=[]
            for i,c in enumerate(['black','white','black']):
                Image.new('RGB',(80,46),c).save(d/f'{i}.jpg');frames.append({'file':f'{i}.jpg','timestamp':100+i*.04})
            audit.write(d/'example-frames.json',{'frames':frames,'capped':False})
            result=audit.frames_analysis(d)
            self.assertEqual(result[0]['candidates'][0]['timestamp'],100.04)
            self.assertEqual(result[0]['candidates'][0]['evidence'],['0.jpg','1.jpg','2.jpg'])

if __name__=='__main__': unittest.main()
