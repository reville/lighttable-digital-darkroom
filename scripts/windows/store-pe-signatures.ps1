param(
    [Parameter(Mandatory)][string]$Payload,
    [switch]$SignMissing,
    [string]$Report = ""
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = (Resolve-Path -LiteralPath $Payload).Path

function Test-PortableExecutable([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    $Reader = [IO.BinaryReader]::new($Stream)
    try {
        if ($Stream.Length -lt 64 -or $Reader.ReadUInt16() -ne 0x5A4D) { return $false }
        $Stream.Position = 0x3C
        $Offset = $Reader.ReadUInt32()
        if ($Offset -lt 64 -or $Offset -gt $Stream.Length - 4) { return $false }
        $Stream.Position = $Offset
        return $Reader.ReadUInt32() -eq 0x00004550
    } finally {
        $Reader.Dispose()
    }
}

# Use PE headers rather than extensions: Python wheels carry native .pyd files,
# and bundled native code can use other suffixes. Never erase a vendor signature.
$RelativePaths = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -Name | Sort-Object)
$Evidence = @(foreach ($RelativePath in $RelativePaths) {
    $File = Get-Item -LiteralPath (Join-Path $Root $RelativePath)
    $IsPe = Test-PortableExecutable $File.FullName
    if (-not $IsPe) {
        if ($File.Extension.ToLowerInvariant() -in @('.exe', '.dll', '.pyd')) {
            throw "Malformed native binary in payload: $($File.FullName)"
        }
        continue
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $File.FullName
    if ($SignMissing -and $Signature.Status -eq 'NotSigned') {
        & (Join-Path $PSScriptRoot 'sign-release.ps1') -RequireSigning -Files $File.FullName
        $Signature = Get-AuthenticodeSignature -LiteralPath $File.FullName
    }
    @{
        # Enumeration supplies the relative path even when Windows expands an
        # 8.3 temporary-directory alias in the file's absolute FullName.
        path = $RelativePath
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        status = [string]$Signature.Status
        publisher = if ($null -ne $Signature.SignerCertificate) { $Signature.SignerCertificate.Subject } else { $null }
    }
})
if ($Evidence.Count -eq 0) { throw 'No Portable Executable files found in the payload.' }
$Invalid = @($Evidence | Where-Object { $_.status -ne 'Valid' })
if ($Report) {
    @{
        pe_count = $Evidence.Count
        invalid_count = $Invalid.Count
        signatures = $Evidence
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Report -Encoding utf8
}
if ($Invalid.Count -gt 0) {
    throw "Store PE signature check failed for $($Invalid.Count) files: $(($Invalid | ForEach-Object { $_.path + ' (' + $_.status + ')' }) -join ', ')"
}
Write-Host "Verified $($Evidence.Count) Portable Executable signatures."
