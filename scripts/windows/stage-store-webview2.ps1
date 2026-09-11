# SPDX-License-Identifier: GPL-3.0-only
param(
    [string]$Manifest = (Join-Path $PSScriptRoot '..\..\packaging\webview2-store-input.json'),
    [Parameter(Mandatory)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InputRecord = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$Source = [Uri]$InputRecord.source_url
if ($Source.Scheme -ne 'https' -or $Source.Host -ne 'msedge.sf.dl.delivery.mp.microsoft.com' -or
    $Source.UserInfo -or $Source.Query -or $Source.Fragment -or
    $InputRecord.filename -cne 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe' -or
    $InputRecord.sha256 -notmatch '^[a-fA-F0-9]{64}$') {
    throw 'Store WebView2 input must be the pinned Microsoft x64 standalone installer.'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$Destination = Join-Path $OutputDirectory $InputRecord.filename
# A failed or changed CDN download must never become a build input.
$Temporary = Join-Path $OutputDirectory ('download-' + [Guid]::NewGuid() + '.exe')
try {
    Invoke-WebRequest -Uri $Source.AbsoluteUri -OutFile $Temporary -TimeoutSec 300 -MaximumRedirection 0
    if ((Get-Item -LiteralPath $Temporary).Length -ne $InputRecord.bytes -or
        (Get-FileHash -LiteralPath $Temporary -Algorithm SHA256).Hash -ne $InputRecord.sha256) {
        throw 'Store WebView2 input size or SHA256 mismatch.'
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Temporary
    if ($Signature.Status -ne 'Valid' -or $null -eq $Signature.SignerCertificate -or
        $Signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
        throw 'Store WebView2 input lacks a trusted Microsoft signature.'
    }
    Move-Item -LiteralPath $Temporary -Destination $Destination -Force
    Write-Host 'Pinned x64 offline WebView2 input verified.'
} finally {
    if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
}
