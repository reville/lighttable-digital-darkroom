param(
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$')]
    [string]$Version = "0.1.0",
    [string]$OutputDirectory = "dist",
    [switch]$PortableOnly,
    [switch]$RuntimeSmokeOnly,
    [switch]$RequireSigning
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Public releases must fail before downloads or compilation when signing is
# unavailable. CI builds can run without a certificate unless explicitly gated.
if ($RuntimeSmokeOnly -and ($RequireSigning -or $PortableOnly)) {
    throw "RuntimeSmokeOnly cannot be combined with release artifact options"
}
$SigningEnabled = if ($RuntimeSmokeOnly) { $false } else {
    & (Join-Path $PSScriptRoot "sign-release.ps1") -CheckOnly -RequireSigning:$RequireSigning
}

if ($RequireSigning -and [string]::IsNullOrWhiteSpace($env:SPARKLE_PRIVATE_KEY)) {
    throw "Windows release updates require SPARKLE_PRIVATE_KEY."
}
$WinSparkleVersion = "0.9.4"
$WinSparkleArchiveSha256 = "6037df37fc263bd1650a1c4949681a9d40ffe991d01f35892a406cb5d103c976"
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
$PreviousTestUpdater = [Environment]::GetEnvironmentVariable("LIGHTTABLE_TEST_WINSPARKLE_DLL", "Process")
$PreviousIcon = [Environment]::GetEnvironmentVariable("LIGHTTABLE_ICON_ICO", "Process")

$RequiredTools = if ($RuntimeSmokeOnly) { @("git", "uv") } else { @("cargo", "git", "rustup", "uv") }
foreach ($Tool in $RequiredTools) {
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
if (-not $RuntimeSmokeOnly -and -not $PortableOnly -and -not $MakeNsis) {
    throw "NSIS is required to build an installer. Use -PortableOnly explicitly for a ZIP-only build."
}

New-Item -ItemType Directory -Force -Path $Payload, $Resources | Out-Null
if (-not $RuntimeSmokeOnly) {
    New-Item -ItemType Directory -Force -Path $Output | Out-Null
}

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
    ) | Set-Content -LiteralPath (Join-Path $Python "python313._pth") -Encoding ascii

    $VCRuntimeVersion = & (Join-Path $PSScriptRoot "stage-vc-runtime.ps1") -Payload $Payload

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

    # Stage every root runtime module, including transitive and optional imports.
    & $PythonExe -B (Join-Path $PSScriptRoot "stage-python-modules.py") $Project $Resources
    if ($LASTEXITCODE -ne 0) { throw "Python runtime module staging failed" }
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
    Copy-Item (Join-Path $Project "film_lab_ai\licenses") $AiPackage -Recurse
    Copy-Item (Join-Path $Project "web") $Resources -Recurse
    $BuildAssets = Join-Path $Resources "build"
    New-Item -ItemType Directory -Force -Path $BuildAssets | Out-Null
    Copy-Item (Join-Path $Project "build\icon-1024.png") $BuildAssets
    Copy-Item (Join-Path $Project "profiles") $Resources -Recurse
    Copy-Item (Join-Path $Project "presets") $Resources -Recurse

    $VendoredSource = Join-Path $Resources "vendor\spektrafilm"
    New-Item -ItemType Directory -Force -Path $VendoredSource | Out-Null
    Copy-Item (Join-Path $PythonSource "src") $VendoredSource -Recurse

    $Engine = Join-Path $Resources "engine"
    New-Item -ItemType Directory -Force -Path $Engine | Out-Null
    Copy-Item (Join-Path $RustSource "data") $Engine -Recurse
    Copy-Item (Join-Path $Project "profiles\*.json") (Join-Path $Engine "data\profiles")
    Set-Content -LiteralPath (Join-Path $Engine "VERSION.txt") -Value $RustSourceRevision -Encoding ascii

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

    # Pin and verify the upstream updater binary before it reaches the payload.
    $WinSparkleArchive = Join-Path $BuildRoot "winsparkle.zip"
    Invoke-WebRequest -Uri "https://github.com/vslavik/winsparkle/releases/download/v$WinSparkleVersion/WinSparkle-$WinSparkleVersion.zip" -OutFile $WinSparkleArchive
    if ((Get-FileHash -Algorithm SHA256 $WinSparkleArchive).Hash.ToLowerInvariant() -ne $WinSparkleArchiveSha256) {
        throw "WinSparkle archive hash mismatch"
    }
    $WinSparkleRoot = Join-Path $BuildRoot "winsparkle"
    Expand-Archive -Path $WinSparkleArchive -DestinationPath $WinSparkleRoot
    $WinSparkle = Join-Path $WinSparkleRoot "WinSparkle-$WinSparkleVersion"
    & (Join-Path $PSScriptRoot "stage-winsparkle.ps1") -Sdk $WinSparkle -Payload $Payload
    # ZIP extraction stays portable; NSIS writes direct ownership after copying.
    Set-Content -LiteralPath (Join-Path $Payload "install-channel.txt") -Value "portable" -Encoding ascii -NoNewline

    # Pull requests exercise the exact embedded Python and updater payload before paying
    # for native engine/shell compilation, signing, or installer creation.
    if ($RuntimeSmokeOnly) {
        & $PythonExe -B (Join-Path $PSScriptRoot "runtime-smoke.py") $Resources
        if ($LASTEXITCODE -ne 0) { throw "The staged Windows runtime smoke test failed" }
        Write-Host "Staged Windows runtime smoke passed"
        return
    }

    & rustup target add $Target
    if ($LASTEXITCODE -ne 0) { throw "The Windows Rust target could not be installed" }

    & cargo test --locked --target $Target --manifest-path (Join-Path $Project "rust-engine\Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "The resident render-engine tests failed" }
    $env:LIGHTTABLE_TEST_WINSPARKLE_DLL = Join-Path $Payload "WinSparkle.dll"
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

    if ($SigningEnabled) {
        & (Join-Path $PSScriptRoot "sign-release.ps1") -RequireSigning -Files @(
            (Join-Path $Payload "LightTable.exe"),
            (Join-Path $Payload "WinSparkle.dll"),
            (Join-Path $Engine "lighttable-engine.exe"),
            (Join-Path $Engine "spektrafilm-rs.exe")
        )
    }

    & $PythonExe -B `
        (Join-Path $Project "scripts\windows\runtime-smoke.py") `
        $Resources `
        (Join-Path $Payload "LightTable.exe")
    if ($LASTEXITCODE -ne 0) { throw "The packaged Windows runtime smoke test failed" }

    & (Join-Path $PSScriptRoot "updater-smoke.ps1") -Application (Join-Path $Payload "LightTable.exe")

    $SourceRevision = & git -C $Project rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve the package source revision" }
    @{
        version = $Version
        source_revision = $SourceRevision.Trim()
        architecture = "x64"
        winsparkle_version = $WinSparkleVersion
        python_version = $PythonVersion
        vc_runtime_version = $VCRuntimeVersion
        authenticode_signed = [bool]$SigningEnabled
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Payload "build-manifest.json") -Encoding utf8

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
        if ($SigningEnabled) {
            & (Join-Path $PSScriptRoot "sign-release.ps1") -RequireSigning -Files $Installer
        }
        & (Join-Path $Project "scripts\windows\installer-smoke.ps1") -Installer $Installer -Version $Version
        if ($RequireSigning) {
            # WinSparkle accepts both the current Sparkle 32-byte seed and its
            # older 96-byte private-key format. No key material is logged or
            # included in the package. Sign only after Authenticode is final.
            $UpdateTool = Join-Path $WinSparkle "bin\winsparkle-tool.exe"
            $KeyDirectory = Join-Path $BuildRoot "update-signing"
            New-Item -ItemType Directory -Path $KeyDirectory | Out-Null
            $Identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $Acl = [Security.AccessControl.DirectorySecurity]::new()
            $Acl.SetAccessRuleProtection($true, $false)
            $Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $Identity, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"))
            Set-Acl -LiteralPath $KeyDirectory -AclObject $Acl
            $KeyFile = Join-Path $KeyDirectory "key"
            try {
                [IO.File]::WriteAllText($KeyFile, $env:SPARKLE_PRIVATE_KEY.Trim(), [Text.Encoding]::ASCII)
                $Signature = (& $UpdateTool sign --private-key-file $KeyFile $Installer | Out-String).Trim()
                if ($LASTEXITCODE -ne 0 -or $Signature -notmatch '^[A-Za-z0-9+/]{86}==$') {
                    throw "The Windows update signature could not be created."
                }
                $PublicKey = "mrBmSL8f0FRN8j/imZxCWdCt0L4N3zP9kgOleH46eXA="
                & $UpdateTool verify --public-key $PublicKey --signature $Signature $Installer
                if ($LASTEXITCODE -ne 0) { throw "The Windows update signature does not match the installed public key." }
                & $PythonExe -B (Join-Path $PSScriptRoot "write-appcast.py") $Version $Installer $Signature (Join-Path $Output "appcast-windows-x64.xml")
                if ($LASTEXITCODE -ne 0) { throw "The Windows update feed could not be written." }
            } finally {
                Remove-Item -LiteralPath $KeyDirectory -Recurse -Force
            }
        }
    }

    # Archive only after every required signature and installer check passes.
    $Zip = Join-Path $Output "LightTable-$Version-windows-x64.zip"
    if (Test-Path $Zip) { Remove-Item -Force $Zip }
    Compress-Archive -Path $Payload -DestinationPath $Zip -CompressionLevel Optimal

    Write-Host "Built Windows artifacts in $Output"
} finally {
    [Environment]::SetEnvironmentVariable("LIGHTTABLE_TEST_WINSPARKLE_DLL", $PreviousTestUpdater, "Process")
    [Environment]::SetEnvironmentVariable("LIGHTTABLE_ICON_ICO", $PreviousIcon, "Process")
    if (Test-Path $BuildRoot) {
        Remove-Item -Recurse -Force $BuildRoot
    }
}
