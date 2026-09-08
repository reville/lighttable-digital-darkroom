"""Run the bounded model-based save-queue campaign in ordinary CI."""
from pathlib import Path
import shutil
import subprocess
import unittest


class SaveSequenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_seeded_save_sequences(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('save-sequences.test.mjs'))],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
