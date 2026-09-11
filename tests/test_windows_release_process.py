# SPDX-License-Identifier: GPL-3.0-only
"""Executable checks for release timings and exact candidate identity, without Windows UI."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, 'PowerShell is required')
class WindowsReleaseProcessTests(unittest.TestCase):
    def run_ps(self, script):
        return subprocess.run([POWERSHELL, '-NoProfile', '-Command', script],
                              capture_output=True, text=True, timeout=30)

    def test_timing_records_success_and_failure_without_logging_environment(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report, summary = root / 'timing.json', root / 'summary.md'
                result = self.run_ps(
                    f". {quote(ROOT / 'scripts/windows/build-timing.ps1')}; "
                    f"$env:GITHUB_STEP_SUMMARY = {quote(summary)}; "
                    "$env:SPARKLE_PRIVATE_KEY = 'never-record-this-fixture'; "
                    f"$timing = New-BuildTiming -Path {quote(report)} -Version '0.6.0'; "
                    "$ok = $false; try { Start-BuildStage $timing 'dependency-acquisition'; "
                    "Start-Sleep -Milliseconds 20; Start-BuildStage $timing 'payload-signing'; "
                    + ("$ok = $true; " if succeeds else "throw 'original build failure'; ")
                    + "} finally { Complete-BuildTiming $timing $ok }")
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                data = json.loads(report.read_text(encoding='utf-8-sig'))
                self.assertEqual(data['status'], 'success' if succeeds else 'failed')
                self.assertEqual(len(data['stages']), 2)
                self.assertEqual(data['stages'][0]['status'], 'success')
                self.assertGreaterEqual(data['stages'][0]['elapsed_seconds'], .02)
                self.assertEqual(data['stages'][1]['status'], data['status'])
                self.assertGreaterEqual(data['elapsed_seconds'], data['stages'][0]['elapsed_seconds'])
                self.assertNotIn('never-record-this-fixture', report.read_text() + summary.read_text() + result.stdout)
                if not succeeds:
                    self.assertIn('original build failure', result.stderr)

    def test_timing_write_failure_does_not_mask_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / 'file'
            blocker.write_text('not a directory')
            result = self.run_ps(
                "$ErrorActionPreference = 'Stop'; "
                f". {quote(ROOT / 'scripts/windows/build-timing.ps1')}; "
                f"$timing = New-BuildTiming -Path {quote(blocker / 'timing.json')} -Version '0.6.0'; "
                "try { Start-BuildStage $timing 'compile'; throw 'original build failure' } "
                "finally { Complete-BuildTiming $timing $false }")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('original build failure', result.stderr)
            self.assertIn('Could not persist', result.stdout)

    def test_selected_candidate_requires_matching_source_manifest_and_artifact_hashes(self):
        cases = ('valid', 'receipt-source', 'manifest-source', 'wrong-installer', 'corrupt-archive',
                 'wrong-size', 'unsigned', 'duplicate-artifact', 'wrong-architecture', 'version-mismatch')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                installer = root / 'LightTable-0.6.0-windows-x64-setup.exe'
                installer.write_bytes(b'installer fixture')
                archive = root / 'LightTable-0.6.0-windows-x64.zip'
                manifest = dict(source_revision='b' * 40 if case == 'manifest-source' else 'a' * 40,
                                version='0.5.0' if case == 'version-mismatch' else '0.6.0',
                                architecture='arm64' if case == 'wrong-architecture' else 'x64',
                                authenticode_signed=case != 'unsigned', store_candidate=True)
                with zipfile.ZipFile(archive, 'w') as bundle:
                    bundle.writestr('LightTable/build-manifest.json', json.dumps(manifest))
                artifacts = [dict(file=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                                  bytes=p.stat().st_size) for p in (installer, archive)]
                if case == 'wrong-size':
                    artifacts[0]['bytes'] += 1
                if case == 'duplicate-artifact':
                    artifacts.append(artifacts[0])
                receipt = dict(source_revision='b' * 40 if case == 'receipt-source' else 'a' * 40,
                               version='0.6.0', artifacts=artifacts)
                (root / 'store-candidate-receipt.json').write_text(json.dumps(receipt))
                if case == 'corrupt-archive':
                    archive.write_bytes(b'tampered')
                digest = '0' * 64 if case == 'wrong-installer' else artifacts[0]['sha256']
                result = self.run_ps(
                    f"& {quote(ROOT / 'scripts/windows/verify-native-candidate.ps1')} "
                    f"-Directory {quote(root)} -SourceRevision {'a' * 40} -InstallerSha256 {digest}")
                self.assertEqual(result.returncode == 0, case == 'valid', result.stderr)
                if case == 'valid':
                    self.assertEqual(result.stdout.strip(), '0.6.0')


if __name__ == '__main__':
    unittest.main()
