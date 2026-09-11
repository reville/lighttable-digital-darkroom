# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/windows/client-vm/prepare.py'

class ClientPreparationTests(unittest.TestCase):
    def prepare(self, digest=None, source='a'*40, version='0.6.0'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        download = root / '.build/windows-client-download'
        download.mkdir(parents=True)
        (download / 'LightTable-0.6.0-windows-x64-setup.exe').write_bytes(b'verified candidate')
        scripts = root / 'scripts/windows/client-vm'
        scripts.mkdir(parents=True)
        for name in ('guest-test.ps1', 'bootstrap.ps1'):
            (scripts/name).write_text('# fixture')
        digest = digest or hashlib.sha256(b'verified candidate').hexdigest()
        result = subprocess.run([sys.executable, str(SCRIPT), '11', '--version', version,
            '--source-sha', source, '--installer-sha256', digest], cwd=root,
            capture_output=True, text=True)
        return root, result

    def test_exact_candidate_and_matching_receiver_are_staged(self):
        root, result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((root/'.build/client-vm/oem/candidate.json').read_text())
        self.assertEqual(config['version'], '0.6.0')
        self.assertEqual(config['source_sha'], 'a'*40)
        self.assertEqual(config['evidence_url'], 'http://10.0.2.1:18080')
        tree = ET.parse(root/'.build/client-vm/custom.xml')
        ns = {'u':'urn:schemas-microsoft-com:unattend'}
        command = tree.find('.//u:CommandLine', ns).text
        self.assertIn('C:\\OEM\\bootstrap.ps1', command)
        password = tree.find('.//u:Password/u:Value', ns).text
        self.assertNotIn(password, result.stdout+result.stderr)

    def test_wrong_hash_is_rejected_before_staging(self):
        root, result = self.prepare(digest='0'*64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root/'.build/client-vm/custom.xml').exists())

    def test_invalid_source_or_version_is_rejected(self):
        for kwargs in ({'source':'main'}, {'version':'../0.6.0'}):
            with self.subTest(kwargs=kwargs):
                root, result = self.prepare(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root/'.build/client-vm/custom.xml').exists())

if __name__ == '__main__': unittest.main()
