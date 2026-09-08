"""Run MIDI permission and controller behavior regressions in the Python suite."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class MidiAccessTests(unittest.TestCase):
    def test_midi_access(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_name('midi-access.test.mjs'))],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
