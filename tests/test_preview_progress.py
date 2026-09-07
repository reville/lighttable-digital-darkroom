"""Run the browser progress and RAW refinement orchestration regressions."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class PreviewProgressTests(unittest.TestCase):
    def test_preview_progress(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_name('preview-progress.test.mjs'))],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
