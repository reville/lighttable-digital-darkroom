param(
    [Parameter(Mandatory = $true)]
    [string]$Payload
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# OpenImageIO's C++ mutex implementation crashes with older system CRTs.
# Ship the redistributable DLLs beside each executable so per-user installs
# and portable builds do not need an administrator to update Windows first.
$MinimumVersion = [version]"14.44.35211.0"
$Roots = @()
if ($env:VCToolsRedistDir) { $Roots += $env:VCToolsRedistDir }
$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $VsWhere) {
    $Installations = & $VsWhere -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0) { throw "Could not locate Visual C++ redistributable files" }
    foreach ($Installation in $Installations) {
        $Redist = Join-Path $Installation "VC\Redist\MSVC"
        if (Test-Path $Redist) {
            $Roots += @(Get-ChildItem $Redist -Directory | ForEach-Object { $_.FullName })
        }
    }
}

$Candidates = @(
    foreach ($Root in ($Roots | Select-Object -Unique)) {
        $X64 = Join-Path $Root "x64"
        if (-not (Test-Path $X64)) { continue }
        foreach ($Directory in (Get-ChildItem $X64 -Directory -Filter "Microsoft.VC*.CRT")) {
            $Msvcp = Join-Path $Directory.FullName "msvcp140.dll"
            if (Test-Path $Msvcp) {
                $Version = [version](Get-Item $Msvcp).VersionInfo.FileVersion
                if ($Version -ge $MinimumVersion) {
                    [pscustomobject]@{ Directory = $Directory.FullName; Version = $Version }
                }
            }
        }
    }
)
$Selected = $Candidates | Sort-Object Version -Descending | Select-Object -First 1
if (-not $Selected) {
    throw "Visual C++ x64 redistributable $MinimumVersion or newer is required. Install the Visual Studio C++ Build Tools workload."
}

$Libraries = @(Get-ChildItem $Selected.Directory -File -Filter "*.dll")
foreach ($Required in @("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    if ($Required -notin $Libraries.Name) { throw "Incomplete Visual C++ runtime: missing $Required" }
}
foreach ($Library in $Libraries) {
    $Signature = Get-AuthenticodeSignature $Library.FullName
    if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw "Microsoft signature verification failed for $($Library.Name)"
    }
}
foreach ($Destination in @($Payload, (Join-Path $Payload "Python"), (Join-Path $Payload "Resources\LightTable\engine"))) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($Library in $Libraries) {
        Copy-Item -LiteralPath $Library.FullName -Destination $Destination -Force
    }
}
Write-Host "Staged Microsoft Visual C++ runtime $($Selected.Version) beside the application executables"
return $Selected.Version.ToString()
