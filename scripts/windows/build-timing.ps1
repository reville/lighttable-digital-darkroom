# Stage names and durations only: never record commands, environment, or errors.
function New-BuildTiming {
    param([string]$Path, [string]$Version)
    return @{
        Path = $Path; Version = $Version; StartedAt = [DateTime]::UtcNow.ToString('o')
        Clock = [Diagnostics.Stopwatch]::StartNew()
        Stages = [Collections.Generic.List[object]]::new(); Active = $null
    }
}

function Stop-BuildStage {
    param([hashtable]$Timing, [bool]$Succeeded = $true)
    if ($null -eq $Timing.Active) { return }
    $Timing.Active.Clock.Stop()
    $Timing.Stages.Add([ordered]@{
        name = $Timing.Active.Name; started_at = $Timing.Active.StartedAt
        elapsed_seconds = [Math]::Round($Timing.Active.Clock.Elapsed.TotalSeconds, 3)
        status = $(if ($Succeeded) { 'success' } else { 'failed' })
    })
    Write-Host ("Release stage {0}: {1:N1}s ({2})" -f $Timing.Active.Name,
        $Timing.Active.Clock.Elapsed.TotalSeconds, $Timing.Stages[-1].status)
    $Timing.Active = $null
}

function Start-BuildStage {
    param([hashtable]$Timing, [ValidatePattern('^[a-z][a-z0-9-]*$')][string]$Name)
    Stop-BuildStage $Timing
    $Timing.Active = @{
        Name = $Name; StartedAt = [DateTime]::UtcNow.ToString('o')
        Clock = [Diagnostics.Stopwatch]::StartNew()
    }
    Write-Host "Release stage: $Name"
}

function Complete-BuildTiming {
    param([hashtable]$Timing, [bool]$Succeeded)
    Stop-BuildStage $Timing $Succeeded
    $Timing.Clock.Stop()
    try {
        $Directory = Split-Path -Parent $Timing.Path
        if ($Directory) { New-Item -ItemType Directory -Force -Path $Directory | Out-Null }
        [ordered]@{
            schema_version = 1; version = $Timing.Version; started_at = $Timing.StartedAt
            elapsed_seconds = [Math]::Round($Timing.Clock.Elapsed.TotalSeconds, 3)
            status = $(if ($Succeeded) { 'success' } else { 'failed' })
            stages = @($Timing.Stages.ToArray())
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Timing.Path -Encoding utf8
        if ($env:GITHUB_STEP_SUMMARY) {
            $Lines = @('### Windows build timings', '', '| Stage | Seconds | Result |', '| --- | ---: | --- |')
            foreach ($Stage in $Timing.Stages) {
                $Lines += "| $($Stage.name) | $($Stage.elapsed_seconds) | $($Stage.status) |"
            }
            $Lines += "`nTotal: $([Math]::Round($Timing.Clock.Elapsed.TotalSeconds, 1)) seconds."
            $Lines | Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Encoding utf8
        }
    } catch {
        # Timing diagnostics must not mask the build's original failure or result.
        Write-Warning 'Could not persist Windows build timing diagnostics.'
    }
}
