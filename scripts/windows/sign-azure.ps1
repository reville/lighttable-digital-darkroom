param([string[]]$Files = @(), [switch]$CheckOnly)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Endpoint = $null
if (-not [Uri]::TryCreate($env:AZURE_SIGNING_ENDPOINT, [UriKind]::Absolute, [ref]$Endpoint) -or
    $Endpoint.Scheme -ne "https" -or $Endpoint.Host -notmatch '^[a-z0-9-]+\.codesigning\.azure\.net$' -or
    $Endpoint.UserInfo -or $Endpoint.Query -or $Endpoint.Fragment -or $Endpoint.AbsolutePath -ne "/") {
    throw "Azure Artifact Signing requires an HTTPS regional codesigning.azure.net endpoint."
}
foreach ($Setting in @("AZURE_TENANT_ID", "AZURE_CLIENT_ID")) {
    $Identifier = [Guid]::Empty
    if (-not [Guid]::TryParse([Environment]::GetEnvironmentVariable($Setting), [ref]$Identifier)) {
        throw "Azure Artifact Signing requires a valid $Setting GUID."
    }
}
if ($env:GITHUB_ACTIONS -ne "true" -or $env:GITHUB_REPOSITORY -ne "reville/lighttable-digital-darkroom" -or
    $env:GITHUB_EVENT_NAME -eq "pull_request" -or
    ($env:GITHUB_REF -ne "refs/heads/main" -and $env:GITHUB_REF -notmatch '^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$')) {
    throw "Azure signing is restricted to this repository's main branch and version tags in GitHub Actions."
}
foreach ($Setting in @("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Setting))) {
        throw "Azure signing requires GitHub OIDC with id-token: write."
    }
}
if ($env:OS -ne "Windows_NT") { throw "Azure Artifact Signing requires Windows." }
foreach ($Setting in @("AZURE_SIGNING_SIGNTOOL", "AZURE_SIGNING_DLIB")) {
    $Dependency = [Environment]::GetEnvironmentVariable($Setting)
    if ([string]::IsNullOrWhiteSpace($Dependency) -or -not (Test-Path -LiteralPath $Dependency -PathType Leaf)) {
        throw "Run install-signing-tools.ps1 before Azure signing; missing $Setting."
    }
}
if ($CheckOnly) { return $true }
if ($Files.Count -eq 0) { throw "At least one executable is required for Authenticode signing." }
$Executables = @(foreach ($File in $Files) {
    $Executable = (Resolve-Path -LiteralPath $File).Path
    if ([IO.Path]::GetExtension($Executable).ToLowerInvariant() -notin @(".exe", ".dll", ".pyd")) {
        throw "The Windows release signing list must contain only executables, DLLs, or Python native modules."
    }
    $Executable
})
$SigningDirectory = Join-Path ([IO.Path]::GetTempPath()) ("lighttable-signing-" + [Guid]::NewGuid())
$PreviousTokenFile = $env:AZURE_FEDERATED_TOKEN_FILE
try {
    New-Item -ItemType Directory -Path $SigningDirectory | Out-Null
    # Fetch a fresh GitHub assertion at each signing stage, after compilation.
    # Azure.Identity exchanges it using WorkloadIdentityCredential; no client secret
    # or Azure private signing key is stored in GitHub or on the build runner.
    try {
        $Response = Invoke-RestMethod -Uri ($env:ACTIONS_ID_TOKEN_REQUEST_URL + "&audience=api://AzureADTokenExchange") `
            -Headers @{ Authorization = "bearer $env:ACTIONS_ID_TOKEN_REQUEST_TOKEN" } -TimeoutSec 30
        if ([string]::IsNullOrWhiteSpace($Response.value)) { throw "Missing assertion" }
        $env:AZURE_FEDERATED_TOKEN_FILE = Join-Path $SigningDirectory "oidc.jwt"
        [IO.File]::WriteAllText($env:AZURE_FEDERATED_TOKEN_FILE, $Response.value)
    } catch {
        # Do not echo HTTP diagnostics, which can include credential material.
        throw "Could not obtain the GitHub OIDC assertion for Azure signing."
    }
    $MetadataPath = Join-Path $SigningDirectory "metadata.json"
    @{
        Endpoint = $env:AZURE_SIGNING_ENDPOINT
        CodeSigningAccountName = $env:AZURE_SIGNING_ACCOUNT
        CertificateProfileName = $env:AZURE_SIGNING_PROFILE
        CorrelationId = "$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"
        ExcludeCredentials = @(
            "EnvironmentCredential", "ManagedIdentityCredential", "SharedTokenCacheCredential",
            "VisualStudioCredential", "VisualStudioCodeCredential", "AzureCliCredential",
            "AzurePowerShellCredential", "AzureDeveloperCliCredential", "InteractiveBrowserCredential"
        )
    } | ConvertTo-Json | Set-Content -LiteralPath $MetadataPath -Encoding utf8
    foreach ($Executable in $Executables) {
        # The Microsoft client can include HTTP response headers in failure
        # diagnostics. Keep its output private and expose only an AADSTS code.
        $SignLog = Join-Path $SigningDirectory "signing.log"
        & $env:AZURE_SIGNING_SIGNTOOL sign /q /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 `
            /dlib $env:AZURE_SIGNING_DLIB /dmdf $MetadataPath $Executable *> $SignLog
        if ($LASTEXITCODE -ne 0) {
            $FailureCode = [regex]::Match((Get-Content -LiteralPath $SignLog -Raw), 'AADSTS[0-9]+').Value
            throw "Azure signing or timestamping failed. $FailureCode Check the federated identity and certificate-profile signer role."
        }
        & $env:AZURE_SIGNING_SIGNTOOL verify /q /pa /all /tw $Executable
        if ($LASTEXITCODE -ne 0) { throw "Windows executable Authenticode verification failed." }
        $Signature = Get-AuthenticodeSignature -LiteralPath $Executable
        if ($Signature.Status -ne "Valid" -or $null -eq $Signature.TimeStamperCertificate) {
            throw "Azure signing requires a valid, timestamped Authenticode signature."
        }
        Write-Host "Verified Authenticode signature: $([IO.Path]::GetFileName($Executable)); publisher: $($Signature.SignerCertificate.Subject)"
    }
} finally {
    $env:AZURE_FEDERATED_TOKEN_FILE = $PreviousTokenFile
    $Response = $null
    if (Test-Path -LiteralPath $SigningDirectory) { Remove-Item -LiteralPath $SigningDirectory -Recurse -Force }
}
