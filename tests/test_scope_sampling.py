# SPDX-License-Identifier: GPL-3.0-only
"""Run the scope and sampling-helper regressions in the Python CI suite."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class ScopeSamplingTests(unittest.TestCase):
    def test_scopes_and_sampling_helper(self):
        test_file = Path(__file__).with_name('scope-sampling.test.mjs')
        result = subprocess.run(
            ['node', '--test', str(test_file)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
