# SPDX-License-Identifier: GPL-3.0-only
"""Run behavioral JavaScript regressions through the standard test entrypoint."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
class UIAuditRegressions(unittest.TestCase):
    def test_ui_behaviors(self):
        result = subprocess.run(
            ['node', '--test', str(ROOT / 'tests/ui-audit-regressions.test.mjs')],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
