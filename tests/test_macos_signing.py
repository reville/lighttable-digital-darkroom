# SPDX-License-Identifier: GPL-3.0-only
"""Exercise nested signing with the packaged command wrapper present."""
import platform
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('cc'),
                     'macOS compiler and signing tools required')
class MacOSSigningTests(unittest.TestCase):
    def test_main_app_is_sealed_after_the_command_wrapper(self):
        with tempfile.TemporaryDirectory(prefix='lighttable-signing-test-') as raw:
            app = Path(raw) / 'LightTable.app'
            contents = app / 'Contents'
            executable = contents / 'MacOS' / 'LightTable'
            executable.parent.mkdir(parents=True)
            (contents / 'Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'org.lighttable.signing-test',
                'CFBundleExecutable': 'LightTable',
                'CFBundlePackageType': 'APPL',
                'CFBundleVersion': '1',
            }))
            subprocess.run(['cc', '-x', 'c', '-', '-o', str(executable)],
                           input='int main(void) { return 0; }\n', text=True,
                           capture_output=True, check=True, timeout=60)
            wrapper = executable.with_name('lighttable-cli')
            wrapper.write_text('#!/bin/sh\nexit 0\n')
            wrapper.chmod(0o755)
            signed = subprocess.run([str(ROOT / 'scripts/sign-app.sh'), str(app)],
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(signed.returncode, 0, signed.stdout + signed.stderr)
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)],
                           capture_output=True, check=True, timeout=30)
            # Both executables remain usable after sealing the enclosing app.
            for path in (executable, wrapper):
                subprocess.run([str(path)], check=True, timeout=10)
