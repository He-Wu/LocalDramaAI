$ErrorActionPreference = 'Stop'
$patterns = @(
    'uvicorn app.main:app',
    'app.worker_main',
    'uvicorn ai_services.qwen3_tts.service:app',
    'uvicorn ai_services.musetalk.service:app',
    'scripts.inference'
)
$processes = Get-CimInstance Win32_Process | Where-Object {
    $commandLine = $_.CommandLine
    $commandLine -and ($patterns | Where-Object { $commandLine -match [regex]::Escape($_) })
}
$processes | Sort-Object ProcessId -Unique | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
}
$pidFile = 'E:\LocalDramaAI\run\musetalk-service.pid'
if (Test-Path -LiteralPath $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force
}
