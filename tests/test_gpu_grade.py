"""Reference parity, fallback and float export integration for GPU finishing.

Set LIGHTTABLE_TEST_GPU=1 on a host with the bundled engine for device checks.
"""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

import edits
import grade
import gpu_compute


DETAIL = dict(texture=.3, clarity=.25, sharpness=.4, sharpenRadius=1.5,
              sharpenMasking=.3, luminanceNoise=.25, colorNoise=.25)
COLOR = dict(hsl={c: dict(h=.1, s=.15, l=.05) for c in ('red', 'orange', 'blue')},
             pointColor=[dict(hue=35, range=30, saturation=.15, luminance=.1,
                             refSaturation=.4, refLuminance=.65,
                             uniformHue=.2, uniformSaturation=.3, uniformLuminance=0),
                         dict(hue=210, range=40, hueShift=8, uniformHue=.1)],
             curveL=[(i / 255) ** .9 for i in range(256)],
             curveR=[(i / 255) ** 1.1 for i in range(256)],
             curveB=[(i / 255) ** .95 for i in range(256)])
TONES = dict(exposure=.35, contrast=.15, shadows=.2, highlights=-.2, temp=.08,
             tint=-.1, vibrance=.15, saturation=-.12, whites=.2, blacks=-.1,
             dehaze=.17, vignette=-.2)
WHEELS = dict(colorGrading={
    'shadows': dict(hue=213, saturation=.3, luminance=-.2),
    'midtones': dict(hue=25, saturation=.25, luminance=.1),
    'highlights': dict(hue=55, saturation=.2, luminance=-.1),
    'global': dict(hue=90, saturation=.1, luminance=.03), 'balance': .2, 'blending': .4})
MONOCHROME = dict(monochrome=1.0, hsl={c: dict(h=0, s=0, l=.2) for c in ('red', 'green', 'blue')})
SENSITIVE = dict(DETAIL, **COLOR, **TONES, **WHEELS)
SENSITIVE['pointColor'] = [dict(COLOR['pointColor'][0], uniformLuminance=.2), COLOR['pointColor'][1]]


def fixture(h=503, w=509):
    rng = np.random.default_rng(7419)
    c = rng.uniform(-.1, 1.1, (h, w, 3)).astype(np.float32)
    c[:h // 5] *= .001  # near-black transfer function
    c[h // 5:2 * h // 5] = [.65, .43, .34]  # neutral skin-like patch
    c[-1] = np.linspace(0, 1, w)[:, None]  # smooth precision ramp
    return c


class GpuGradeRoutingTests(unittest.TestCase):
    def test_missing_worker_at_cold_start_falls_back_without_losing_pixels(self):
        image = fixture(513, 515)
        expected = grade.apply(image, DETAIL)
        gpu_compute.close()
        with tempfile.TemporaryDirectory(prefix='lighttable-missing-worker-') as temp, \
                mock.patch.object(gpu_compute, '_BINARY', Path(temp) / 'missing-engine'), \
                mock.patch.object(gpu_compute, '_RETRY_AFTER', 0.0):
            actual = grade.apply_accelerated(image, DETAIL)
        np.testing.assert_array_equal(actual, expected)
        self.assertIsNone(gpu_compute._PROCESS)

    def test_near_black_uniformity_regression_retains_reference_precision(self):
        # This exact 17x17 capture crop exposed amplification of <2e-8 tone
        # differences into a 5-code RGB16 error. Tiling preserves the center
        # pixel's neighbor context while exceeding the GPU dispatch threshold.
        crop = np.load(Path(__file__).with_name('fixtures') / 'gpu-grade-dark-uniformity.npy')
        image = np.tile(crop, (32, 32, 1))
        expected = grade.apply(image, SENSITIVE)
        with mock.patch.object(gpu_compute, 'compute') as compute:
            actual = grade.apply_accelerated(image, SENSITIVE)
        compute.assert_not_called()
        np.testing.assert_array_equal(actual, expected)

    def test_point_color_without_luminance_lift_remains_eligible(self):
        image = fixture(513, 515)
        for point in [dict(COLOR['pointColor'][0], uniformLuminance=0),
                      dict(COLOR['pointColor'][0], uniformLuminance=.2, refLuminance=0),
                      dict(hue=30, saturation=.2, luminance=.2, uniformLuminance=.5)]:
            with self.subTest(point=point), \
                    mock.patch.object(gpu_compute, 'compute', return_value=image) as compute:
                grade.apply_accelerated(image, {'pointColor': [point]})
            compute.assert_called_once()

    def test_basic_tone_retains_faster_numba_route(self):
        image = fixture(513, 515)
        with mock.patch.object(grade, '_HAS_NUMBA', True), \
                mock.patch.object(gpu_compute, 'compute') as compute:
            actual = grade.apply_accelerated(image, TONES)
        compute.assert_not_called()
        np.testing.assert_array_equal(actual, grade.apply(image, TONES))

    def test_identity_and_small_region_do_not_start_gpu(self):
        image = fixture(17, 19)
        with mock.patch.object(gpu_compute, 'compute') as compute:
            self.assertIs(grade.apply_accelerated(image, {}), image)
            np.testing.assert_array_equal(grade.apply_accelerated(image, DETAIL), grade.apply(image, DETAIL))
        compute.assert_not_called()

    def test_failed_gpu_uses_reference_without_mutating_input(self):
        image = fixture(513, 515)
        original = image.copy()
        expected = grade.apply(image, DETAIL)
        for error in [RuntimeError('no adapter'), OSError('missing binary'),
                      TimeoutError('device lost')]:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(gpu_compute, 'compute', side_effect=error):
                np.testing.assert_array_equal(grade.apply_accelerated(image, DETAIL), expected)
        np.testing.assert_array_equal(image, original)

    def test_mask_acceleration_preserves_reference_composition_and_default(self):
        image = fixture(513, 515)
        masks = [dict(type='radial', center=[.4, .4], radius=.4, grade=DETAIL),
                 dict(type='brush', strokes=[dict(size=.4, feather=.6, flow=.7,
                       points=[[.1, .2], [.9, .8]])], grade={'exposure': .2})]
        expected = edits.apply_masks(image, masks)
        with mock.patch.object(grade, 'apply_accelerated', side_effect=grade.apply) as apply:
            actual = edits.apply_masks(image, masks, accelerated=True)
        self.assertEqual(apply.call_count, 2)
        np.testing.assert_array_equal(actual, expected)


@unittest.skipUnless(os.environ.get('LIGHTTABLE_TEST_GPU') == '1', 'host GPU test opt-in')
class GpuGradeDeviceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        gpu_compute.close()

    def compare(self, image, settings, tolerance=2e-5):
        cleaned = grade.clean(settings)
        actual = gpu_compute.compute('grade', image,
            [image.shape[1], image.shape[0]], image.shape, grade=cleaned)
        expected = grade.apply(image, cleaned)
        self.assertEqual(actual.dtype, np.float32)
        self.assertTrue(np.isfinite(actual).all())
        np.testing.assert_allclose(actual, expected, rtol=0, atol=tolerance)

    def test_every_grade_stage_and_combination(self):
        image = fixture()
        for settings in [DETAIL, COLOR, TONES, WHEELS, MONOCHROME,
                         dict(DETAIL, **COLOR, **TONES, **WHEELS)]:
            with self.subTest(settings=list(settings)):
                self.compare(image, settings)

    def test_vignette_shape_matches_reference(self):
        for settings in [dict(vignette=.65, vignetteSize=.2, vignetteFeather=.8),
                         dict(vignette=-.65, vignetteSize=.3, vignetteFeather=0),
                         dict(vignette=.65, vignetteSize=.8, vignetteFeather=1)]:
            with self.subTest(settings=settings):
                self.compare(fixture(), settings)

    def test_odd_single_pixel_axes_and_noncontiguous_input(self):
        for image in [fixture(1, 73), fixture(71, 1), fixture(97, 101)[::2, ::3]]:
            with self.subTest(shape=image.shape):
                self.compare(image, dict(DETAIL, **COLOR, **TONES))

    def test_export_sized_dispatch_does_not_silently_fallback(self):
        image = fixture(513, 515)
        with mock.patch.object(grade, 'apply', side_effect=AssertionError('CPU fallback')):
            actual = grade.apply_accelerated(image, COLOR)
        np.testing.assert_allclose(actual, grade.apply(image, COLOR), rtol=0, atol=2e-5)

    def test_chromatic_aberration_keeps_exact_geometry_before_gpu_grade(self):
        image = fixture(513, 515)
        settings = dict(DETAIL, chromaticAberrationRedCyan=.4,
                        chromaticAberrationBlueYellow=-.6)
        expected = grade.apply(image, settings)
        with mock.patch.object(grade, 'apply', side_effect=AssertionError('CPU grade fallback')):
            actual = grade.apply_accelerated(image, settings)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-5)

    def test_resident_export_gpu_grade_then_masks_and_crop_matches_rust_reference(self):
        import film_pipeline as film
        import tifffile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='lighttable-direct-grade-') as temp:
            directory = Path(temp)
            source = directory / 'input.tif'
            tifffile.imwrite(source, (np.clip(fixture(127, 193), 0, 1) * 65535 + .5).astype(np.uint16),
                             photometric='rgb')
            params = film.clean_params(dict(linear_input=False, auto_exposure=False,
                grain_on=False, halation_on=False, glare_on=False, couplers_on=False,
                scan_sharpen=False, output_recipe='clean_scan'))
            base = dict(input=str(source), data_dir=str(root / 'engine/data'),
                film=params['stock'], paper=params['paper'], params=film.rust_params_json(params),
                bit_depth=32, rotate_quarters_ccw=1,
                masks=edits.clean_masks([dict(type='radial', center=[.4, .5], radius=.3,
                    opacity=.7, grade=dict(exposure=.2))]),
                crop=dict(x=.11, y=.17, w=.73, h=.61), long_edge=97)
            cases = [grade.clean(dict(DETAIL, **COLOR, **TONES, **WHEELS)),
                     grade.clean(dict(DETAIL, chromaticAberrationRedCyan=.5)),
                     grade.clean(SENSITIVE)]
            rendered = {}
            for mode in ['0', '1']:
                requests = [dict(base, id=i + 1, grade=settings,
                    output=str(directory / f'{mode}-{i}.tif')) for i, settings in enumerate(cases)]
                result = subprocess.run([str(root / 'engine/lighttable-engine')],
                    input=''.join(json.dumps(request) + '\n' for request in requests),
                    capture_output=True, text=True, timeout=120,
                    env=dict(os.environ, LIGHTTABLE_GPU_COMPUTE=mode, SPEKTRAFILM_BACKEND='wgpu'))
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                responses = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(len(responses), len(cases))
                for i, response in enumerate(responses):
                    self.assertTrue(response['ok'], response)
                    self.assertIn('gpu', response['backend'].lower())
                    self.assertEqual(response['grade_gpu'], mode == '1' and i == 0)
                    rendered[mode, i] = tifffile.imread(requests[i]['output'])
            for i in range(len(cases)):
                np.testing.assert_allclose(rendered['1', i], rendered['0', i], rtol=0, atol=2e-5)
