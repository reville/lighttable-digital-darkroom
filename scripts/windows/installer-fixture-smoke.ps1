param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Fast installer-only acceptance. No fixture executable/DLL is ever launched.
# Native loading, WebView2, rendering, and packaged Python require separate gates.
if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
    throw "Installer fixture smoke requires 64-bit Windows."
}
foreach ($Key in @("HKCU:\Software\Classes\lighttable",
                   "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LightTable")) {
    if (Test-Path $Key) { throw "Installer fixture smoke requires an account without an existing LightTable install or protocol handler." }
}
foreach ($Shortcut in @(
    (Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "LightTable.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "LightTable")
)) {
    if (Test-Path -LiteralPath $Shortcut) { throw "Refusing to overwrite existing LightTable shortcuts during installer fixture smoke." }
}
$MakeNsis = Get-Command "makensis.exe" -ErrorAction SilentlyContinue
if (-not $MakeNsis) {
    $NsisPath = Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"
    if (Test-Path -LiteralPath $NsisPath) { $MakeNsis = Get-Command $NsisPath }
}
if (-not $MakeNsis) { throw "Install NSIS before running installer fixture smoke." }
$PythonCommand = Get-Command $Python -ErrorAction Stop
$Root = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-installer-fixture-" + [Guid]::NewGuid())
$Payload = Join-Path $Root "payload"
$Source = Join-Path $Root "source"
$Installer = Join-Path $Root "installer-fixture.exe"
$Manifest = Join-Path $Root "uninstall-files.nsh"
$BeforeLocation = Get-Location
$Version = "0.0.0"
try {
    New-Item -ItemType Directory -Path $Root, $Payload, $Source, (Join-Path $Payload "bin") | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "installer.nsi") -Destination (Join-Path $Source "installer.nsi")
    # Temporary diagnosis of the real install-channel read; removed after the
    # repaired fixture passes. No production installer is instrumented.
    $TracePath = Join-Path $Root "installer-trace.txt"
    $SourcePath = Join-Path $Source "installer.nsi"
    $SourceText = [IO.File]::ReadAllText($SourcePath)
    $SourceText = $SourceText.Replace('      FileRead $0 $1', @'
      FileRead $0 $1
      FileOpen $9 "TRACE_PATH" a
      FileWrite $9 "read INSTDIR=<$INSTDIR> owner=<$UpdateOwner> contents=<$1>$\r$\n"
      FileClose $9
'@.Replace('TRACE_PATH', $TracePath))
    $SourceText = $SourceText.Replace('  FileWrite $0 "$UpdateOwner"', @'
  FileWrite $0 "$UpdateOwner"
  FileOpen $9 "TRACE_PATH" a
  FileWrite $9 "write INSTDIR=<$INSTDIR> owner=<$UpdateOwner>$\r$\n"
  FileClose $9
'@.Replace('TRACE_PATH', $TracePath))
    [IO.File]::WriteAllText($SourcePath, $SourceText)
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "update-user-path.ps1") -Destination $Payload
    # This stub exists only beside the temporary COPY of installer.nsi. It
    # prevents prerequisite downloads and certifies no WebView2/runtime behavior.
    [IO.File]::WriteAllText((Join-Path $Source "ensure-webview2.ps1"),
        "# Installer fixture only; WebView2 is deliberately outside this gate.`r`nexit 0`r`n", [Text.Encoding]::ASCII)
    [IO.File]::WriteAllBytes((Join-Path $Payload "LightTable.exe"), [byte[]](77, 90, 0, 0))
    [IO.File]::WriteAllText((Join-Path $Payload "WinSparkle.dll"), "installer fixture only", [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText((Join-Path $Payload "install-channel.txt"), "portable", [Text.Encoding]::ASCII)
    $Command = @'
@echo off
if "%~1"=="--help" (
  echo usage: lighttable [installer fixture]
  exit /b 0
)
exit /b 2
'@
    $Command = $Command.Replace("`r`n", "`n").Replace("`n", "`r`n") + "`r`n"
    [IO.File]::WriteAllText((Join-Path $Payload "lighttable.cmd"), $Command, [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText((Join-Path $Payload "bin\lighttable.cmd"), $Command, [Text.Encoding]::ASCII)
    & $PythonCommand.Source -B (Join-Path $PSScriptRoot "make-uninstall-manifest.py") $Payload $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Could not generate the fixture uninstall manifest." }
    Set-Location $Source
    & $MakeNsis.Source "/WX" "/DVERSION=$Version" "/DPAYLOAD=$Payload" "/DOUTPUT=$Installer" "/DUNINSTALL_MANIFEST=$Manifest" (Join-Path $Source "installer.nsi")
    if ($LASTEXITCODE -ne 0) { throw "Could not compile the actual installer source with its fixture payload." }
    Set-Location $BeforeLocation
    Write-Host "Installer-only fixture: real NSIS source, real PATH registration and uninstall; dummy runtime and prerequisite."
    & (Join-Path $PSScriptRoot "installer-smoke.ps1") -Installer $Installer -Version $Version
    # The shared smoke owns installation cleanup, restoration of PATH and user
    # data checks; its exceptions propagate. This outer scope owns only fixtures.
    Write-Host "Installer-only fixture acceptance passed. Native/runtime acceptance remains a separate check."
} finally {
    Set-Location $BeforeLocation
    if (Test-Path -LiteralPath $TracePath) { Get-Content -LiteralPath $TracePath | Write-Host }
    if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force }
}
