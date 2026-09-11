# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the actual editor save and navigation functions at their boundaries."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class EditSaveIntegrationTests(unittest.TestCase):
    def test_browser_save_integration(self):
        test_file = Path(__file__).with_name('edit-save-integration.test.mjs')
        result = subprocess.run(
            ['node', '--test', str(test_file)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
