$ErrorActionPreference = 'Stop'
Start-Process powershell -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start_qwen3_tts.ps1') -WindowStyle Hidden
Write-Host 'Qwen3-TTS service launch requested on http://127.0.0.1:8020'
Start-Process powershell -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start_musetalk.ps1') -WindowStyle Hidden
Write-Host 'MuseTalk service launch requested on http://127.0.0.1:8030'
