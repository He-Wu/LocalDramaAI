[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI',
    [string]$ProjectRoot = '',
    [int]$StartupTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$python = Join-Path $Root 'env-musetalk\Scripts\python.exe'
$repository = Join-Path $Root 'MuseTalk'
$runDirectory = Join-Path $Root 'run'
$pidFile = Join-Path $runDirectory 'musetalk-service.pid'
$healthUrl = 'http://127.0.0.1:8030/health'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "MuseTalk Python is missing; run setup_musetalk.ps1 first: $python"
}
if (-not (Test-Path -LiteralPath (Join-Path $repository '.git'))) {
    throw "MuseTalk repository is missing; run setup_musetalk.ps1 first: $repository"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'ai_services\musetalk\service.py'))) {
    throw "LocalDramaAI MuseTalk service module is missing under $ProjectRoot"
}
$listener = Get-NetTCPConnection -State Listen -LocalPort 8030 -ErrorAction SilentlyContinue
if ($listener) {
    throw "Port 8030 is already listening (PID $($listener.OwningProcess -join ','))"
}

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction Stop
$ffmpegBin = Split-Path -Parent $ffmpeg.Source
$resolvedCommit = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve MuseTalk repository commit"
}

$env:LOCALDRAMA_MUSETALK_REPOSITORY = $repository
$env:LOCALDRAMA_MUSETALK_PYTHON = $python
$env:LOCALDRAMA_MUSETALK_FFMPEG_BIN = $ffmpegBin
$env:LOCALDRAMA_MUSETALK_REPO_COMMIT = $resolvedCommit
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null

$arguments = @(
    '-m', 'uvicorn',
    'ai_services.musetalk.service:app',
    '--app-dir', $ProjectRoot,
    '--host', '127.0.0.1',
    '--port', '8030',
    '--no-access-log'
)
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$temporaryPid = "$pidFile.tmp"
$process.Id | Set-Content -LiteralPath $temporaryPid -Encoding ascii
Move-Item -LiteralPath $temporaryPid -Destination $pidFile -Force

$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
try {
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            throw "MuseTalk service exited during startup with code $($process.ExitCode)"
        }
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 5
            if ($health.status -in @('ok', 'ready')) {
                Write-Host "MuseTalk service ready on http://127.0.0.1:8030 (PID $($process.Id))"
                return
            }
        }
        catch {
            # The service may still be importing; retry until the bounded deadline.
        }
        Start-Sleep -Milliseconds 500
    }
    throw "MuseTalk service did not become ready within $StartupTimeoutSeconds seconds"
}
catch {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw
}
