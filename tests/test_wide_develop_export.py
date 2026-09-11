# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic color/geometry fixtures; never opens a photographer's originals."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import colour
import numpy as np
import tifffile

import color_pipeline as color
import film_pipeline
import render_cli
import server


def raw_patches():
    # Colors within P3 but outside sRGB, neutral mid-grey, and a white anchor.
    p3 = np.array([[[.7, .04, .04], [.04, .65, .05], [.04, .06, .6],
                    [.18, .18, .18], [1., 1., 1.]]])
    linear = colour.RGB_to_RGB(
        p3, 'Display P3', 'ProPhoto RGB',
        chromatic_adaptation_transform='Bradford')
    return np.round(np.clip(linear, 0, 1) * 65535).astype(np.uint16)


def base_job(space='display_p3'):
    return {'params': film_pipeline.clean_params({
                'profile_enabled': False, 'linear_input': False,
                'developProfile': 'linear'}),
            'grade': {}, 'masks': [], 'heals': [], 'optics': {},
            'watermark': {'enabled': False}, 'outputSpace': space,
            'format': 'tif', 'bitDepth': 16, 'metadata': 'none'}


class WideDevelopColorTests(unittest.TestCase):
    def test_saturated_raw_keeps_colors_discarded_by_srgb_intermediate(self):
        raw = raw_patches()
        for space in ('display_p3', 'prophoto'):
            with self.subTest(space=space):
                wide = color.linear_prophoto_to_display(
                    raw, develop_profile='linear', output_space=space)
                preview = color.linear_prophoto_to_display_srgb(
                    raw, develop_profile='linear')
                previously = color.convert_output_space(preview, space)
                self.assertGreater(float(np.max(abs(wide - previously))), .05)
                self.assertTrue(np.all(np.isfinite(wide)))
                self.assertGreaterEqual(float(wide.min()), 0)
                self.assertLessEqual(float(wide.max()), 1)
                # Display the delivery under the preview's profile. The same
                # tones appear, with only the preview's expected gamut clip.
                displayed = color.convert_output_space(
                    wide, 'srgb', input_space=space)
                np.testing.assert_allclose(displayed, preview, atol=2e-4)
                np.testing.assert_allclose(displayed[:, 3:4], preview[:, 3:4],
                                           atol=5e-5)

    def test_every_develop_profile_keeps_srgb_preview_contract(self):
        raw = np.random.default_rng(4).integers(0, 65536, (9, 13, 3), np.uint16)
        for profile in ('linear', 'standard', 'soft'):
            preview = color.linear_prophoto_to_display_srgb(
                raw, develop_profile=profile)
            result = color.linear_prophoto_to_display(
                raw, develop_profile=profile, output_space='srgb')
            np.testing.assert_array_equal(result, preview)
            # Patches within the delivery gamut also match the preview for
            # every tone curve. Random ProPhoto edge colors can legitimately
            # reach outside the output gamut after the tone rendering.
            patches = raw_patches()
            preview = color.linear_prophoto_to_display_srgb(
                patches, develop_profile=profile)
            wide = color.linear_prophoto_to_display(
                patches, develop_profile=profile, output_space='prophoto')
            np.testing.assert_allclose(color.convert_output_space(
                wide, 'srgb', input_space='prophoto'), preview, atol=4e-4)

    def test_wide_input_is_not_converted_twice_and_has_correct_profile(self):
        pixels = color.linear_prophoto_to_display(
            raw_patches(), develop_profile='linear', output_space='display_p3')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'p3.tif'
            color.save_export_image(pixels, destination, fmt='tif',
                                    input_space='display_p3', output_space='display_p3')
            with tifffile.TiffFile(destination) as image:
                self.assertEqual(image.pages[0].tags[34675].value,
                                 color.required_icc_bytes('display_p3'))
                np.testing.assert_array_equal(image.asarray(),
                    np.round(pixels * 65535).astype(np.uint16))

    def test_unsupported_color_operations_keep_preview_and_warn(self):
        for delta in ({'grade': {'exposure': .2}},
                      {'masks': [{'type': 'linear', 'enabled': True,
                                  'grade': {'exposure': .2}}]},
                      {'optics': {'vignette': .2}},
                      {'heals': [{'enabled': True}]},
                      {'watermark': {'enabled': True}},
                      {'params': film_pipeline.clean_params({'profile_enabled': True})}):
            with self.subTest(delta=delta):
                job = dict(base_job(), **delta)
                self.assertEqual(server.export_input_color_space(job), 'srgb')
                self.assertIn('sRGB-limited', job['warnings'][0])
                server.export_input_color_space(job)
                self.assertEqual(len(job['warnings']), 1)
        self.assertEqual(server.export_input_color_space(base_job()), 'display_p3')
        narrow = base_job('srgb')
        server.export_input_color_space(narrow)
        self.assertNotIn('warnings', narrow)

    def test_cli_refuses_wide_pixels_with_srgb_grade(self):
        job = dict(base_job(), inputColorSpace='display_p3', grade={'exposure': .2})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_path = root / 'job.json'
            job_path.write_text(json.dumps(job))
            with mock.patch.object(sys, 'argv', ['render_cli.py', str(root / 'src.tif'),
                                                str(root / 'output.tif'), str(job_path)]), \
                    mock.patch.object(render_cli.fp, 'load_linear') as load:
                with self.assertRaisesRegex(ValueError, 'sRGB color adjustments'):
                    render_cli.main()
                load.assert_not_called()
                self.assertFalse((root / 'output.tif').exists())

    def test_neutral_cache_and_cli_preserve_color_crop_rotation_and_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'neutral').mkdir()
            original = root / 'source.dng'
            original.write_bytes(b'synthetic decoded RAW is supplied separately')
            decoded = root / 'linear.tif'
            pixels = np.repeat(np.repeat(raw_patches(), 12, axis=0), 8, axis=1)
            tifffile.imwrite(decoded, pixels, photometric='rgb')
            job = base_job()
            job['params']['linear_input'] = True  # capture identity is not TIFF encoding
            job['params']['rotate'] = 90
            job['crop'] = {'x': .1, 'y': .25, 'w': .8, 'h': .5}
            job['longEdge'] = 12
            with ExitStack() as stack:
                for key, value in (('CACHE', root), ('src_path', lambda _: original),
                                   ('file_key', lambda _: 'fixture'), ('is_raw', lambda _: True),
                                   ('tiff_for', lambda *a, **k: decoded)):
                    stack.enter_context(mock.patch.object(server, key, value))
                wide_path = server.export_render_source('source.dng', job)
                narrow_path = server.neutral_tiff_for('source.dng', job['params'])
            self.assertNotEqual(wide_path, narrow_path)
            self.assertEqual(job['inputColorSpace'], 'display_p3')
            self.assertFalse(job['params']['linear_input'])
            wide = color.load_float_rgb(wide_path)
            self.assertGreater(float(np.max(abs(wide - color.convert_output_space(
                color.load_float_rgb(narrow_path), 'display_p3')))), .05)
            destination = root / 'delivery.tif'
            job_path = root / 'job.json'
            job_path.write_text(json.dumps(job))
            with mock.patch.object(sys, 'argv', ['render_cli.py', str(wide_path),
                                                str(destination), str(job_path)]), \
                    redirect_stdout(StringIO()) as output:
                render_cli.main()
            result = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(result['warnings'], [])
            expected = np.rot90(wide, 3)
            h, w = expected.shape[:2]
            x0, y0 = round(.1*w), round(.25*h)
            expected = expected[y0:y0+round(.5*h), x0:x0+round(.8*w)]
            expected = color.resize_float(expected, 12)
            with tifffile.TiffFile(destination) as image:
                np.testing.assert_array_equal(image.asarray(),
                    (expected * 65535 + .5).astype(np.uint16))
                self.assertEqual(image.pages[0].tags[34675].value,
                                 color.required_icc_bytes('display_p3'))

    def test_processed_source_converts_directly_to_selected_gamut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'neutral').mkdir()
            source = root / 'capture.jpg'
            source.write_bytes(b'synthetic input')
            def convert(src, dst, **options):
                self.assertEqual(options['output_space'], 'prophoto')
                tifffile.imwrite(dst, np.zeros((2, 3, 3), dtype=np.uint16))
            with mock.patch.object(server, 'CACHE', root), \
                    mock.patch.object(server, 'src_path', return_value=source), \
                    mock.patch.object(server, 'file_key', return_value='fixture'), \
                    mock.patch.object(server, 'is_raw', return_value=False), \
                    mock.patch.object(server.platform_image, 'convert_processed_to_tiff',
                                      side_effect=convert) as conversion:
                path = server.export_render_source('capture.jpg', base_job('prophoto'))
            conversion.assert_called_once()
            self.assertTrue(path.is_file())

    def test_external_worker_receives_encoding_and_retains_metadata_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'neutral.tif'
            destination = root / 'edit.tif'
            job = base_job()
            def render_source(name, worker_job):
                worker_job['inputColorSpace'] = 'display_p3'
                return source
            def run(command, **options):
                payload = json.loads(Path(command[-1]).read_text())
                self.assertEqual(payload['inputColorSpace'], 'display_p3')
                Path(command[-2]).write_bytes(b'good rendered pixels')
                return mock.Mock(returncode=0, stderr='', stdout=json.dumps({
                    'ok': True, 'warnings': ['Camera metadata unavailable']}))
            with mock.patch.object(server, 'CACHE', root), \
                    mock.patch.object(server, 'RUST_WORKER_BIN', None), \
                    mock.patch.object(server, 'export_render_source', side_effect=render_source), \
                    mock.patch.object(server.subprocess, 'run', side_effect=run):
                server._render_external_job('capture.dng', destination, job)
            self.assertEqual(job['warnings'], ['Camera metadata unavailable'])
            self.assertEqual(destination.read_bytes(), b'good rendered pixels')


    def test_external_worker_marks_raw_film_input_as_linear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'raw-linear.tif'
            source.write_bytes(b'linear master')
            destination = root / 'edit.tif'
            job = base_job()
            job['params']['profile_enabled'] = True
            seen = {}

            def render_source(name, worker_job):
                seen['linear_input'] = worker_job['params']['linear_input']
                return source

            def run(command, **options):
                Path(command[-2]).write_bytes(b'good rendered pixels')
                return mock.Mock(returncode=0, stderr='', stdout=json.dumps({
                    'ok': True, 'warnings': []}))

            with mock.patch.object(server, 'CACHE', root), \
                    mock.patch.object(server, 'RUST_WORKER_BIN', None), \
                    mock.patch.object(server, 'is_raw', return_value=True), \
                    mock.patch.object(server, 'export_render_source',
                                      side_effect=render_source), \
                    mock.patch.object(server.subprocess, 'run', side_effect=run):
                server._render_external_job('capture.dng', destination, job)

            self.assertTrue(seen['linear_input'])


if __name__ == '__main__':
    unittest.main()
