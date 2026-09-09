param([string]$Root = 'C:\OEM')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Evidence = Join-Path $Root 'evidence'
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Report = @{ok = $false; offline = $true; account_is_elevated = $false}
try {
    $Principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    $Report.account_is_elevated = $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($Report.account_is_elevated) { throw 'Installer acceptance must run unelevated' }
    if (@(Get-NetAdapter | Where-Object Status -eq 'Up').Count) { throw 'A network adapter is still enabled' }
    $Config = Get-Content (Join-Path $Root 'candidate.json') -Raw | ConvertFrom-Json
    $Installer = Join-Path $Root $Config.installer
    if ((Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Config.installer_sha256) {
        throw 'Installer hash mismatch'
    }
    $Report.installer_sha256 = $Config.installer_sha256
    & (Join-Path $Root 'scripts/installer-smoke.ps1') -Installer $Installer -Version $Config.version -ExpectedPublisher 'Chonkers LLC' -VerifyStoreSignatures -SignatureReport (Join-Path $Evidence 'installed-signatures.json') -NativeAcceptanceReport (Join-Path $Evidence 'native')
    if ($LASTEXITCODE -ne 0) { throw 'Installer or installed desktop acceptance failed' }
    $Report.ok = $true
} catch {
    $Report.error = $_.Exception.Message
} finally {
    $Report | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $Evidence 'result.json') -Encoding utf8
    Set-Content (Join-Path $Root 'test.done') 'done'
}
