param(
    [Parameter(Mandatory = $true)]
    [string]$Application
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Exercise the real native helper without replacing an installed application,
# opening a window, running an actual installer, or touching a user catalog.
$Compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $Compiler)) { throw "The updater smoke test requires the .NET Framework C# compiler" }
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
        Copy-Item -LiteralPath $Application -Destination $Helper
        Copy-Item -LiteralPath $Fixture -Destination $Installer
        Copy-Item -LiteralPath $Fixture -Destination $Target
        $Owner = if ($Scenario -eq "portable") { "portable" } else { "direct" }
        Set-Content -LiteralPath (Join-Path $Install "install-channel.txt") -Encoding ascii -NoNewline $Owner
        Set-Content -LiteralPath (Join-Path $Install "build-manifest.json") -Encoding ascii -NoNewline '{"version":"1.2.3","authenticode_signed":true}'
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
        $Update = Start-Process -FilePath $Helper -ArgumentList $HelperArguments -PassThru -WindowStyle Hidden
        $Processes.Add($Update)
        if ($Scenario -eq "portable") {
            if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -eq 0 -or (Test-Path $Ready)) {
                throw "The native helper accepted a portable installation"
            }
        } else {
            $Deadline = (Get-Date).AddSeconds(10)
            while (-not (Test-Path $Ready) -and (Get-Date) -lt $Deadline -and -not $Update.HasExited) { Start-Sleep -Milliseconds 20 }
            if (-not (Test-Path $Ready)) { throw "The native helper did not open both process handles" }
            if ($Scenario -eq "cancel") {
                Set-Content -LiteralPath (Join-Path $Stage "cancelled") -Value "cancelled"
                if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -eq 0) { throw "The update helper ignored cancellation" }
                if ($Gui.HasExited -or $Server.HasExited) { throw "Cancelling an update terminated an application process" }
            } else {
                Set-Content -LiteralPath (Join-Path $Stage "authorized") -Value "saved and closed"
                Set-Content -LiteralPath $GuiStop -Value "stop"
                if (-not $Gui.WaitForExit(5000)) { throw "The GUI fixture did not close" }
                Start-Sleep -Milliseconds 100
                if (Test-Path (Join-Path $Stage "installed.txt")) { throw "Installation started while the render server was running" }
                Set-Content -LiteralPath $ServerStop -Value "stop"
                if (-not $Update.WaitForExit(10000) -or $Update.ExitCode -ne 0) { throw "The native helper did not complete the update" }
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
