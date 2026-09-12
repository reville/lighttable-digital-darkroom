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
            # A RAW source with the original interpretation takes the engines'
            # untouched path. A processed source still needs its expansion,
            # with no green interpretation at all.
            self.assertEqual(fp.rust_tuning_request(dict(params, linear_input=True)), {})
            spec = fp.rust_tuning_request(dict(params, linear_input=False))['input_tuning']
            self.assertEqual(spec['green_amount'], 0.0)
            self.assertEqual(spec['display_expansion'], ft.DISPLAY_EXPANSION)
            self.assertTrue(spec['input_cctf_decoding'])

    def test_display_expansion_is_for_processed_sources_only(self):
        raw = fp.rust_tuning_request(fp.clean_params({'stock':'kodak_portra_400','linear_input':True}))
        self.assertEqual(raw['input_tuning']['display_expansion'], 0.0)
        self.assertFalse(raw['input_tuning']['input_cctf_decoding'])
        processed = fp.rust_tuning_request(fp.clean_params({'stock':'kodak_portra_400'}))
        self.assertEqual(processed['input_tuning']['display_expansion'], ft.DISPLAY_EXPANSION)
        self.assertEqual(processed['input_tuning']['green_amount'], ft.TUNINGS['kodak_portra_400']['green_amount'])
        self.assertEqual(fp.rust_tuning_request(fp.clean_params({'profile_enabled':False})), {})

    def test_display_expansion_holds_middle_grey_and_frees_highlights(self):
        source = np.array([[[.18,.18,.18],[1.,.5,.05],[0.,-.01,.02],[.36,.09,.72]]], np.float32)
        spec = dict(version=1, green_amount=0., display_expansion=ft.DISPLAY_EXPANSION)
        out = ft.prepare_input(source, spec)
        np.testing.assert_allclose(out[0,0], source[0,0], atol=1e-7, rtol=0)
        # Highlights are scene-linear now and may exceed display white.
        self.assertGreater(float(out[0,1,0]), 3.9)
        self.assertLess(float(out[0,1,2]), float(source[0,1,2]))
        # Zero and negative values are untouched; the expansion is monotonic.
        self.assertEqual(float(out[0,2,0]), 0.)
        self.assertEqual(float(out[0,2,1]), float(source[0,2,1]))
        ramp = np.linspace(0, 1, 512, dtype=np.float32)[None, :, None].repeat(3, axis=2)
        expanded = ft.prepare_input(ramp, spec)[0, :, 0]
        self.assertGreater(float(np.diff(expanded).min()), 0)
        untouched = ft.prepare_input(source, dict(spec, display_expansion=0.))
        np.testing.assert_array_equal(untouched, source)
        with self.assertRaises(ValueError):
            ft.prepare_input(source, dict(spec, display_expansion=9.))

    def test_expansion_anchor_keeps_the_metered_mean(self):
        # The film meter is a centre-weighted mean of luminance; anchoring the
        # expansion at expansion_anchor() must leave that mean unchanged so
        # automatic exposure does not pull an expanded frame darker.
        def metered(img):
            lum = np.maximum(img @ ft.PROPHOTO_Y, 0); h, w = lum.shape; longest = max(h, w)
            x = (np.arange(w) / w - .5) * (w / longest); y = (np.arange(h) / h - .5) * (h / longest)
            weight = np.exp(-(x[None] ** 2 + y[:, None] ** 2) / (2 * ft.ANCHOR_SIGMA ** 2))
            return float((weight * lum).sum() / weight.sum())
        rng = np.random.default_rng(5)
        linear = (rng.random((120, 180, 3), dtype=np.float32) ** 2.2) * 0.9
        encoded = np.where(linear < 1/512, linear * 16, linear ** (1/1.8)).astype(np.float32)
        anchor = ft.expansion_anchor(encoded, encoded=True)
        self.assertNotAlmostEqual(anchor, ft.MIDDLE_GREY_LINEAR, places=2)
        spec = fp.rust_tuning_request({'stock': 'kodak_ektar_100'}, image=encoded)['input_tuning']
        self.assertAlmostEqual(spec['display_expansion_anchor'], anchor)
        out = ft.prepare_input(encoded, spec)
        before = metered(ft._romm_decode(encoded))
        self.assertLess(abs(metered(out) - before) / before, 0.01)
        # Without pixels the anchor is middle grey, and an explicit anchor wins.
        self.assertEqual(fp.rust_tuning_request({'stock': 'kodak_ektar_100'})['input_tuning']['display_expansion_anchor'], ft.MIDDLE_GREY_LINEAR)
        self.assertEqual(fp.rust_tuning_request({'stock': 'kodak_ektar_100'}, image=encoded, anchor=0.3)['input_tuning']['display_expansion_anchor'], 0.3)
        # RAW sources carry no anchor of interest and are never expanded.
        raw = fp.rust_tuning_request({'stock': 'kodak_portra_400', 'linear_input': True}, image=encoded)['input_tuning']
        self.assertEqual(raw['display_expansion'], 0.0)
        with self.assertRaises(ValueError):
            ft.prepare_input(encoded, dict(spec, display_expansion_anchor=0.0))

    def test_prepared_cli_input_keeps_expanded_highlights(self):
        import tempfile, tifffile
        from pathlib import Path
        # A varied frame: pixels above the anchor expand past display white.
        source = np.linspace(0.05, 0.98, 48, dtype=np.float32).reshape(4, 12, 1).repeat(3, axis=2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'in.tif'
            tifffile.imwrite(path, (source * 65535 + .5).astype(np.uint16), photometric='rgb')
            with fp.prepared_input_file(str(path), fp.clean_params({'stock':'kodak_ektar_100'})) as prepared:
                pixels = tifffile.imread(prepared)
                self.assertEqual(pixels.dtype, np.float32)
                self.assertGreater(float(pixels.max()), 1.0)
            with fp.prepared_input_file(str(path), fp.clean_params({'stock':'kodak_portra_400','linear_input':True})) as prepared:
                self.assertEqual(tifffile.imread(prepared).dtype, np.uint16)

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
        # With the original interpretation the processed source is still
        # expanded, so the transfer decode stays inside the preparation.
        self.assertFalse(fp.rust_params_json(dict(tuned,film_tuning='original'))['io']['input_cctf_decoding'])
        self.assertTrue(fp.rust_params_json(dict(tuned,film_tuning='original',linear_input=True))['io']['input_cctf_decoding'] is False)
        self.assertEqual(fp.rust_tuning_request(dict(tuned,film_tuning='original',linear_input=True)), {})
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

    def test_original_render_passes_exact_raw_source_to_upstream(self):
        source=np.full((2,2,3),.18,np.float32)
        with patch('spektrafilm.simulate',return_value=source) as simulate:
            fp.render_float(source,{'stock':'kodak_portra_160','linear_input':True})
            self.assertIs(simulate.call_args.args[0],source)

    def test_processed_source_is_expanded_before_upstream(self):
        source=np.array([[[.3,.3,.3],[.6,.6,.6]],[[.45,.45,.45],[.9,.9,.9]]],np.float32)
        with patch('spektrafilm.simulate',return_value=source) as simulate:
            fp.render_float(source,{'stock':'kodak_portra_160'})
            prepared = simulate.call_args.args[0]
            self.assertIsNot(prepared, source)
            # The transfer curve is decoded and the spread between a bright
            # and a dark pixel grows by the expansion power.
            decoded = ft._romm_decode(source.astype(np.float64))
            before = decoded[1,1,0] / decoded[0,0,0]
            after = prepared[1,1,0] / prepared[0,0,0]
            self.assertAlmostEqual(float(after), float(before ** ft.DISPLAY_EXPANSION), places=3)
            self.assertFalse(simulate.call_args.args[1].io.input_cctf_decoding)


if __name__ == '__main__':
    unittest.main()
