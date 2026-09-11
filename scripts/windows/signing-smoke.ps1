# SPDX-License-Identifier: GPL-3.0-only
# Exercise the real signing identity before a long release build. The fixture
# is never executed or shipped, and all temporary files are removed afterward.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Directory = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-signing-smoke-" + [Guid]::NewGuid())
try {
    New-Item -ItemType Directory -Path $Directory | Out-Null
    $Compiler = Join-Path $env:WINDIR "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) { throw "The signing smoke test requires the Windows .NET Framework compiler." }
    $Source = Join-Path $Directory "signing-smoke.cs"
    $Executable = Join-Path $Directory "signing-smoke.exe"
    'class SigningSmoke { static int Main() { return 0; } }' | Set-Content -LiteralPath $Source -Encoding ascii
    & $Compiler /nologo /target:exe "/out:$Executable" $Source
    if ($LASTEXITCODE -ne 0) { throw "The signing smoke fixture failed to compile." }
    & (Join-Path $PSScriptRoot "sign-release.ps1") -RequireSigning -Files $Executable
    Write-Host "Production signing and timestamp verification passed before compilation."
} finally {
    if (Test-Path -LiteralPath $Directory) { Remove-Item -LiteralPath $Directory -Recurse -Force }
}
