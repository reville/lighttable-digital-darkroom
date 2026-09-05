"""Run the browser save queue's behavioral regressions in the Python CI suite."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class EditSaveQueueTests(unittest.TestCase):
    def test_browser_save_queue(self):
        test_file = Path(__file__).with_name('edit-save-queue.test.mjs')
        result = subprocess.run(
            ['node', '--test', str(test_file)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
