# SPDX-License-Identifier: GPL-3.0-only
"""Exercise native Photos browsing without permission prompts or personal data."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift required')
class ApplePhotosBrowserTests(unittest.TestCase):
    def test_permissions_pages_album_scope_cancellation_and_library_changes(self):
        source = (ROOT / 'app/main.swift').read_text()
        browser = source[source.index('private final class ApplePhotosBrowser:'):].split('\n// MARK:', 1)[0]
        harness = (ROOT / 'tests/fixtures/apple-photos-browser.swift').read_text().replace('BROWSER_SOURCE', browser)
        with tempfile.TemporaryDirectory(prefix='lighttable-photos-browser-') as directory:
            root = Path(directory)
            (root / 'main.swift').write_text(harness)
            build = subprocess.run(['swiftc', '-swift-version', '5', '-module-cache-path',
                str(root / 'cache'), str(root / 'main.swift'), '-o', str(root / 'browser-test')],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(root / 'browser-test')], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('PASS:', run.stdout)
