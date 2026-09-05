param(
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$')]
    [string]$Version = "0.1.0",
    [string]$OutputDirectory = "dist",
    [switch]$PortableOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PythonVersion = "3.13.12"
$PythonArchiveSha256 = "76f238f606250c87c6beac75dccd35ee99070a13490555936abb6cb64ecce3d0"
$PythonSourceRevision = "3bb2c2d2801ff68b92019cf1dbcbb133d60832bc"
$RustSourceRevision = "9dd59b0380194b93686aaa230a8bb9680aa270a4"
$Target = "x86_64-pc-windows-msvc"

$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Output = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
} else {
    Join-Path $Project $OutputDirectory
}
$BuildRoot = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-windows-" + [Guid]::NewGuid())
$Payload = Join-Path $BuildRoot "LightTable"
$Resources = Join-Path $Payload "Resources\LightTable"
$Python = Join-Path $Payload "Python"
$PythonExe = Join-Path $Python "python.exe"
$PythonSource = Join-Path $BuildRoot "python-engine"
$RustSource = Join-Path $BuildRoot "rust-engine-source"
$PreviousIcon = [Environment]::GetEnvironmentVariable("LIGHTTABLE_ICON_ICO", "Process")

foreach ($Tool in @("cargo", "git", "rustup", "uv")) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        throw "$Tool is required to build the Windows package"
    }
}

$MakeNsis = Get-Command "makensis" -ErrorAction SilentlyContinue
if (-not $MakeNsis) {
    # Chocolatey's NSIS package does not always refresh PATH in the same step.
    $NsisPath = Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"
    if (Test-Path $NsisPath) { $MakeNsis = Get-Command $NsisPath }
}
if (-not $PortableOnly -and -not $MakeNsis) {
    throw "NSIS is required to build an installer. Use -PortableOnly explicitly for a ZIP-only build."
}

New-Item -ItemType Directory -Force -Path $Output, $Payload, $Resources | Out-Null

try {
    $PythonArchive = Join-Path $BuildRoot "python.zip"
    Invoke-WebRequest `
        -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
        -OutFile $PythonArchive
    $ActualPythonHash = (Get-FileHash -Algorithm SHA256 $PythonArchive).Hash.ToLowerInvariant()
    if ($ActualPythonHash -ne $PythonArchiveSha256) {
        throw "Embedded Python archive hash mismatch"
    }
    Expand-Archive -Path $PythonArchive -DestinationPath $Python
    New-Item -ItemType Directory -Force -Path (Join-Path $Python "Lib\site-packages") | Out-Null
    @(
        "python313.zip"
        "."
        "Lib\site-packages"
        "..\Resources\LightTable"
        "..\Resources\LightTable\vendor\spektrafilm\src"
        "import site"
    ) | Set-Content -Encoding ascii (Join-Path $Python "python313._pth")

    & uv pip install `
        --python-platform $Target `
        --python-version "3.13" `
        --target (Join-Path $Python "Lib\site-packages") `
        --only-binary :all: `
        --requirements (Join-Path $Project "packaging\runtime-windows.lock")
    if ($LASTEXITCODE -ne 0) { throw "Windows runtime dependency installation failed" }

    & git clone --quiet --filter=blob:none https://github.com/andreavolpato/agx-emulsion.git $PythonSource
    & git -C $PythonSource checkout --quiet $PythonSourceRevision
    if ($LASTEXITCODE -ne 0) { throw "Could not check out the pinned Python render source" }

    & git clone --quiet --filter=blob:none https://github.com/turbasvin/spektrafilm-rs.git $RustSource
    & git -C $RustSource checkout --quiet $RustSourceRevision
    if ($LASTEXITCODE -ne 0) { throw "Could not check out the pinned Rust render source" }

    # Every top-level module server.py imports. The Python contract test
    # `WindowsPackagingContractTests` compares this list against those imports,
    # so a new module cannot ship on macOS and be missing here.
    foreach ($File in @(
        "server.py",
        "events.py",
        "jobs.py",
        "validation.py",
        "media_formats.py",
        "film_pipeline.py",
        "grade.py",
        "edits.py",
        "color_pipeline.py",
        "calibration_target.py",
        "preset_io.py",
        "platform_image.py",
        "semantic_masks.py",
        "export_workflow.py",
        "library_workflow.py",
        "merge_workflow.py",
        "soft_proof.py",
        "catalog.py",
        "catalog_scan.py",
        "catalog_import.py",
        "xmp_sidecar.py",
        "durable_io.py",
        "recovery.py",
        "ingest_workflow.py",
        "watch_workflow.py",
        "geometry_auto.py",
        "enhance_workflow.py",
        "render_cli.py"
    )) {
        Copy-Item (Join-Path $Project $File) $Resources
    }
    Copy-Item (Join-Path $Project "media-formats.json") $Resources
    Copy-Item (Join-Path $Project "lighttable_cli") $Resources -Recurse
    Copy-Item (Join-Path $Project "scripts\windows\lighttable.cmd") $Payload
    $CommandDirectory = Join-Path $Payload "bin"
    New-Item -ItemType Directory -Force -Path $CommandDirectory | Out-Null
    Copy-Item (Join-Path $Project "scripts\windows\lighttable.cmd") $CommandDirectory
    Copy-Item (Join-Path $Project "scripts\windows\update-user-path.ps1") $Payload

    $AiPackage = Join-Path $Resources "film_lab_ai"
    New-Item -ItemType Directory -Force -Path $AiPackage | Out-Null
    Get-ChildItem (Join-Path $Project "film_lab_ai") -Filter *.py |
        ForEach-Object { Copy-Item $_.FullName $AiPackage }
    Copy-Item (Join-Path $Project "web") $Resources -Recurse
    Copy-Item (Join-Path $Project "profiles") $Resources -Recurse

    $VendoredSource = Join-Path $Resources "vendor\spektrafilm"
    New-Item -ItemType Directory -Force -Path $VendoredSource | Out-Null
    Copy-Item (Join-Path $PythonSource "src") $VendoredSource -Recurse

    $Engine = Join-Path $Resources "engine"
    New-Item -ItemType Directory -Force -Path $Engine | Out-Null
    Copy-Item (Join-Path $RustSource "data") $Engine -Recurse
    Copy-Item (Join-Path $Project "profiles\*.json") (Join-Path $Engine "data\profiles")
    Set-Content -Encoding ascii (Join-Path $Engine "VERSION.txt") $RustSourceRevision

    $Licenses = Join-Path $Resources "licenses"
    New-Item -ItemType Directory -Force -Path $Licenses | Out-Null
    Copy-Item (Join-Path $PythonSource "LICENSE") (Join-Path $Licenses "python-render-engine.txt")
    Copy-Item (Join-Path $RustSource "LICENSE") (Join-Path $Licenses "rust-render-engine.txt")
    foreach ($Notice in @("LICENSE", "THIRD_PARTY_NOTICES.md")) {
        if (Test-Path (Join-Path $Project $Notice)) {
            Copy-Item (Join-Path $Project $Notice) $Payload
        }
    }

    & $PythonExe (Join-Path $Project "scripts\fetch-color-profiles.py") (Join-Path $Resources "color-profiles")
    if ($LASTEXITCODE -ne 0) { throw "Color-profile download failed" }

    & rustup target add $Target
    if ($LASTEXITCODE -ne 0) { throw "The Windows Rust target could not be installed" }

    & cargo test --locked --target $Target --manifest-path (Join-Path $Project "rust-engine\Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "The resident render-engine tests failed" }
    & cargo test --locked --target $Target --manifest-path (Join-Path $Project "windows-shell\Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "The Windows desktop-shell tests failed" }

    & cargo build --locked --release --target $Target --manifest-path (Join-Path $Project "rust-engine\Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "The resident render engine failed to build" }
    Copy-Item `
        (Join-Path $Project "rust-engine\target\$Target\release\lighttable-engine.exe") `
        (Join-Path $Engine "lighttable-engine.exe")

    & cargo build --locked --release --target $Target --manifest-path (Join-Path $RustSource "Cargo.toml") -p spektrafilm-cli --bin spektrafilm
    if ($LASTEXITCODE -ne 0) { throw "The export render engine failed to build" }
    Copy-Item `
        (Join-Path $RustSource "target\$Target\release\spektrafilm.exe") `
        (Join-Path $Engine "spektrafilm-rs.exe")

    $Icon = Join-Path $BuildRoot "LightTable.ico"
    & $PythonExe (Join-Path $Project "scripts\windows\make-icon.py") (Join-Path $Project "build\icon-1024.png") $Icon
    if ($LASTEXITCODE -ne 0) { throw "The Windows icon could not be built" }
    Copy-Item $Icon $Payload
    $env:LIGHTTABLE_ICON_ICO = $Icon
    & cargo build --locked --release --target $Target --manifest-path (Join-Path $Project "windows-shell\Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "The Windows desktop shell failed to build" }
    Copy-Item `
        (Join-Path $Project "windows-shell\target\$Target\release\lighttable-desktop-shell.exe") `
        (Join-Path $Payload "LightTable.exe")

    & $PythonExe -B `
        (Join-Path $Project "scripts\windows\runtime-smoke.py") `
        $Resources `
        (Join-Path $Payload "LightTable.exe")
    if ($LASTEXITCODE -ne 0) { throw "The packaged Windows runtime smoke test failed" }

    $SourceRevision = & git -C $Project rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve the package source revision" }
    @{
        version = $Version
        source_revision = $SourceRevision.Trim()
        architecture = "x64"
        python_version = $PythonVersion
    } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Payload "build-manifest.json")

    $Zip = Join-Path $Output "LightTable-$Version-windows-x64.zip"
    if (Test-Path $Zip) { Remove-Item -Force $Zip }
    Compress-Archive -Path $Payload -DestinationPath $Zip -CompressionLevel Optimal

    if (-not $PortableOnly) {
        $Installer = Join-Path $Output "LightTable-$Version-windows-x64-setup.exe"
        $UninstallManifest = Join-Path $BuildRoot "uninstall-files.nsh"
        & $PythonExe -B (Join-Path $Project "scripts\windows\make-uninstall-manifest.py") $Payload $UninstallManifest
        if ($LASTEXITCODE -ne 0) { throw "Could not generate the safe uninstall file list" }
        & $MakeNsis.Source `
            "/WX" `
            "/DVERSION=$Version" `
            "/DPAYLOAD=$Payload" `
            "/DOUTPUT=$Installer" `
            "/DUNINSTALL_MANIFEST=$UninstallManifest" `
            (Join-Path $Project "scripts\windows\installer.nsi")
        if ($LASTEXITCODE -ne 0) { throw "The Windows installer failed to build" }
        & (Join-Path $Project "scripts\windows\installer-smoke.ps1") -Installer $Installer -Version $Version
    }

    Write-Host "Built Windows artifacts in $Output"
} finally {
    [Environment]::SetEnvironmentVariable("LIGHTTABLE_ICON_ICO", $PreviousIcon, "Process")
    if (Test-Path $BuildRoot) {
        Remove-Item -Recurse -Force $BuildRoot
    }
}
