# SPDX-License-Identifier: GPL-3.0-only
"""Run the control audit behavior regressions in the regular CI suite."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node required')
class ControlAuditTests(unittest.TestCase):
    def test_controls(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', '--test', 'tests/enhance-controls.test.mjs',
             'tests/maps-survey-controls.test.mjs',
             'tests/photo-action-controls.test.mjs',
             'tests/keyword-controls.test.mjs',
             'tests/catalog-controls.test.mjs'], cwd=root,
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
