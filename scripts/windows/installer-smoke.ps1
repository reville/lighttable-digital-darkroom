param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-RawUserPath {
    $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment")
    if ($null -eq $Key) { return $null }
    try {
        return $Key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    } finally {
        $Key.Dispose()
    }
}

function Invoke-InstallerProcess([string]$Executable, [string]$Arguments) {
    $Process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(120000)) {
        $Process.Kill()
        throw "Installer process exceeded the two-minute smoke-test limit"
    }
    if ($Process.ExitCode -ne 0) { throw "Installer process exited with $($Process.ExitCode)" }
}

$ProtocolPath = "HKCU:\Software\Classes\lighttable"
if (Test-Path $ProtocolPath) {
    throw "Installer smoke requires an account without an existing LightTable protocol handler"
}
$RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LightTable"
if (Test-Path $RegistryPath) {
    throw "Installer smoke requires a Windows account without an existing LightTable install"
}
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-install-smoke-" + [Guid]::NewGuid())
$InstallPath = Join-Path $TempRoot "LightTable with spaces"
$DataDirectory = Join-Path $env:LOCALAPPDATA "LightTable"
$DataExisted = Test-Path $DataDirectory
$UserData = Join-Path $DataDirectory ("installer-smoke-" + [Guid]::NewGuid() + ".txt")
$BeforePath = Get-RawUserPath
$BeforeProcessPath = $env:Path
$BeforeLocation = Get-Location
$Uninstaller = Join-Path $InstallPath "Uninstall.exe"
New-Item -ItemType Directory -Force -Path $TempRoot, $DataDirectory | Out-Null
Set-Content -LiteralPath $UserData -Value "preserve catalog and photo data" -NoNewline

try {
    # NSIS requires /D as the final argument, with no quotes around its value.
    Invoke-InstallerProcess $Installer "/S /D=$InstallPath"
    if ((Get-Content -Raw (Join-Path $InstallPath "install-channel.txt")) -ne "direct") {
        throw "Installer did not mark direct update ownership"
    }
    if (-not (Test-Path (Join-Path $InstallPath "WinSparkle.dll"))) {
        throw "The Windows update component was not installed"
    }
    $Installed = Get-ItemProperty $RegistryPath
    if ($Installed.DisplayVersion -ne $Version -or $Installed.Publisher -ne "Nicholas Reville") {
        throw "Installed package identity does not match the release"
    }
    if ($Installed.InstallLocation -ne $InstallPath -or $Installed.QuietUninstallString -notmatch ' /S$') {
        throw "Installer registration is missing installation or silent uninstall metadata"
    }
    $ProtocolCommand = (Get-Item ($ProtocolPath + "\shell\open\command")).GetValue("")
    if ($ProtocolCommand -cne ('"' + $InstallPath + '\LightTable.exe" --preset-url "%1"')) {
        throw "The preset protocol handler is missing or incorrectly quoted"
    }
    $ProtocolKey = Get-Item $ProtocolPath
    if ($ProtocolKey.GetValueNames() -notcontains "URL Protocol") {
        throw "The preset URL protocol marker is missing"
    }
    $FirstPath = Get-RawUserPath
    $BinPath = Join-Path $InstallPath "bin"
    if (@($FirstPath.Split(";") | Where-Object { $_ -ieq $BinPath }).Count -ne 1) {
        throw "The installed CLI must be registered exactly once on user PATH"
    }
    $UnrelatedFile = Join-Path $InstallPath "user-owned-file.txt"
    Set-Content -LiteralPath $UnrelatedFile -Value "preserve unrelated files" -NoNewline

    Set-Content -LiteralPath (Join-Path $InstallPath "install-channel.txt") -Value "winget" -Encoding ascii -NoNewline
    if ([IO.File]::ReadAllText((Join-Path $InstallPath "install-channel.txt")) -cne "winget") { throw "Could not set the repair ownership fixture" }
    # Exercise reinstall/upgrade registration and CLI discovery outside the bundle.
    Invoke-InstallerProcess $Installer "/S /D=$InstallPath"
    $RepairedOwnerPath = Join-Path $InstallPath "install-channel.txt"
    $RepairedOwner = Get-Content -Raw $RepairedOwnerPath
    if ($RepairedOwner -ne "winget") {
        $OwnerBytes = [BitConverter]::ToString([IO.File]::ReadAllBytes($RepairedOwnerPath))
        throw "Reinstallation lost package-manager update ownership: '$RepairedOwner' (bytes $OwnerBytes)"
    }
    if ((Get-RawUserPath) -cne $FirstPath) { throw "Reinstallation changed user PATH" }
    $env:Path = [Environment]::ExpandEnvironmentVariables([string](Get-RawUserPath)) + ";" + $BeforeProcessPath
    Set-Location $TempRoot
    $Command = Get-Command lighttable -CommandType Application
    if ($Command.Source -ine (Join-Path $BinPath "lighttable.cmd")) {
        throw "The lighttable command resolved to the GUI or another installation"
    }
    $Help = & $Command.Source --help 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $Help -notmatch "usage: lighttable") {
        throw "The installed CLI could not run using its bundled runtime"
    }
    # Windows PowerShell 5.1 turns redirected native stderr into error records.
    # This command deliberately writes usage to stderr; validate its exit code.
    try {
        $ErrorActionPreference = "Continue"
        & $Command.Source --invalid-smoke-test-option 2>&1 | Out-Null
        $UsageExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = "Stop"
    }
    if ($UsageExitCode -ne 2) { throw "The CLI wrapper did not preserve the usage-error exit code" }
    $PortableHelp = & (Join-Path $InstallPath "lighttable.cmd") --help 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $PortableHelp -notmatch "usage: lighttable") {
        throw "The portable CLI wrapper failed outside the bundle directory"
    }

    # _?= prevents the uninstaller from forking into a temporary executable, so
    # waiting here waits for the real removal operation.
    Invoke-InstallerProcess $Uninstaller "/S _?=$InstallPath"
    if (Test-Path $RegistryPath) { throw "Uninstall left product registration behind" }
    if (Test-Path $ProtocolPath) { throw "Uninstall left the preset protocol handler behind" }
    if ((Get-RawUserPath) -cne $BeforePath) { throw "Uninstall did not restore the previous user PATH" }
    if (Test-Path (Join-Path $BinPath "lighttable.cmd")) { throw "Uninstall left the CLI installed" }
    if (Test-Path (Join-Path $InstallPath "LightTable.exe")) { throw "Uninstall left the GUI installed" }
    if ((Get-Content -Raw -LiteralPath $UserData) -cne "preserve catalog and photo data") {
        throw "Uninstall changed user data"
    }
    if ((Get-Content -Raw -LiteralPath $UnrelatedFile) -cne "preserve unrelated files") {
        throw "Uninstall changed an unrelated file in the installation directory"
    }
    Write-Host "Windows silent install, reinstall, CLI, and data-preserving uninstall smoke passed"
} finally {
    Set-Location $BeforeLocation
    $env:Path = $BeforeProcessPath
    if ((Test-Path $RegistryPath) -and (Test-Path $Uninstaller)) {
        Invoke-InstallerProcess $Uninstaller "/S _?=$InstallPath"
    }
    Remove-Item -LiteralPath $UserData -Force -ErrorAction SilentlyContinue
    if (-not $DataExisted -and @(Get-ChildItem -LiteralPath $DataDirectory -Force).Count -eq 0) {
        Remove-Item -LiteralPath $DataDirectory -Force
    }
    if ((Get-RawUserPath) -ceq $BeforePath) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    } else {
        Write-Warning "Smoke-test cleanup retained the install directory because PATH restoration failed"
    }
}
