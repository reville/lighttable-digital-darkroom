"""Run conservative culling grouping checks in ordinary Python CI."""
from pathlib import Path
import shutil
import subprocess
import unittest


class CullSimilarityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_similar_photo_groups_and_recommendations(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("cull-similarity.test.mjs"))],
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
