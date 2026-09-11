# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class WorkflowIntegrityUITests(unittest.TestCase):
    def test_behavioral_regressions(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("workflow-integrity.test.mjs"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
