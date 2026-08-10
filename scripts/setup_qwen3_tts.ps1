$ErrorActionPreference = 'Stop'
$root = 'E:\LocalDramaAI'
$python = "$root\env-comfyui\Scripts\python.exe"
$envTts = "$root\env-tts"
if (-not (Test-Path "$envTts\Scripts\python.exe")) {
    & $python -m venv --system-site-packages $envTts
}
$ttsPython = "$envTts\Scripts\python.exe"
& $ttsPython -m pip install --upgrade pip
& $ttsPython -m pip install --no-deps qwen-tts==0.1.1
& $ttsPython -m pip install -r (Join-Path $PSScriptRoot '..\ai_services\qwen3_tts\requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Qwen3-TTS runtime installation failed.' }
$comfySite = "$root\env-comfyui\Lib\site-packages"
$ttsSite = "$envTts\Lib\site-packages"
$torchCuda = (& $ttsPython -c "import torch; print(torch.version.cuda or '')").Trim()
if (-not $torchCuda) {
    & $ttsPython -m pip uninstall -y torch torchaudio
    foreach ($name in @('torch', 'torch-2.13.0+cu126.dist-info', 'torchaudio', 'torchaudio-2.11.0+cu126.dist-info', 'torchgen', 'functorch')) {
        $link = Join-Path $ttsSite $name
        if (-not (Test-Path $link)) { New-Item -ItemType Junction -Path $link -Target (Join-Path $comfySite $name) | Out-Null }
    }
}
& $ttsPython -c "import torch, torchaudio; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda, torchaudio.__version__)"
Write-Host "Qwen3-TTS environment ready: $envTts"
