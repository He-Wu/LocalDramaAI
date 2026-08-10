$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envTtsPython = 'E:\LocalDramaAI\env-tts\Scripts\python.exe'
$env:PYTHONPATH = $root
$env:LOCALDRAMA_QWEN3_TTS_MODEL = 'E:\LocalDramaAI\Models\Qwen3-TTS-12Hz-0.6B-Base'
& $envTtsPython -m uvicorn ai_services.qwen3_tts.service:app --host 127.0.0.1 --port 8020 --log-level info
