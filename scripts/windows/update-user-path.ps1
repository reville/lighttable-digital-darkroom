# SPDX-License-Identifier: GPL-3.0-only
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Add", "Remove")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$BinPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Read the unexpanded value and preserve its registry type. Expanding %variables%
# here would silently change unrelated tools' PATH entries.
$Key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Environment")
try {
    $Existing = $Key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $Kind = if ($null -eq $Existing) {
        [Microsoft.Win32.RegistryValueKind]::ExpandString
    } else {
        $Key.GetValueKind("Path")
    }
    $Normalized = [IO.Path]::GetFullPath($BinPath).TrimEnd("\")
    $Entries = if ($null -eq $Existing) { @() } else { @(([string]$Existing).Split(";")) }
    $Matches = @($Entries | Where-Object {
        $_.Trim().Trim('"').TrimEnd("\") -ieq $Normalized
    })
    if ($Action -eq "Add") {
        if ($Matches.Count -gt 0) { exit 0 }
        $Updated = if ([string]::IsNullOrEmpty($Existing)) { $Normalized } else { "$Existing;$Normalized" }
    } else {
        if ($Matches.Count -eq 0) { exit 0 }
        $Updated = (@($Entries | Where-Object {
            $_.Trim().Trim('"').TrimEnd("\") -ine $Normalized
        }) -join ";")
    }
    if ($Action -eq "Remove" -and $Updated -eq "") {
        $Key.DeleteValue("Path", $false)
    } else {
        $Key.SetValue("Path", $Updated, $Kind)
    }
} finally {
    $Key.Dispose()
}
