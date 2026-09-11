# SPDX-License-Identifier: GPL-3.0-only
"""Flag batch behavior is part of the ordinary CI gate."""
from pathlib import Path
import shutil
import subprocess
import unittest


class CullBatchTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_batch(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('cull-batch.test.mjs'))],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
