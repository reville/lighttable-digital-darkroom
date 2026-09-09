from types import SimpleNamespace
from unittest import TestCase, mock
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
