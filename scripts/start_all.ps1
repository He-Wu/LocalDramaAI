Start-Process powershell -ArgumentList '-NoProfile','-File', (Join-Path $PSScriptRoot 'start_api.ps1') -WindowStyle Hidden
Start-Process powershell -ArgumentList '-NoProfile','-File', (Join-Path $PSScriptRoot 'start_worker.ps1') -WindowStyle Hidden
& (Join-Path $PSScriptRoot 'start_ai_services.ps1')
