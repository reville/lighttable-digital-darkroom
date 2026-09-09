"""Update-feed contracts: exact signed payload identity and release-only URLs."""
import base64
import importlib.util
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_appcast", ROOT / "scripts/windows/write-appcast.py")
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


class WindowsAppcastTests(unittest.TestCase):
    def test_feed_identifies_the_exact_verified_exe_and_immutable_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "LightTable-1.2.3-windows-x64-setup.exe"
            installer.write_bytes(b"signed executable fixture")
            signature = base64.b64encode(bytes(range(64))).decode()
            root = ET.fromstring(feed.appcast("1.2.3", installer, signature))
            enclosure = root.find("channel/item/enclosure")
            self.assertEqual(enclosure.get("length"), str(installer.stat().st_size))
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}edSignature"), signature)
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}os"), "windows")
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}version"), "1.2.3")
            self.assertEqual(enclosure.get("url"), f"{feed.REPOSITORY}/releases/download/v1.2.3/{installer.name}")

    def test_feed_rejects_mismatched_empty_and_unsigned_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "LightTable-1.2.3-windows-x64-setup.exe"
            installer.write_bytes(b"fixture")
            signature = base64.b64encode(bytes(range(64))).decode()
            for version, sig in [("1.2.4", signature), ("1.2.3-beta", signature), ("1.2.3", "invalid"), ("1.2.3", base64.b64encode(b"short").decode())]:
                with self.subTest(version=version, signature=sig), self.assertRaises(ValueError):
                    feed.appcast(version, installer, sig)
            installer.write_bytes(b"")
            with self.assertRaises(ValueError):
                feed.appcast("1.2.3", installer, signature)

    def test_windows_and_sparkle_publishers_pin_the_same_public_key(self):
        key = plistlib.loads((ROOT / "app/Info.plist").read_bytes())["SUPublicEDKey"]
        native = (ROOT / "windows-shell/src/windows_update.rs").read_text()
        build = (ROOT / "scripts/windows/build-release.ps1").read_text()
        self.assertEqual(re.search(r'PUBLIC_KEY: &str = "([^"]+)"', native)[1], key)
        self.assertEqual(re.search(r'\$PublicKey = "([^"]+)"', build)[1], key)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WinSparkleStagingTests(unittest.TestCase):
    def test_stages_the_x64_release_dll_and_notices_without_development_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk, payload = root / "SDK with spaces", root / "payload"
            for name, data in {"x64/Release/WinSparkle.dll": b"x64 updater", "Win32/Release/WinSparkle.dll": b"wrong arch",
                               "bin/winsparkle-tool.exe": b"signing tool", "COPYING": b"license",
                               "COPYING.expat": b"dependency license"}.items():
                path = sdk / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            result = self.stage(sdk, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((payload / "WinSparkle.dll").read_bytes(), b"x64 updater")
            self.assertEqual({p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()},
                             {"WinSparkle.dll", "Resources/LightTable/licenses/winsparkle/COPYING",
                              "Resources/LightTable/licenses/winsparkle/COPYING.expat"})
            # An incomplete SDK fails before touching an existing payload.
            (sdk / "bin/winsparkle-tool.exe").unlink()
            (sdk / "x64/Release/WinSparkle.dll").write_bytes(b"replacement")
            result = self.stage(sdk, payload)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((payload / "WinSparkle.dll").read_bytes(), b"x64 updater")

    @staticmethod
    def stage(sdk, payload):
        return subprocess.run([shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-File",
                               str(ROOT / "scripts/windows/stage-winsparkle.ps1"), "-Sdk", str(sdk),
                               "-Payload", str(payload)], capture_output=True, text=True, timeout=30)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WindowsUpdaterSmokeDiagnosticTests(unittest.TestCase):
    def run_diagnostic(self, body):
        # Import just the real function ASTs: no fixture compiler, native helper,
        # install, or user profile is invoked by these focused helper tests.
        bootstrap = r'''
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Script = Join-Path $env:LIGHTTABLE_UPDATER_TEST_REPO "scripts/windows/updater-smoke.ps1"
$Tokens = $null; $Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
foreach ($Name in @("Get-UpdaterLogText", "Get-UpdaterProcessDiagnostic", "Assert-UpdaterRejection")) {
    $Function = $Ast.Find({ param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name }, $true)
    if (-not $Function) { throw "Missing diagnostic function $Name" }
    Invoke-Expression $Function.Extent.Text
}
$Helper = Join-Path $env:LIGHTTABLE_UPDATER_TEST_ROOT "LightTable-update.exe"
$ErrorFile = [IO.Path]::ChangeExtension($Helper, "error.txt")
function Start-TestChild([string]$Command) {
    $Info = New-Object Diagnostics.ProcessStartInfo
    $Info.FileName = (Get-Process -Id $PID).Path
    $Info.Arguments = '-NoProfile -Command "' + $Command + '"'
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    return [Diagnostics.Process]::Start($Info)
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ, LIGHTTABLE_UPDATER_TEST_REPO=str(ROOT),
                               LIGHTTABLE_UPDATER_TEST_ROOT=temporary)
            result = subprocess.run([shutil.which("pwsh") or shutil.which("powershell"),
                                     "-NoProfile", "-Command", bootstrap + body], env=environment,
                                    capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_diagnostic_retains_exit_code_and_native_failure_and_rejects_wrong_reason(self):
        self.run_diagnostic(r'''
$Child = Start-TestChild "exit 7"
try {
    if (-not $Child.WaitForExit(10000)) { throw "Test child did not exit" }
    [IO.File]::WriteAllText($ErrorFile, "This installation does not allow direct updates")
    $Message = Get-UpdaterProcessDiagnostic $Child $Helper
    if (-not $Message.Contains("exit 0x00000007 (7)") -or -not $Message.Contains("does not allow direct updates")) { throw $Message }
    Assert-UpdaterRejection $Child $Helper "This installation does not allow direct updates"
    try {
        Assert-UpdaterRejection $Child $Helper "The update was cancelled"
        throw "Wrong rejection reason was accepted"
    } catch {
        if (-not $_.Exception.Message.Contains("Expected native rejection")) { throw }
    }
    Remove-Item -LiteralPath $ErrorFile
    try {
        Assert-UpdaterRejection $Child $Helper "The update was cancelled"
        throw "An unrelated early exit passed the cancellation test"
    } catch {
        if (-not $_.Exception.Message.Contains("Expected native rejection")) { throw }
    }
} finally {
    if (-not $Child.HasExited) { $Child.Kill(); [void]$Child.WaitForExit(5000) }
    $Child.Dispose()
}
''')

    def test_running_helper_is_distinguished_from_an_early_exit_and_logs_are_bounded(self):
        self.run_diagnostic(r'''
$Child = Start-TestChild "Start-Sleep -Seconds 30"
try {
    [IO.File]::WriteAllText($ErrorFile, (("x" * 6000) + "diagnostic-tail"))
    $Tail = Get-UpdaterLogText $ErrorFile
    if ($Tail.Length -gt 4096 -or -not $Tail.EndsWith("diagnostic-tail")) { throw "Diagnostic tail was not bounded" }
    $Message = Get-UpdaterProcessDiagnostic $Child $Helper
    if (-not $Message.Contains("still running")) { throw $Message }
} finally {
    if (-not $Child.HasExited) { $Child.Kill(); [void]$Child.WaitForExit(5000) }
    $Child.Dispose()
}
''')


if __name__ == "__main__":
    unittest.main()
