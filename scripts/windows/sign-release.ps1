param(
    [string[]]$Files = @(),
    [switch]$RequireSigning,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$HasCertificate = -not [string]::IsNullOrWhiteSpace($env:WINDOWS_CERTIFICATE_BASE64)
$HasPassword = -not [string]::IsNullOrWhiteSpace($env:WINDOWS_CERTIFICATE_PASSWORD)
if (-not $HasCertificate -and -not $HasPassword -and -not $RequireSigning) {
    if ($CheckOnly) { return $false }
    Write-Host "Authenticode is not configured; this is an unsigned CI build."
    return
}
if (-not $HasCertificate -or -not $HasPassword) {
    throw "Windows signing requires both WINDOWS_CERTIFICATE_BASE64 and WINDOWS_CERTIFICATE_PASSWORD."
}
if ($env:OS -ne "Windows_NT") { throw "Authenticode signing requires Windows and the Windows SDK." }

$SignTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
if (-not $SignTool) {
    $SdkBin = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $Candidate = Get-ChildItem -Path (Join-Path $SdkBin "*\x64\signtool.exe") -File -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
        Select-Object -First 1
    if ($Candidate) { $SignTool = Get-Command $Candidate.FullName }
}
if (-not $SignTool) { throw "Windows signing requires signtool.exe from the installed Windows SDK." }

$SigningDirectory = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-signing-" + [Guid]::NewGuid())
$CertificatePath = Join-Path $SigningDirectory "certificate.pfx"
$Certificate = $null
try {
    New-Item -ItemType Directory -Path $SigningDirectory | Out-Null
    # Keep the decoded key in the runner's user-private temporary directory;
    # neither its contents nor the password are written to build logs.
    try {
        [IO.File]::WriteAllBytes($CertificatePath, [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE_BASE64))
        $Certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
            $CertificatePath, $env:WINDOWS_CERTIFICATE_PASSWORD,
            [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
        )
    } catch {
        throw "The Windows signing certificate could not be decoded or opened."
    }
    if (-not $Certificate.HasPrivateKey) { throw "The Windows signing certificate has no private key." }
    if ($Certificate.NotBefore -gt (Get-Date) -or $Certificate.NotAfter -le (Get-Date)) {
        throw "The Windows signing certificate is not currently valid."
    }
    if ($CheckOnly) { return $true }
    if ($Files.Count -eq 0) { throw "At least one executable is required for Authenticode signing." }

    foreach ($File in $Files) {
        $Executable = (Resolve-Path -LiteralPath $File).Path
        if ([IO.Path]::GetExtension($Executable).ToLowerInvariant() -notin @(".exe", ".dll")) {
            throw "The Windows release signing list must contain only executables or DLLs."
        }
        # Microsoft's SignTool syntax uses /fd for the file digest and /td with
        # /tr for an RFC 3161 timestamp. Pass arguments directly; never echo them.
        # https://learn.microsoft.com/windows/win32/seccrypto/signtool
        & $SignTool.Source sign /q /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
            /f $CertificatePath /p $env:WINDOWS_CERTIFICATE_PASSWORD $Executable
        if ($LASTEXITCODE -ne 0) { throw "Windows executable signing or timestamping failed." }
        & $SignTool.Source verify /q /pa /all /tw $Executable
        if ($LASTEXITCODE -ne 0) { throw "Windows executable Authenticode verification failed." }
        Write-Host "Verified Authenticode signature: $([IO.Path]::GetFileName($Executable))"
    }
} finally {
    if ($Certificate) { $Certificate.Dispose() }
    if (Test-Path -LiteralPath $SigningDirectory) {
        Remove-Item -LiteralPath $SigningDirectory -Recurse -Force
    }
}
