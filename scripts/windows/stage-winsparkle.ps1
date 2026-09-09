param(
    [Parameter(Mandatory = $true)][string]$Sdk,
    [Parameter(Mandatory = $true)][string]$Payload
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# These paths are the layout of the hash-pinned upstream binary SDK. Validate
# everything before modifying the payload, including the release signing tool.
$Dll = Join-Path $Sdk "x64/Release/WinSparkle.dll"
$Tool = Join-Path $Sdk "bin/winsparkle-tool.exe"
$Notices = @("COPYING", "COPYING.expat")
foreach ($Required in @($Dll, $Tool) + @($Notices | ForEach-Object { Join-Path $Sdk $_ })) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf) -or (Get-Item -LiteralPath $Required).Length -eq 0) {
        throw "The pinned WinSparkle SDK is incomplete: $Required"
    }
}
$Licenses = Join-Path $Payload "Resources/LightTable/licenses/winsparkle"
New-Item -ItemType Directory -Force -Path $Payload, $Licenses | Out-Null
Copy-Item -LiteralPath $Dll -Destination (Join-Path $Payload "WinSparkle.dll")
foreach ($Notice in $Notices) {
    Copy-Item -LiteralPath (Join-Path $Sdk $Notice) -Destination $Licenses
}
