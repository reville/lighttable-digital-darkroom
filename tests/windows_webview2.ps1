# SPDX-License-Identifier: GPL-3.0-only
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '..\scripts\windows\ensure-webview2.ps1')

# Substitute only OS/network boundaries; exercise the real installation logic.
$script:State = @{}
function Reset-State {
    $script:State = @{
        Machine = $null; User = $null; Downloaded = $false; DownloadFailure = $false
        Signature = 'Valid'; Subject = 'CN=Microsoft Corporation, O=Microsoft Corporation'
        Launched = $false; ExitCode = 0; Completes = $true; Register = $true
        Killed = $false; Disposed = $false; Path = ''; Timeout = 0
    }
}
function Assert($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Get-WebView2RuntimeVersion {
    param($Hive)
    if ($Hive -eq [Microsoft.Win32.RegistryHive]::LocalMachine) { return $script:State.Machine }
    return $script:State.User
}
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    Assert ($Uri -eq 'https://go.microsoft.com/fwlink/p/?LinkId=2124703') 'Unexpected download source'
    Assert ($TimeoutSec -gt 0 -and $TimeoutSec -le 120) 'Unbounded download'
    $script:State.Downloaded = $true
    $script:State.Path = $OutFile
    if ($script:State.DownloadFailure) { throw 'Offline' }
    [IO.File]::WriteAllText($OutFile, 'test bootstrapper')
}
function Get-AuthenticodeSignature {
    param($LiteralPath)
    Assert (Test-Path -LiteralPath $LiteralPath) 'Signature checked before download'
    [pscustomobject]@{
        Status = $script:State.Signature
        SignerCertificate = [pscustomobject]@{ Subject = $script:State.Subject }
    }
}
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru)
    Assert (($ArgumentList -join ' ') -eq '/silent /install') 'Unexpected installer arguments'
    $script:State.Launched = $true
    $Process = [pscustomobject]@{ ExitCode = $script:State.ExitCode }
    $Process | Add-Member ScriptMethod WaitForExit {
        param($Timeout)
        $script:State.Timeout = $Timeout
        if ($script:State.Completes -and $script:State.Register) { $script:State.User = '140.0.1.0' }
        return $script:State.Completes
    }
    $Process | Add-Member ScriptMethod Kill { $script:State.Killed = $true }
    $Process | Add-Member ScriptMethod Dispose { $script:State.Disposed = $true }
    return $Process
}
function Expect-Failure([string]$Pattern) {
    $Failed = $false
    try { Install-WebView2Runtime } catch {
        $Failed = $true
        Assert ($_.Exception.Message -match $Pattern) "Wrong failure: $($_.Exception.Message)"
    }
    Assert $Failed 'Installation unexpectedly succeeded'
    Assert (-not (Test-Path -LiteralPath $script:State.Path)) 'Temporary bootstrapper was not cleaned'
}

foreach ($Scope in @('Machine', 'User')) {
    Reset-State
    $script:State[$Scope] = '140.0.1.0'
    Install-WebView2Runtime
    Assert (-not $script:State.Downloaded) 'An installed runtime should not require a network connection'
}
foreach ($Invalid in @($null, '', '0.0.0.0', 'invalid')) {
    Reset-State
    $script:State.Machine = $Invalid
    Assert (-not (Test-WebView2Runtime)) 'Invalid runtime version accepted'
}
Reset-State
$script:State.Machine = '0.0.0.0'
$script:State.User = '140.0.1.0'
Assert (Test-WebView2Runtime) 'Per-user runtime should override an empty machine install'

Reset-State
Install-WebView2Runtime
Assert $script:State.Launched 'Missing runtime was not installed'
Assert $script:State.Disposed 'Process handle was not disposed'
Assert ($script:State.Timeout -eq 300000) 'Unexpected process timeout'
Assert (-not (Test-Path -LiteralPath $script:State.Path)) 'Temporary bootstrapper was not cleaned'

Reset-State
$script:State.DownloadFailure = $true
Expect-Failure 'Offline'
Assert (-not $script:State.Launched) 'Ran after download failure'
foreach ($Signature in @('NotSigned', 'HashMismatch', 'NotTrusted')) {
    Reset-State
    $script:State.Signature = $Signature
    Expect-Failure 'valid Microsoft signature'
    Assert (-not $script:State.Launched) 'Ran an untrusted download'
}
Reset-State
$script:State.Subject = 'CN=Another Publisher, O=Microsoft Corporation'
Expect-Failure 'valid Microsoft signature'
Assert (-not $script:State.Launched) 'Ran a different publisher download'

Reset-State
$script:State.ExitCode = 1
Expect-Failure 'exit code 1'
Reset-State
$script:State.Register = $false
Expect-Failure 'still unavailable'
Reset-State
$script:State.Completes = $false
Expect-Failure 'timed out'
Assert $script:State.Killed 'Timed-out bootstrapper was not stopped'
Assert $script:State.Disposed 'Timed-out process handle was not disposed'
Write-Output 'WebView2 runtime behavior checks passed'

# An explicit bundled standalone installer never accesses the network, even
# when its file or signature is invalid. The original bundle remains untouched.
$Offline = Join-Path ([IO.Path]::GetTempPath()) ('lighttable-offline-test-' + [Guid]::NewGuid() + '.exe')
try {
    [IO.File]::WriteAllText($Offline, 'test standalone installer')
    Reset-State
    $script:State.DownloadFailure = $true
    Install-WebView2Runtime -OfflineInstaller $Offline
    Assert $script:State.Launched 'Bundled runtime was not installed'
    Assert (-not $script:State.Downloaded) 'Offline installation accessed the network'
    Assert (Test-Path -LiteralPath $Offline) 'Offline bundle input was removed'
    foreach ($Case in @('missing', 'untrusted')) {
        Reset-State
        $script:State.DownloadFailure = $true
        $Candidate = $Offline
        if ($Case -eq 'missing') { $Candidate += '.missing' }
        else { $script:State.Signature = 'NotSigned' }
        $Failed = $false
        try { Install-WebView2Runtime -OfflineInstaller $Candidate } catch { $Failed = $true }
        Assert $Failed 'Invalid offline installer was accepted'
        Assert (-not $script:State.Downloaded) 'Offline failure fell back to a download'
        Assert (-not $script:State.Launched) 'Invalid offline installer was launched'
    }
} finally {
    Remove-Item -LiteralPath $Offline -Force
}
Write-Output 'Offline WebView2 behavior checks passed'
