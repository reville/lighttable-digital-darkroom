"""Exercise engine defaults and accurate-only requests through real dispatch."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import server


class PreviewEngineRoutingTests(unittest.TestCase):
    def render(self, engine=None, *, available=True, raw=False, allow_draft=True):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            name = 'photo.dng' if raw else 'photo.jpg'
            Image.new('RGB', (12, 8), 'blue').save(folder/'photo.jpg')
            steps = []

            def rust(name, params, width, output, native_output, viewport,
                     variant=None):
                steps.append('rust')
                output.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (12, 8), 'green').save(output)
                return dict(mean=.5, backend='test GPU', render_ms=1, total_ms=2)

            def python(image, params):
                steps.append('python')
                return np.full((8, 12, 3), 128, dtype=np.uint8)

            def accurate(*args):
                steps.append('accurate-input')

            with (
                mock.patch.object(server, 'FOLDER', folder),
                mock.patch.object(server, 'CACHE', folder/'cache'),
                mock.patch.object(server, 'RUST_AVAILABLE', available),
                mock.patch.object(server, 'file_key', return_value='test-input'),
                mock.patch.object(server, 'preview_variant', return_value='full'),
                mock.patch.object(server, 'orig_mean_display', return_value=.5),
                mock.patch.object(server, 'render_rust', side_effect=rust),
                mock.patch.object(server.fp, 'render', side_effect=python),
                mock.patch.object(server, 'linear_for', return_value=np.zeros((8,12,3), dtype=np.float32)),
                mock.patch.object(server, 'build_raw_preview', side_effect=accurate),
            ):
                options = dict(allow_draft=allow_draft)
                if engine is not None:
                    options['engine'] = engine
                result = server._render_preview(name, {'profile_enabled': True}, 1100, **options)
            return result, steps

    def test_default_uses_rust_and_unavailable_rust_falls_back(self):
        result, steps = self.render()
        self.assertEqual(result['engine'], 'rs')
        self.assertEqual(steps, ['rust'])
        result, steps = self.render(available=False)
        self.assertEqual(result['engine'], 'py')
        self.assertEqual(steps, ['python'])

    def test_explicit_python_is_preserved(self):
        result, steps = self.render('py')
        self.assertEqual(result['engine'], 'py')
        self.assertEqual(steps, ['python'])

    def test_accurate_only_request_prepares_raw_before_any_film_work(self):
        result, steps = self.render(raw=True, allow_draft=False)
        self.assertEqual(steps, ['accurate-input', 'rust'])
        self.assertFalse(result['refining'])
        _, steps = self.render(raw=True)
        self.assertEqual(steps, ['rust'], 'a cold first frame must not wait for demosaic')

    def test_http_default_and_draft_policy_reach_renderer(self):
        for supplied, expected in (({}, 'rs'), ({'engine': 'py'}, 'py')):
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/api/render'
            handler.headers = {}
            handler._enforce_security = mock.Mock()
            handler._json = mock.Mock()
            handler._log_request = mock.Mock()
            handler._body = lambda: dict(name='photo.jpg', allow_draft=False, **supplied)
            with mock.patch.object(server, 'render_preview', return_value={}) as render, \
                    mock.patch.object(server, 'apply_preview_edits', return_value={}):
                handler.do_POST()
            self.assertEqual(render.call_args.args[3], expected)
            self.assertFalse(render.call_args.kwargs['allow_draft'])
