"""Run compare viewport geometry checks with the standard Python suite."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
class CompareViewTests(unittest.TestCase):
    def test_viewport_geometry(self):
        result = subprocess.run(
            ['node', '--test', str(ROOT / 'tests/compare-view.test.mjs')],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
