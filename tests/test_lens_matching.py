# SPDX-License-Identifier: GPL-3.0-only
import sys
from types import SimpleNamespace
from unittest import TestCase, mock

import numpy as np

import edits


class LensMatchingTests(TestCase):
    def setUp(self):
        self.camera = SimpleNamespace(maker='Example', model='Camera', crop_factor=1.5)
        self.a = SimpleNamespace(maker='Example', model='24-70 A', min_focal=24, max_focal=70,
                                 score=90, calib_distortion=[1], calib_vignetting=[1], calib_tca=[])
        self.b = SimpleNamespace(**dict(vars(self.a), model='24-70 B', score=100))
        self.meta = {'Make': 'Example', 'Model': 'Camera', 'FocalLength': '35 mm', 'FNumber': '5.6'}
        self.db = mock.Mock()
        self.db.find_cameras.return_value = [self.camera]
        self.db.find_lenses.return_value = [self.a, self.b]

    def test_missing_lens_name_never_picks_the_highest_of_two_scores(self):
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            result = edits.lens_match_for(self.meta)
        self.assertFalse(result['found'])
        self.assertIn('Multiple lens profiles', result['reason'])
        self.assertEqual([v['lensModel'] for v in result['candidates']], ['24-70 A', '24-70 B'])

    def test_explicit_choice_survives_cleaning_and_selects_lower_scored_lens(self):
        override = dict(cameraMaker='Example', cameraModel='Camera', lensMaker='Example', lensModel='24-70 A')
        optics = edits.clean_optics({'profileEnabled': True, 'profileOverride': override})
        self.assertEqual(optics['profileOverride'], override)
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            result = edits.lens_match_for(self.meta, optics['profileOverride'])
        self.assertTrue(result['found'])
        self.assertEqual(result['profile']['lensModel'], '24-70 A')
        self.assertTrue(result['profile']['manualOverride'])

    def test_invalid_saved_override_does_not_fall_back_to_another_lens(self):
        override = dict(cameraMaker='Example', cameraModel='Other Camera', lensMaker='Example', lensModel='24-70 A')
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            self.assertIsNone(edits.lens_profile_for(self.meta, override))

    def test_exact_name_outside_calibrated_focal_range_is_rejected(self):
        self.db.find_lenses.return_value = [self.a]
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            result = edits.lens_match_for(dict(self.meta, LensModel='24-70 A', FocalLength='200 mm'))
        self.assertFalse(result['found'])
        self.assertEqual(result['candidates'], [])

    def test_unique_fixed_lens_still_matches_and_explains_inference(self):
        self.db.find_lenses.return_value = [self.a]
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            result = edits.lens_match_for(self.meta)
        self.assertTrue(result['found'])
        self.assertIn('Only one', result['reason'])

    def test_multiple_loose_name_matches_are_not_silently_ranked(self):
        self.db.find_lenses.side_effect = lambda *args, **kw: [self.a, self.b] if kw.get('lens') is None or kw.get('loose_search') else []
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            self.assertIsNone(edits.lens_profile_for(dict(self.meta, LensModel='generic 24-70')))

    def test_exact_metadata_match_indicates_confident_auto_activation(self):
        self.db.find_lenses.return_value = [self.a]
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            result = edits.lens_match_for(dict(self.meta, LensModel='24-70 A'))
        self.assertTrue(result['found'])
        self.assertEqual(result['reason'], "Exact camera and lens metadata match.")
        self.assertEqual(result['profile']['lensModel'], '24-70 A')

    def test_only_an_exact_metadata_match_is_confident(self):
        with mock.patch.object(edits, '_lens_database', return_value=self.db):
            self.db.find_lenses.return_value = [self.a]
            exact = edits.lens_match_for(dict(self.meta, LensModel='24-70 A'))
            inferred = edits.lens_match_for(self.meta)
            override = dict(cameraMaker='Example', cameraModel='Camera',
                            lensMaker='Example', lensModel='24-70 A')
            chosen = edits.lens_match_for(dict(self.meta, LensModel='24-70 A'), override)
            self.db.find_lenses.side_effect = lambda *args, **kw: (
                [self.a] if kw.get('lens') is None or kw.get('loose_search') else [])
            loose = edits.lens_match_for(dict(self.meta, LensModel='generic 24-70'))
            self.db.find_lenses.side_effect = None
            self.db.find_lenses.return_value = [self.a, self.b]
            ambiguous = edits.lens_match_for(self.meta)
        self.assertTrue(exact['found'] and exact['confident'])
        self.assertTrue(inferred['found'])
        self.assertFalse(inferred['confident'])
        self.assertTrue(chosen['found'])
        self.assertFalse(chosen['confident'])
        self.assertTrue(loose['found'])
        self.assertFalse(loose['confident'])
        self.assertFalse(ambiguous['found'] or ambiguous['confident'])
        self.assertFalse(edits.lens_match_for({})['confident'])


class FakeModifyFlags:
    DISTORTION = 1
    TCA = 2
    SCALE = 4
    VIGNETTING = 8


class LensProfileFlagTests(TestCase):
    """The profile switches map onto separate remap flags."""

    def setUp(self):
        self.calls = []
        tests = self

        class Modifier:
            def __init__(self, lens, crop, width, height):
                self.width, self.height = width, height

            def initialize(self, focal, aperture, distance, scale, pixel_format, flags):
                tests.calls.append(('flags', flags))

            def apply_color_modification(self, source):
                tests.calls.append(('vignette',))

            def apply_subpixel_geometry_distortion(self):
                tests.calls.append(('remap',))
                return None

        self.module = SimpleNamespace(ModifyFlags=FakeModifyFlags, Modifier=Modifier)
        camera = SimpleNamespace(maker='Example', model='Camera', crop_factor=1.5)
        lens = SimpleNamespace(maker='Example', model='24-70 A', min_focal=24)
        self.resolved = mock.patch.object(edits, '_resolve_lens_profile', return_value=(camera, lens))
        self.resolved.start(); self.addCleanup(self.resolved.stop)
        self.spec = {'focal': 35, 'aperture': 5.6}
        self.image = np.zeros((8, 12, 3), dtype=np.float32)

    def flags_for(self, **optics):
        self.calls.clear()
        with mock.patch.dict(sys.modules, {'lensfunpy': self.module}):
            edits.apply_lens_profile(self.image, {'profileEnabled': True, **optics}, self.spec)
        flags = [call[1] for call in self.calls if call[0] == 'flags'][0]
        return flags, [call[0] for call in self.calls if call[0] != 'flags']

    def test_chromatic_aberration_is_its_own_switch(self):
        flags, steps = self.flags_for()
        self.assertEqual(flags, FakeModifyFlags.DISTORTION | FakeModifyFlags.SCALE
                         | FakeModifyFlags.TCA | FakeModifyFlags.VIGNETTING)
        flags, steps = self.flags_for(profileChromatic=False)
        self.assertFalse(flags & FakeModifyFlags.TCA)
        self.assertTrue(flags & FakeModifyFlags.DISTORTION)
        self.assertIn('remap', steps)
        flags, steps = self.flags_for(profileDistortion=False, profileVignette=False)
        self.assertEqual(flags, FakeModifyFlags.TCA)
        self.assertEqual(steps, ['remap'])
        flags, steps = self.flags_for(profileDistortion=False, profileChromatic=False)
        self.assertEqual(flags, FakeModifyFlags.VIGNETTING)
        self.assertEqual(steps, ['vignette'])


class LensDatabaseInfoTests(TestCase):
    def setUp(self):
        edits.lens_database_info.cache_clear()
        self.addCleanup(edits.lens_database_info.cache_clear)

    def test_reports_the_runtime_library_version_and_record_counts(self):
        module = SimpleNamespace(lensfun_version=(0, 3, 4, 0), __version__='1.18.0')
        database = SimpleNamespace(cameras=[1, 2, 3], lenses=[1, 2])
        with mock.patch.dict(sys.modules, {'lensfunpy': module}), \
                mock.patch.object(edits, '_lens_database', return_value=database):
            info = edits.lens_database_info()
        self.assertEqual(info, {'available': True, 'version': '0.3.4', 'binding': '1.18.0',
                                'cameras': 3, 'lenses': 2})

    def test_unavailable_library_is_reported_rather_than_claimed(self):
        with mock.patch.object(edits, '_lens_database', side_effect=RuntimeError('missing')):
            info = edits.lens_database_info()
        self.assertFalse(info['available'])
        self.assertEqual(info['version'], '')
