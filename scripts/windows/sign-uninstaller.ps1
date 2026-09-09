param([Parameter(Mandatory)][string[]]$Files, [switch]$RequireSigning)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($Files.Count -ne 1 -or -not $RequireSigning) { throw 'Exactly one uninstaller and required signing are expected.' }
# NSIS supplies an extensionless temporary file. SignTool needs a PE filename,
# and our release signer deliberately rejects arbitrary file types.
$Directory = Join-Path ([IO.Path]::GetTempPath()) ('lighttable-uninstaller-sign-' + [Guid]::NewGuid())
try {
    New-Item -ItemType Directory -Path $Directory | Out-Null
    $Executable = Join-Path $Directory 'Uninstall.exe'
    Copy-Item -LiteralPath $Files[0] -Destination $Executable
    & (Join-Path $PSScriptRoot 'sign-release.ps1') -RequireSigning -Files $Executable
    Copy-Item -LiteralPath $Executable -Destination $Files[0] -Force
} finally {
    if (Test-Path -LiteralPath $Directory) { Remove-Item -LiteralPath $Directory -Recurse -Force }
}
