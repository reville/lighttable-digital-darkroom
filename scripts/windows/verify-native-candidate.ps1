param(
    [Parameter(Mandatory)][string]$Directory,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceRevision,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$InstallerSha256
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Receipt = Get-Content -LiteralPath (Join-Path $Directory 'store-candidate-receipt.json') -Raw | ConvertFrom-Json
if ($Receipt.source_revision -cne $SourceRevision -or $Receipt.version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    throw 'Candidate receipt source or version does not match the selected build.'
}
$Archives = @(Get-ChildItem -LiteralPath $Directory -File -Filter 'LightTable-*-windows-x64.zip')
$Installers = @(Get-ChildItem -LiteralPath $Directory -File -Filter 'LightTable-*-windows-x64-setup.exe')
if ($Archives.Count -ne 1 -or $Installers.Count -ne 1) { throw 'Expected one archive and one installer.' }
$Archive = $Archives[0]
$Installer = $Installers[0]
if ($Installer.Name -cne "LightTable-$($Receipt.version)-windows-x64-setup.exe" -or
    $Archive.Name -cne "LightTable-$($Receipt.version)-windows-x64.zip") {
    throw 'Candidate filenames do not match its version.'
}
foreach ($File in @($Archive, $Installer)) {
    $Entries = @($Receipt.artifacts | Where-Object { $_.file -ceq $File.Name })
    $Hash = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Entries.Count -ne 1 -or $Entries[0].sha256 -cne $Hash -or $Entries[0].bytes -ne $File.Length) {
        throw 'Candidate artifact hash or size differs from its receipt.'
    }
    if ($File.Name -ceq $Installer.Name -and $Hash -cne $InstallerSha256) {
        throw 'Installer differs from the independently verified candidate.'
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = [IO.Compression.ZipFile]::OpenRead($Archive.FullName)
try {
    $Entries = @($Zip.Entries | Where-Object { $_.FullName -ceq 'LightTable/build-manifest.json' })
    if ($Entries.Count -ne 1) { throw 'Expected one bundle identity manifest.' }
    $Reader = [IO.StreamReader]::new($Entries[0].Open())
    try { $Manifest = $Reader.ReadToEnd() | ConvertFrom-Json } finally { $Reader.Dispose() }
    if ($Manifest.source_revision -cne $SourceRevision -or $Manifest.version -cne $Receipt.version -or
        $Manifest.architecture -cne 'x64' -or $Manifest.store_candidate -ne $true -or
        $Manifest.authenticode_signed -ne $true) {
        throw 'Bundle identity differs from the selected signed Store candidate.'
    }
} finally { $Zip.Dispose() }
$Receipt.version
