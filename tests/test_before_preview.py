from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class BeforePreviewTests(unittest.TestCase):
    def test_before_texture_selection(self):
        result = subprocess.run(["node", "--test", str(ROOT / "tests/before-preview.test.mjs")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
