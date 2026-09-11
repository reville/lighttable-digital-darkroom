# SPDX-License-Identifier: GPL-3.0-only
"""The scorer must catch realistic silent failures, not just average drift."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

from processing_support import compare_images, target_rgb8

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('processing_gate', ROOT / 'scripts/check-processing.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ProcessingGateTests(unittest.TestCase):
    def test_rejects_bad_pixels_shapes_and_missing_work(self):
        reference = target_rgb8().astype(float) / 255
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(compare_images('same', reference, reference, directory)['status'], 'pass')
            for name, bad in [('upside-down', reference[::-1]), ('wrong-channels', reference[..., ::-1]),
                              ('blank', np.zeros_like(reference)), ('nan', reference * np.nan),
                              ('dimensions', reference[:20])]:
                self.assertEqual(compare_images(name, reference, bad, directory)['status'], 'fail', name)
            one_bad_pixel = reference.copy()
            one_bad_pixel[20, 20] = 1 - one_bad_pixel[20, 20]
            self.assertEqual(compare_images('localized', reference, one_bad_pixel, directory)['status'], 'fail')
        for suite in [{}, {'status': 'pass', 'records': []},
                      {'status': 'blocked', 'records': [{'status': 'pass'}]},
                      {'records': [{'status': 'skip'}]}]:
            self.assertFalse(gate.passed(suite))

    def test_blocked_is_a_junit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            gate.write_reports({'status': 'fail', 'provenance': {'revision': 'fixture'},
                                'suites': {'native': {'status': 'blocked', 'records': []}}}, path)
            self.assertIn('failures="1"', (path / 'junit.xml').read_text())


if __name__ == '__main__':
    unittest.main()
