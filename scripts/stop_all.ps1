[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Stop-ProcessIfRunning {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-ProcessTreeIds {
    param(
        [Parameter(Mandatory = $true)][object[]]$Processes,
        [Parameter(Mandatory = $true)][int]$RootProcessId
    )

    $ordered = [System.Collections.Generic.List[int]]::new()
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    [void]$seen.Add($RootProcessId)
    $queue.Enqueue($RootProcessId)
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        [void]$ordered.Add($parentId)
        foreach ($process in $Processes) {
            $childId = [int]$process.ProcessId
            if ([int]$process.ParentProcessId -eq $parentId -and $seen.Add($childId)) {
                $queue.Enqueue($childId)
            }
        }
    }
    $result = @($ordered)
    [Array]::Reverse($result)
    return $result
}

$processes = @(Get-CimInstance Win32_Process)
$applicationPatterns = @(
    'uvicorn app.main:app',
    'app.worker_main',
    'uvicorn ai_services.qwen3_tts.service:app'
)
$applicationProcesses = @($processes | Where-Object {
    $commandLine = [string]$_.CommandLine
    $commandLine -and ($applicationPatterns | Where-Object {
        $commandLine -match [regex]::Escape($_)
    })
})
foreach ($process in ($applicationProcesses | Sort-Object ProcessId -Unique)) {
    Stop-ProcessIfRunning -ProcessId ([int]$process.ProcessId)
}

$pidFile = Join-Path $Root 'run\musetalk-service.pid'
if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    return
}

try {
    $rawPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $serviceProcessId = 0
    if (-not [int]::TryParse($rawPid, [ref]$serviceProcessId) -or $serviceProcessId -le 0) {
        Write-Warning "Ignoring invalid MuseTalk service PID file: $pidFile"
        return
    }

    $serviceProcess = @($processes | Where-Object {
        [int]$_.ProcessId -eq $serviceProcessId
    } | Select-Object -First 1)
    if ($serviceProcess.Count -eq 0) {
        return
    }

    $expectedExecutable = [System.IO.Path]::GetFullPath(
        (Join-Path $Root 'env-musetalk\Scripts\python.exe')
    )
    $actualExecutable = [string]$serviceProcess[0].ExecutablePath
    $commandLine = [string]$serviceProcess[0].CommandLine
    $executableMatches = $actualExecutable -and
        $actualExecutable.Equals($expectedExecutable, [System.StringComparison]::OrdinalIgnoreCase)
    $commandMatches = $commandLine -and
        $commandLine -match '(?i)(?:^|\s)-m\s+uvicorn(?:\s|$)' -and
        $commandLine -match '(?i)(?:^|\s)ai_services\.musetalk\.service:app(?:\s|$)'
    if (-not $executableMatches -or -not $commandMatches) {
        Write-Warning "MuseTalk PID $serviceProcessId does not identify the managed env-musetalk Uvicorn service; preserving it"
        return
    }

    $treeIds = Get-ProcessTreeIds -Processes $processes -RootProcessId $serviceProcessId
    foreach ($processIdToStop in $treeIds) {
        Stop-ProcessIfRunning -ProcessId $processIdToStop
    }
}
finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
