param(
    [Parameter(Mandatory = $true)]
    [string]$Application
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-UpdaterLogText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    # Read only the bounded tail: diagnostics must not obscure the CI failure.
    $Stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $Count = [int][Math]::Min(4096, $Stream.Length)
        if ($Count -eq 0) { return "" }
        [void]$Stream.Seek(-$Count, [IO.SeekOrigin]::End)
        $Bytes = New-Object byte[] $Count
        $Read = $Stream.Read($Bytes, 0, $Count)
        return [Text.Encoding]::UTF8.GetString($Bytes, 0, $Read).Trim()
    } finally {
        $Stream.Dispose()
    }
}

function Get-UpdaterProcessDiagnostic([Diagnostics.Process]$Process, [string]$Helper) {
    $Process.Refresh()
    $State = "still running"
    if ($Process.HasExited) {
        # Retain the unsigned NTSTATUS representation, e.g. 0xC0000139 for a
        # missing DLL entry point. A generic readiness timeout loses this clue.
        $Process.WaitForExit()
        $Code = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$Process.ExitCode), 0)
        $State = "exit 0x{0:X8} ({1})" -f $Code, $Process.ExitCode
    }
    $Details = @("helper PID $($Process.Id): $State")
    foreach ($Path in @([IO.Path]::ChangeExtension($Helper, "error.txt"), "$Helper.stdout.txt", "$Helper.stderr.txt")) {
        $Content = Get-UpdaterLogText $Path
        if ($Content) { $Details += "$([IO.Path]::GetFileName($Path)): $Content" }
    }
    return $Details -join "; "
}

function Assert-UpdaterRejection([Diagnostics.Process]$Process, [string]$Helper, [string]$Reason) {
    $Process.Refresh()
    $Diagnostic = Get-UpdaterProcessDiagnostic $Process $Helper
    $NativeError = Get-UpdaterLogText ([IO.Path]::ChangeExtension($Helper, "error.txt"))
    if (-not $Process.HasExited -or $Process.ExitCode -eq 0 -or -not $NativeError.Contains($Reason)) {
        throw "Expected native rejection '$Reason'; $Diagnostic"
    }
}

# Exercise the real native helper without replacing an installed application,
# opening a window, running an actual installer, or touching a user catalog.
$Compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $Compiler)) { throw "The updater smoke test requires the .NET Framework C# compiler" }
if (-not ("LightTableUpdaterSmoke.Native" -as [type])) {
    Add-Type -Namespace LightTableUpdaterSmoke -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetErrorMode(uint mode);
'@
}
# Child loader failures must report an exit status, never wait on an error dialog.
$PreviousErrorMode = [LightTableUpdaterSmoke.Native]::SetErrorMode(0x8003)
$Root = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-updater-smoke-" + [Guid]::NewGuid())
$Processes = [Collections.Generic.List[Diagnostics.Process]]::new()
$StopFiles = [Collections.Generic.List[string]]::new()
New-Item -ItemType Directory -Path $Root | Out-Null
try {
    $Source = Join-Path $Root "fixture.cs"
    $Fixture = Join-Path $Root "fixture.exe"
    @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
class Fixture {
    static int Main(string[] args) {
        if (args.Length == 2 && args[0] == "--wait") {
            var deadline = DateTime.UtcNow.AddSeconds(45);
            while (!File.Exists(args[1]) && DateTime.UtcNow < deadline) Thread.Sleep(20);
            return File.Exists(args[1]) ? 0 : 2;
        }
        var executable = Process.GetCurrentProcess().MainModule.FileName;
        var receipt = Path.GetFileName(executable) == "setup.exe" ? "installed.txt" : "reopened.txt";
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(executable), receipt), Environment.CommandLine);
        return 0;
    }
}
'@ | Set-Content -LiteralPath $Source -Encoding ascii
    & $Compiler /nologo /target:winexe "/out:$Fixture" $Source
    if ($LASTEXITCODE -ne 0) { throw "The updater fixture could not compile" }

    foreach ($Scenario in @("cancel", "install", "portable")) {
        $Stage = Join-Path $Root "$Scenario helper"
        $Install = Join-Path $Root "$Scenario install with spaces"
        New-Item -ItemType Directory -Path $Stage, $Install | Out-Null
        $Helper = Join-Path $Stage "LightTable-update.exe"
        $Installer = Join-Path $Stage "setup.exe"
        $Target = Join-Path $Install "LightTable.exe"
        # Match production stage_installer: only the EXE is relocated. Copying
        # extra DLLs here would conceal a broken real update helper.
        Copy-Item -LiteralPath $Application -Destination $Helper
        Copy-Item -LiteralPath $Fixture -Destination $Installer
        Copy-Item -LiteralPath $Fixture -Destination $Target
        $Owner = if ($Scenario -eq "portable") { "portable" } else { "direct" }
        Set-Content -LiteralPath (Join-Path $Install "install-channel.txt") -Encoding ascii -NoNewline $Owner
        Set-Content -LiteralPath (Join-Path $Install "build-manifest.json") -Encoding ascii -NoNewline '{"version":"1.2.3","authenticode_signed":true}'
        $Loader = Start-Process -FilePath $Helper -ArgumentList "--runtime-smoke" -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput "$Helper.stdout.txt" -RedirectStandardError "$Helper.stderr.txt"
        $Processes.Add($Loader)
        $Loaded = $Loader.WaitForExit(10000)
        if ($Loaded) { $Loader.WaitForExit() } # Finish draining redirected output.
        if (-not $Loaded -or $Loader.ExitCode -ne 0 -or
                (Get-UpdaterLogText "$Helper.stdout.txt") -notmatch "LightTable shell loader smoke passed") {
            throw "The relocated native updater could not load; $(Get-UpdaterProcessDiagnostic $Loader $Helper)"
        }
        $GuiStop = Join-Path $Stage "gui-stop"
        $ServerStop = Join-Path $Stage "server-stop"
        $StopFiles.Add($GuiStop)
        $StopFiles.Add($ServerStop)
        $Gui = Start-Process -FilePath $Target -ArgumentList ('--wait "{0}"' -f $GuiStop) -PassThru -WindowStyle Hidden
        $Server = Start-Process -FilePath $Target -ArgumentList ('--wait "{0}"' -f $ServerStop) -PassThru -WindowStyle Hidden
        $Processes.Add($Gui)
        $Processes.Add($Server)
        $Ready = Join-Path $Stage "ready"
        $HelperArguments = '--apply-windows-update "{0}" "{1}" {2} {3} "{4}"' -f $Installer, $Target, $Gui.Id, $Server.Id, $Ready
        $Update = Start-Process -FilePath $Helper -ArgumentList $HelperArguments -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput "$Helper.stdout.txt" -RedirectStandardError "$Helper.stderr.txt"
        $Processes.Add($Update)
        if ($Scenario -eq "portable") {
            if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -eq 0 -or (Test-Path $Ready)) {
                throw "The native helper accepted a portable installation or did not exit; $(Get-UpdaterProcessDiagnostic $Update $Helper)"
            }
            Assert-UpdaterRejection $Update $Helper "This installation does not allow direct updates"
        } else {
            $Deadline = (Get-Date).AddSeconds(10)
            while (-not (Test-Path $Ready) -and (Get-Date) -lt $Deadline -and -not $Update.HasExited) { Start-Sleep -Milliseconds 20 }
            if (-not (Test-Path $Ready)) {
                throw "The native helper did not acknowledge both process handles; $(Get-UpdaterProcessDiagnostic $Update $Helper); GUI exited=$($Gui.HasExited); server exited=$($Server.HasExited)"
            }
            if ($Scenario -eq "cancel") {
                Set-Content -LiteralPath (Join-Path $Stage "cancelled") -Value "cancelled"
                if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -eq 0) {
                    throw "The update helper ignored cancellation; $(Get-UpdaterProcessDiagnostic $Update $Helper)"
                }
                Assert-UpdaterRejection $Update $Helper "The update was cancelled"
                if ($Gui.HasExited -or $Server.HasExited) { throw "Cancelling an update terminated an application process" }
            } else {
                Set-Content -LiteralPath (Join-Path $Stage "authorized") -Value "saved and closed"
                Set-Content -LiteralPath $GuiStop -Value "stop"
                if (-not $Gui.WaitForExit(5000)) { throw "The GUI fixture did not close" }
                Start-Sleep -Milliseconds 100
                if (Test-Path (Join-Path $Stage "installed.txt")) { throw "Installation started while the render server was running" }
                Set-Content -LiteralPath $ServerStop -Value "stop"
                if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -ne 0) {
                    throw "The native helper did not complete the update; $(Get-UpdaterProcessDiagnostic $Update $Helper)"
                }
                $Receipt = Join-Path $Stage "installed.txt"
                if (-not (Test-Path $Receipt)) { throw "The staged installer was not called" }
                if (-not (Get-Content -Raw $Receipt).EndsWith("/D=$Install")) {
                    throw "The NSIS installation directory was not the final unquoted argument"
                }
                $Deadline = (Get-Date).AddSeconds(5)
                while (-not (Test-Path (Join-Path $Install "reopened.txt")) -and (Get-Date) -lt $Deadline) { Start-Sleep -Milliseconds 20 }
                if (-not (Test-Path (Join-Path $Install "reopened.txt"))) { throw "The updated application was not reopened" }
            }
        }
        if ($Scenario -ne "install" -and (Test-Path (Join-Path $Stage "installed.txt"))) { throw "A cancelled or portable update ran an installer" }
        Set-Content -LiteralPath $GuiStop -Value "stop"
        Set-Content -LiteralPath $ServerStop -Value "stop"
        [void]$Gui.WaitForExit(5000)
        [void]$Server.WaitForExit(5000)
    }
    Write-Host "Windows updater process waits, cancellation, package ownership, installer arguments, and relaunch smoke passed"
} finally {
    [void][LightTableUpdaterSmoke.Native]::SetErrorMode($PreviousErrorMode)
    foreach ($StopFile in $StopFiles) { Set-Content -LiteralPath $StopFile -Value "stop" }
    foreach ($Process in $Processes) {
        if (-not $Process.HasExited -and -not $Process.WaitForExit(5000)) {
            # These are only the exact dummy/helper children this test created.
            $Process.Kill()
            [void]$Process.WaitForExit(5000)
        }
        $Process.Dispose()
    }
    Remove-Item -LiteralPath $Root -Recurse -Force
}
