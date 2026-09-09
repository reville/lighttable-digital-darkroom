"""Store preparation checks; native certificate and runtime proof still needs Windows."""
import json
import hashlib
import importlib.util
import zipfile
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
                (payload / 'nested').mkdir()
                (payload / 'nested' / 'module.pyd').write_bytes(pe_fixture())
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
                self.assertEqual(evidence['pe_count'], 6)
                self.assertEqual(evidence['invalid_count'], int(invalid))
                self.assertEqual({row['path'].replace('\\', '/') for row in evidence['signatures']},
                                 {'LightTable.exe', 'native.pyd', 'vendor.dll', 'renamed.bin', 'Uninstall.exe',
                                  'nested/module.pyd'})
                if invalid:
                    self.assertIn('native.pyd (NotSigned)', result.stderr)

    def test_signing_output_does_not_enter_evidence_and_failures_still_block(self):
        for outcome in ('valid', 'invalid-signature', 'signer-error'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload = root / 'payload'
                payload.mkdir()
                (payload / 'native.pyd').write_bytes(pe_fixture())
                (payload / 'vendor.dll').write_bytes(pe_fixture())
                audit = root / 'store-pe-signatures.ps1'
                shutil.copyfile(ROOT / 'scripts/windows/store-pe-signatures.ps1', audit)
                signer = (
                    'param([string[]]$Files,[switch]$RequireSigning)\n'
                    'if (-not $RequireSigning -or $Files.Count -ne 1 -or '
                    '[IO.Path]::GetFileName($Files[0]) -ne "native.pyd") { throw "Unexpected signing input" }\n'
                    'Write-Output "native signer diagnostic"\n'
                    '[pscustomobject]@{ SignerDiagnostic = "extra output" }\n'
                )
                if outcome == 'signer-error':
                    signer += 'throw "fixture signing failed"\n'
                else:
                    signer += '[IO.File]::WriteAllText(($Files[0] + ".signed"), "done")\n'
                (root / 'sign-release.ps1').write_text(signer)
                report = root / 'report.json'
                final_status = 'Valid' if outcome == 'valid' else 'NotSigned'
                command = (
                    'function Get-AuthenticodeSignature { param($LiteralPath); '
                    '$status = "Valid"; '
                    'if ($LiteralPath.EndsWith("native.pyd")) { '
                    '$status = "NotSigned"; '
                    f'if (Test-Path -LiteralPath ($LiteralPath + ".signed")) {{ $status = "{final_status}" }} }}; '
                    '[pscustomobject]@{ Status = $status; SignerCertificate = @{ Subject = "fixture vendor" } } }; '
                    f'& {quote(audit)} -Payload {quote(payload)} -SignMissing -Report {quote(report)}'
                )
                result = subprocess.run([POWERSHELL, '-NoProfile', '-Command', command],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode == 0, outcome == 'valid', result.stderr)
                if outcome == 'signer-error':
                    self.assertIn('fixture signing failed', result.stderr)
                    self.assertFalse(report.exists())
                    continue
                evidence = json.loads(report.read_text(encoding='utf-8-sig'))
                self.assertEqual(evidence['pe_count'], 2)
                self.assertEqual(evidence['invalid_count'], int(outcome != 'valid'))
                self.assertEqual({row['path'] for row in evidence['signatures']}, {'native.pyd', 'vendor.dll'})
                if outcome == 'invalid-signature':
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

    def test_standalone_staging_checks_hash_and_microsoft_trust_before_promoting_input(self):
        for case in ('valid', 'hash-mismatch', 'untrusted'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = root / 'input.json'
                manifest.write_text(json.dumps({
                    'filename': 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe',
                    'source_url': 'https://msedge.sf.dl.delivery.mp.microsoft.com/fixture.exe',
                    'bytes': 7, 'sha256': '0' * 64 if case == 'hash-mismatch' else hashlib.sha256(b'fixture').hexdigest()}))
                output = root / 'staged'
                status = 'NotSigned' if case == 'untrusted' else 'Valid'
                command = (
                    'function Invoke-WebRequest { param($Uri,$OutFile,$TimeoutSec,$MaximumRedirection); '
                    'if ($MaximumRedirection -ne 0) { throw "Unexpected redirects" }; [IO.File]::WriteAllText($OutFile, "fixture") }; '
                    'function Get-AuthenticodeSignature { param($LiteralPath); '
                    f'[pscustomobject]@{{ Status = "{status}"; SignerCertificate = @{{ Subject = "CN=Microsoft Corporation" }} }} }}; '
                    f'& {quote(ROOT / "scripts/windows/stage-store-webview2.ps1")} -Manifest {quote(manifest)} -OutputDirectory {quote(output)}')
                result = subprocess.run([POWERSHELL, '-NoProfile', '-Command', command],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode == 0, case == 'valid', result.stderr)
                self.assertEqual((output / 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe').exists(), case == 'valid')
                self.assertEqual(list(output.glob('download-*')), [])

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


class StoreReceiptTests(unittest.TestCase):
    def test_receipt_matches_final_installer_source_and_complete_pe_audit(self):
        spec = importlib.util.spec_from_file_location('store_receipt', ROOT / 'scripts/windows/write-store-receipt.py')
        receipt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(receipt)
        for case in ('valid', 'tampered-installer', 'missing-uninstaller', 'wrong-source'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                installer = output / 'LightTable-0.5.0-windows-x64-setup.exe'
                installer.write_bytes(b'signed fixture')
                archive = output / 'LightTable-0.5.0-windows-x64.zip'
                with zipfile.ZipFile(archive, 'w') as bundle:
                    bundle.writestr('LightTable/build-manifest.json', json.dumps({
                        'source_revision': 'a' * 40, 'version': '0.5.0', 'store_candidate': True,
                        'authenticode_signed': True, 'webview2_offline_sha256': 'b' * 64}))
                input_manifest = output / 'input.json'
                input_manifest.write_text(json.dumps({'sha256': 'b' * 64}))
                (output / 'windows-signatures.json').write_text(json.dumps({
                    'source_revision': 'a' * 40, 'archive_sha256': receipt.sha256(archive),
                    'signatures': [{'file': installer.name, 'sha256': receipt.sha256(installer), 'status': 'Valid'}]}))
                (output / 'windows-store-pe-signatures.json').write_text(json.dumps({
                    'pe_count': 1, 'invalid_count': 0, 'signatures': [{
                        'path': 'native.pyd' if case == 'missing-uninstaller' else 'Uninstall.exe', 'status': 'Valid'}]}))
                if case == 'tampered-installer':
                    installer.write_bytes(b'changed after signing')
                revision = 'c' * 40 if case == 'wrong-source' else 'a' * 40
                if case != 'valid':
                    with self.assertRaises(ValueError):
                        receipt.write_receipt(output, input_manifest, revision)
                    self.assertFalse((output / 'store-candidate-receipt.json').exists())
                else:
                    path = receipt.write_receipt(output, input_manifest, revision)
                    data = json.loads(path.read_text())
                    self.assertEqual(data['source_revision'], revision)
                    self.assertEqual(len(data['artifacts']), 4)
                    self.assertIn('NOT DONE', data['offline_clean_machine_acceptance'])


if __name__ == '__main__':
    unittest.main()
