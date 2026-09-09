$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = 'C:\OEM'
$Evidence = Join-Path $Root 'evidence'
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Adapters = @()
$HostReport = @{ok = $false; bootstrap_error = $null; evaluation_vm = $true}
try {
    & icacls.exe $Root /grant ($env:USERNAME + ':(OI)(CI)M') /T /Q | Out-Null
    $OS = Get-CimInstance Win32_OperatingSystem
    $Config = Get-Content (Join-Path $Root 'candidate.json') -Raw | ConvertFrom-Json
    $HostReport.caption = $OS.Caption
    $HostReport.build = $OS.BuildNumber
    $HostReport.product_type = $OS.ProductType
    $HostReport.architecture = $env:PROCESSOR_ARCHITECTURE
    $HostReport.expected_windows = $Config.windows
    $HostReport.graphics = @(Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion)
    $HostReport.uac_enabled = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System').EnableLUA
    if ($OS.ProductType -ne 1 -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Expected an x64 Windows client' }
    if (($Config.windows -eq '11') -ne ([int]$OS.BuildNumber -ge 22000)) { throw 'Windows client version mismatch' }
    . (Join-Path $Root 'scripts/ensure-webview2.ps1')
    $HostReport.webview2_initially_present = Test-WebView2Runtime
    # Use the vendor uninstaller only. Never fake absence by deleting registry keys.
    if ($HostReport.webview2_initially_present) {
        $Setups = @(Get-ChildItem "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application\*\Installer\setup.exe" -ErrorAction SilentlyContinue)
        foreach ($Setup in $Setups) {
            $Process = Start-Process $Setup.FullName -ArgumentList '--uninstall --msedgewebview --system-level --force-uninstall' -PassThru
            if (-not $Process.WaitForExit(120000)) { $Process.Kill(); throw 'WebView2 removal timed out' }
        }
    }
    $HostReport.webview2_absent_before_offline_install = -not (Test-WebView2Runtime)
    # Verify the original file before disconnecting; record this trust-cache warmup.
    $Installer = Join-Path $Root $Config.installer
    if ((Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Config.installer_sha256) { throw 'Installer hash mismatch' }
    $Signature = Get-AuthenticodeSignature $Installer
    $HostReport.installer_signature_before_disconnect = $Signature.Status.ToString()
    $HostReport.certificate_chain_checked_online_before_test = $true
    if ($Signature.Status -ne 'Valid') { throw 'Installer signature is not trusted on the fresh client' }
    $Adapters = @(Get-NetAdapter | Where-Object Status -eq 'Up')
    $Adapters | Disable-NetAdapter -Confirm:$false
    $HostReport.network_adapters_disabled = $true
    # FirstLogonCommands is elevated. Schedule the test in the logged-on user's
    # limited token so per-user installation is tested without administrator rights.
    $Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\OEM\guest-test.ps1'
    $Principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName 'LightTableClientAcceptance' -Action $Action -Principal $Principal -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'LightTableClientAcceptance'
    $Deadline = (Get-Date).AddMinutes(20)
    while ((Get-Date) -lt $Deadline -and -not (Test-Path (Join-Path $Root 'test.done'))) { Start-Sleep -Seconds 5 }
    if (-not (Test-Path (Join-Path $Root 'test.done'))) { throw 'Client acceptance exceeded twenty minutes' }
    $HostReport.ok = $true
} catch {
    $HostReport.bootstrap_error = $_.Exception.Message
} finally {
    foreach ($Adapter in $Adapters) { $Adapter | Enable-NetAdapter -Confirm:$false -ErrorAction Continue }
    Stop-ScheduledTask -TaskName 'LightTableClientAcceptance' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'LightTableClientAcceptance' -Confirm:$false -ErrorAction SilentlyContinue
    $HostReport | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $Evidence 'host.json') -Encoding utf8
    # The receiver is private to the VM container, with no published port.
    # Upload bounded test evidence only, never the Windows image or account file.
    for ($Attempt = 0; $Attempt -lt 12; $Attempt++) {
        try {
            foreach ($File in Get-ChildItem $Evidence -Recurse -File) {
                if ($File.Extension -notin @('.json', '.png', '.log', '.tif') -or $File.Length -gt 16MB) { continue }
                $Relative = $File.FullName.Substring($Evidence.Length + 1).Replace('\', '/')
                Invoke-WebRequest -UseBasicParsing -Method Put -Uri ('http://10.0.2.2:18080/' + $Relative) -InFile $File.FullName -TimeoutSec 20 | Out-Null
            }
            Invoke-WebRequest -UseBasicParsing -Method Put -Uri 'http://10.0.2.2:18080/complete' -Body 'done' -TimeoutSec 10 | Out-Null
            break
        } catch { Start-Sleep -Seconds 5 }
    }
    shutdown.exe /s /t 10
}
