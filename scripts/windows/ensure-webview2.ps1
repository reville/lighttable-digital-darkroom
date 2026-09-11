# SPDX-License-Identifier: GPL-3.0-only
param([switch]$CheckOnly, [string]$OfflineInstaller = "")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Microsoft documents both registry locations and the per-user bootstrapper:
# https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution
function Get-WebView2RuntimeVersion([Microsoft.Win32.RegistryHive]$Hive) {
    # NSIS may launch 32-bit PowerShell on a 64-bit OS. Explicitly select the
    # 32-bit registry view instead of relying on process-dependent redirection.
    $Root = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, [Microsoft.Win32.RegistryView]::Registry32)
    $Key = $null
    try {
        $Key = $Root.OpenSubKey('SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
        if ($null -ne $Key) { return $Key.GetValue('pv') }
        return $null
    } finally {
        if ($null -ne $Key) { $Key.Dispose() }
        $Root.Dispose()
    }
}

function Test-WebView2Runtime {
    foreach ($Hive in @(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryHive]::CurrentUser
    )) {
        $Value = Get-WebView2RuntimeVersion $Hive
        $Version = $null
        if ([Version]::TryParse([string]$Value, [ref]$Version) -and
            $Version -gt [Version]'0.0.0.0') {
            return $true
        }
    }
    return $false
}

function Install-WebView2Runtime([string]$OfflineInstaller = "") {
    if (Test-WebView2Runtime) {
        Write-Host "Microsoft Edge WebView2 Runtime is already installed."
        return
    }

    $Temporary = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-webview2-" + [Guid]::NewGuid())
    $Bootstrapper = Join-Path $Temporary "MicrosoftEdgeWebview2Setup.exe"
    New-Item -ItemType Directory -Path $Temporary | Out-Null
    try {
        Write-Host "Installing Microsoft Edge WebView2 Runtime..."
        if (-not [string]::IsNullOrWhiteSpace($OfflineInstaller)) {
            # An explicit offline package must never fall back to the network.
            if (-not (Test-Path -LiteralPath $OfflineInstaller -PathType Leaf)) {
                throw "The bundled offline WebView2 installer is missing."
            }
            Copy-Item -LiteralPath $OfflineInstaller -Destination $Bootstrapper
        } else {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 `
                -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $Bootstrapper
        }
        $Signature = Get-AuthenticodeSignature -LiteralPath $Bootstrapper
        if ($Signature.Status -ne 'Valid' -or $null -eq $Signature.SignerCertificate -or
            $Signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
            throw "The WebView2 download does not have a valid Microsoft signature."
        }

        # The installer is unelevated, matching LightTable's per-user install.
        $Process = Start-Process -FilePath $Bootstrapper -ArgumentList '/silent', '/install' `
            -WindowStyle Hidden -PassThru
        try {
            if (-not $Process.WaitForExit(300000)) {
                $Process.Kill()
                throw "WebView2 installation timed out after five minutes."
            }
            if ($Process.ExitCode -ne 0) {
                throw "WebView2 installation failed (exit code $($Process.ExitCode))."
            }
        } finally {
            $Process.Dispose()
        }
        if (-not (Test-WebView2Runtime)) {
            throw "WebView2 installation finished, but the Runtime is still unavailable."
        }
        Write-Host "Microsoft Edge WebView2 Runtime is ready."
    } finally {
        Remove-Item -LiteralPath $Temporary -Recurse -Force
    }
}

# Dot-sourcing exposes the functions to the behavior tests without installing.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($CheckOnly) {
            if (Test-WebView2Runtime) { exit 0 }
            exit 1
        }
        Install-WebView2Runtime -OfflineInstaller $OfflineInstaller
    } catch {
        [Console]::Error.WriteLine("$($_.Exception.Message) Install Microsoft Edge WebView2 Runtime from https://developer.microsoft.com/microsoft-edge/webview2 and run LightTable setup again.")
        exit 1
    }
}
