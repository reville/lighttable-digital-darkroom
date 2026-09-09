"""Store preparation checks; native certificate and runtime proof still needs Windows."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def pe_fixture():
    data = bytearray(128)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 64)
    data[64:68] = b'PE\0\0'
    return data


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class StorePackagingTests(unittest.TestCase):
    def test_pe_audit_finds_python_modules_and_unusual_suffixes_and_reports_failures(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload = root / 'payload with spaces'
                payload.mkdir()
                for name in ('LightTable.exe', 'native.pyd', 'vendor.dll', 'renamed.bin', 'Uninstall.exe'):
                    (payload / name).write_bytes(pe_fixture())
                (payload / 'readme.txt').write_text('not executable')
                report = root / 'report.json'
                # Substitute only certificate verification. Scan actual PE headers,
                # walk the real filesystem and produce actual file hashes/report.
                failure = "if ($LiteralPath.EndsWith('native.pyd')) { $status = 'NotSigned' };" if invalid else ''
                script = (
                    "function Get-AuthenticodeSignature { param($LiteralPath); $status = 'Valid'; "
                    + failure + "[pscustomobject]@{ Status = $status; SignerCertificate = @{ Subject = 'fixture vendor' } } }; "
                    + f"& {quote(ROOT / 'scripts/windows/store-pe-signatures.ps1')} "
                    + f"-Payload {quote(payload)} -Report {quote(report)}"
                )
                result = subprocess.run([POWERSHELL, '-NoProfile', '-Command', script], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode != 0, invalid, result.stderr)
                evidence = json.loads(report.read_text(encoding='utf-8-sig'))
                self.assertEqual(evidence['pe_count'], 5)
                self.assertEqual(evidence['invalid_count'], int(invalid))
                self.assertEqual({row['path'] for row in evidence['signatures']},
                                 {'LightTable.exe', 'native.pyd', 'vendor.dll', 'renamed.bin', 'Uninstall.exe'})
                if invalid:
                    self.assertIn('native.pyd (NotSigned)', result.stderr)

    def test_nsis_extensionless_uninstaller_is_signed_and_copied_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'sign-uninstaller.ps1'
            shutil.copyfile(ROOT / 'scripts/windows/sign-uninstaller.ps1', script)
            (root / 'sign-release.ps1').write_text(
                'param([string[]]$Files,[switch]$RequireSigning)\n'
                'if (-not $RequireSigning -or [IO.Path]::GetExtension($Files[0]) -ne ".exe") { throw "Incorrect signing input" }\n'
                '[IO.File]::AppendAllText($Files[0], "signed-fixture")\n')
            uninstaller = root / 'extensionless temp file'
            uninstaller.write_text('native-fixture-')
            result = subprocess.run([POWERSHELL, '-NoProfile', '-File', str(script),
                                     '-RequireSigning', '-Files', str(uninstaller)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(uninstaller.read_text(), 'native-fixture-signed-fixture')

    def test_candidate_rejects_missing_offline_prerequisite_before_signing_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'not-created'
            result = subprocess.run([POWERSHELL, '-NoProfile', '-File',
                                     str(ROOT / 'scripts/windows/build-release.ps1'),
                                     '-StoreCandidate', '-OutputDirectory', str(output)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Standalone Installer', result.stderr)
            self.assertFalse(output.exists())

    def test_offline_hash_mismatch_stops_before_certificate_or_build_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / 'standalone.exe'
            fixture.write_bytes(b'fixture')
            result = subprocess.run([POWERSHELL, '-NoProfile', '-File',
                                     str(ROOT / 'scripts/windows/build-release.ps1'), '-StoreCandidate',
                                     '-OfflineWebView2Installer', str(fixture),
                                     '-OfflineWebView2Sha256', '0' * 64], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SHA256 mismatch', result.stderr)


if __name__ == '__main__':
    unittest.main()
