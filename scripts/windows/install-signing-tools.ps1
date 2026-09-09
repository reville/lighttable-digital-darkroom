# Pinned Microsoft NuGet packages. These tools never contain signing credentials.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "The signing tools require Windows." }
$Destination = Join-Path $env:RUNNER_TEMP "lighttable-signing-tools"
$Packages = @(
    @{
        Name = "microsoft.artifactsigning.client"; Version = "1.0.128"; Folder = "client"
        Sha256 = "74bd7d27e6ce1051409c38d9b46bc8df0400ecd643d51ffbf2ac00869061e40b"
    },
    @{
        Name = "microsoft.windows.sdk.buildtools"; Version = "10.0.26100.8249"; Folder = "sdk"
        Sha256 = "1628c77d21ed187c4db998b37b18e267a7f092ae755589e21110c14260b14960"
    }
)
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
foreach ($Package in $Packages) {
    $Archive = Join-Path $Destination "$($Package.Folder).zip"
    $Uri = "https://api.nuget.org/v3-flatcontainer/$($Package.Name)/$($Package.Version)/$($Package.Name).$($Package.Version).nupkg"
    Invoke-WebRequest -Uri $Uri -OutFile $Archive
    if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Package.Sha256) {
        throw "Signing dependency checksum mismatch: $($Package.Name)."
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath (Join-Path $Destination $Package.Folder) -Force
    Remove-Item -LiteralPath $Archive -Force
}
$SignTool = Join-Path $Destination "sdk/bin/10.0.26100.0/x64/signtool.exe"
$Dlib = Join-Path $Destination "client/bin/x64/Azure.CodeSigning.Dlib.dll"
foreach ($File in @($SignTool, $Dlib)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Missing signing dependency: $File" }
}
"AZURE_SIGNING_SIGNTOOL=$SignTool" | Out-File -LiteralPath $env:GITHUB_ENV -Append -Encoding utf8
"AZURE_SIGNING_DLIB=$Dlib" | Out-File -LiteralPath $env:GITHUB_ENV -Append -Encoding utf8
