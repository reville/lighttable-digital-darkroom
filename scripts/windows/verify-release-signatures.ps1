param(
    [Parameter(Mandatory)][string]$Archive,
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$Report
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Directory = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-signature-check-" + [Guid]::NewGuid())
try {
    Expand-Archive -LiteralPath $Archive -DestinationPath $Directory
    $Bundle = Join-Path $Directory "LightTable"
    $Manifest = Get-Content -LiteralPath (Join-Path $Bundle "build-manifest.json") -Raw | ConvertFrom-Json
    if (-not $Manifest.authenticode_signed) { throw "The archive is not marked as signed." }
    $ExpectedRevision = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $Manifest.source_revision -ne $ExpectedRevision) {
        throw "The archive source revision does not match the checked-out build."
    }
    $Paths = @("LightTable.exe", "WinSparkle.dll", "Resources/LightTable/engine/lighttable-engine.exe", "Resources/LightTable/engine/spektrafilm-rs.exe")
    $Files = @($Paths | ForEach-Object { Join-Path $Bundle $_ }) + @((Resolve-Path -LiteralPath $Installer).Path)
    $Evidence = @(foreach ($File in $Files) {
        $Signature = Get-AuthenticodeSignature -LiteralPath $File
        if ($Signature.Status -ne "Valid" -or $null -eq $Signature.TimeStamperCertificate) {
            throw "Invalid or missing timestamped Authenticode signature: $([IO.Path]::GetFileName($File))"
        }
        @{
            file = [IO.Path]::GetFileName($File)
            sha256 = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
            status = [string]$Signature.Status
            publisher = $Signature.SignerCertificate.Subject
            certificate_thumbprint = $Signature.SignerCertificate.Thumbprint
            timestamp_authority = $Signature.TimeStamperCertificate.Subject
        }
    })
    @{
        source_revision = $ExpectedRevision
        version = $Manifest.version
        archive_sha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        signatures = $Evidence
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Report -Encoding utf8
    Write-Host "Verified all $($Evidence.Count) application and installer signatures in the completed artifacts."
} finally {
    if (Test-Path -LiteralPath $Directory) { Remove-Item -LiteralPath $Directory -Recurse -Force }
}
