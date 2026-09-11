# SPDX-License-Identifier: GPL-3.0-only
"""Behavioral guardrails for the versioned interpretation, not film accuracy."""
import unittest
from unittest.mock import patch
import numpy as np

import film_pipeline as fp
import film_tuning as ft


class FilmTuningTests(unittest.TestCase):
    def test_legacy_and_unknown_interpretations_preserve_original(self):
        for params in ({'stock':'kodak_portra_160'},
                       {'stock':'kodak_ektar_100','film_tuning':'lighttable'},
                       {'stock':'kodak_portra_160','film_tuning':'lighttable','film_tuning_version':'999'}):
            clean = fp.clean_params(params)
            self.assertEqual(clean['film_tuning'], 'original')
            self.assertEqual(fp.rust_tuning_request(params), {})

    def test_default_film_tuning_is_lighttable_portra_400(self):
        for params in ({}, {'profile_enabled': True}, {'profile_enabled': False}):
            clean = fp.clean_params(params)
            self.assertEqual(clean['stock'], 'kodak_portra_400')
            self.assertEqual(clean['film_tuning'], 'lighttable')
            self.assertEqual(clean['film_tuning_version'], '1')

    def test_three_separate_versioned_variants_keep_stock_and_profile_data(self):
        for stock in ft.TUNINGS:
            clean = fp.clean_params({'stock':stock,'film_tuning':'lighttable','linear_input':True})
            self.assertEqual(clean['stock'], stock)
            self.assertEqual(clean['film_tuning_version'], '1')
            self.assertEqual(fp.PROFILE_BY_ID[stock]['tunings'][0]['id'], 'lighttable')
            self.assertEqual(fp.rust_tuning_request(clean)['input_tuning']['green_amount'], ft.TUNINGS[stock]['green_amount'])

    def test_transfer_decode_happens_once_and_film_off_has_no_tuning(self):
        tuned = {'stock':'kodak_portra_160','film_tuning':'lighttable','linear_input':False}
        self.assertTrue(fp.rust_tuning_request(tuned)['input_tuning']['input_cctf_decoding'])
        self.assertFalse(fp.rust_params_json(tuned)['io']['input_cctf_decoding'])
        self.assertFalse(fp.build_params(tuned).io.input_cctf_decoding)
        self.assertTrue(fp.rust_params_json(dict(tuned,film_tuning='original'))['io']['input_cctf_decoding'])
        self.assertEqual(fp.rust_tuning_request(dict(tuned,profile_enabled=False)), {})

    def test_neutrals_skin_yellows_blues_and_cyans_are_unchanged(self):
        srgb = np.array([[[.18,.18,.18],[.8,.5,.3],[.7,.7,.02],
                          [.01,.1,.8],[.02,.7,.7],[0,0,0],[1,1,1]]])
        source = (srgb @ np.linalg.inv(ft.TO_SRGB).T).astype(np.float32)
        out = ft.prepare_input(source,dict(version=1,green_amount=.9))
        np.testing.assert_allclose(out,source,atol=1e-7,rtol=0)

    def test_source_greens_change_without_changing_luminance_or_input(self):
        source = np.array([[[.038,.046,.017],[.3,.6,.05],[.7,.95,.3]]],np.float32)
        before = source.copy()
        out = ft.prepare_input(source,dict(version=1,green_amount=.9))
        self.assertGreater(float(np.max(abs(out-source))),.005)
        np.testing.assert_allclose(out@ft.PROPHOTO_Y,source@ft.PROPHOTO_Y,atol=5e-8,rtol=0)
        np.testing.assert_array_equal(source,before)
        self.assertGreaterEqual(float(out.min()),0)
        self.assertLessEqual(float(out.max()),1)

    def test_dense_hue_sweep_is_continuous_and_ordered(self):
        hue = np.linspace(59,121,6201)
        # G=max, B=min on the tested yellow-green sector; include boundaries.
        rgb = np.stack([.05+.55*(120-hue)/60,np.full_like(hue,.6),np.full_like(hue,.05)],-1)
        source = (rgb@np.linalg.inv(ft.TO_SRGB).T)[None].astype(np.float32)
        out = ft.prepare_input(source,dict(version=1,green_amount=.9))
        self.assertTrue(np.isfinite(out).all())
        self.assertLess(float(np.max(abs(np.diff(out[0],axis=0)))),.0003)
        back = out[0]@ft.TO_SRGB.T
        angle = 120+60*(back[:,2]-back[:,0])/(back[:,1]-back[:,2])
        self.assertGreater(float(np.diff(angle).min()),0)

    def test_encoded_and_linear_input_match(self):
        linear = np.array([[[.001,.002,.001],[.038,.046,.017],[.3,.6,.05]]],np.float32)
        encoded = np.where(linear<1/512,linear*16,linear**(1/1.8))
        a=ft.prepare_input(linear,dict(version=1,green_amount=.8))
        b=ft.prepare_input(encoded,dict(version=1,green_amount=.8,input_cctf_decoding=True))
        np.testing.assert_allclose(a,b,atol=2e-7,rtol=0)

    def test_preset_roundtrip_and_legacy_patch_keep_interpretation(self):
        import preset_library as library
        import preset_io
        look = library.prepare_look(dict(name='Tuned Portra',includeFilm=True,
            params=dict(stock='kodak_portra_160',film_tuning='lighttable',film_tuning_version='1')))
        library.validate_look(look)
        filename, _, encoded = preset_io.export_preset(look, 'lighttable')
        imported = preset_io._import_bytes(filename, encoded.encode())[0]
        self.assertEqual(imported['params']['film_tuning'], 'lighttable')
        self.assertEqual(imported['params']['film_tuning_version'], '1')
        patch = library.look_patch(dict(params={'stock':'kodak_portra_160'},filmMode='on'))
        self.assertEqual(patch['params']['film_tuning'], 'original')
        self.assertEqual(patch['params']['film_tuning_version'], '1')

    def test_original_render_passes_exact_source_to_upstream(self):
        source=np.full((2,2,3),.18,np.float32)
        with patch('spektrafilm.simulate',return_value=source) as simulate:
            fp.render_float(source,{'stock':'kodak_portra_160'})
            self.assertIs(simulate.call_args.args[0],source)


if __name__ == '__main__':
    unittest.main()
