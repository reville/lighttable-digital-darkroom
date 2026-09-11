# SPDX-License-Identifier: GPL-3.0-only
"""Run the selected Photos transfer with disposable, ephemeral provider files."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift required')
class SelectedPhotosImportTests(unittest.TestCase):
    def test_ephemeral_copies_formats_collisions_failures_and_cancellation(self):
        source = (ROOT / 'app/main.swift').read_text()
        importer = source[source.index('private final class SelectedPhotosImporter'):].split('\n// MARK:', 1)[0]
        harness = (ROOT / 'tests/fixtures/selected-photos-import.swift').read_text().replace('IMPORTER_SOURCE', importer)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'main.swift').write_text(harness)
            build = subprocess.run(['swiftc', '-swift-version', '5', '-module-cache-path',
                str(path / 'cache'), str(path / 'main.swift'), '-o', str(path / 'test')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(path / 'test')], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('PASS', run.stdout)
