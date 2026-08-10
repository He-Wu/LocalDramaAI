[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI',
    [string]$ExpectedMuseTalkCommit = '0a89dec45a0192b824e3cf4daf96c239440c5ed8'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Assert-File {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -le 0) {
        throw "Required file is empty: $Path"
    }
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

$repository = Join-Path $Root 'MuseTalk'
$python = Join-Path $Root 'env-musetalk\Scripts\python.exe'
Assert-File -Path $python
if (-not (Test-Path -LiteralPath (Join-Path $repository '.git'))) {
    throw "MuseTalk Git checkout is missing: $repository"
}

$commit = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -ne $ExpectedMuseTalkCommit) {
    throw "MuseTalk commit mismatch: expected $ExpectedMuseTalkCommit, got $commit"
}
$pythonVersion = (& $python -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0 -or -not $pythonVersion.StartsWith('3.10.')) {
    throw "env-musetalk must use Python 3.10, got $pythonVersion"
}
Invoke-NativeChecked -FilePath $python -Arguments @(
    '-c',
    "import torch; assert torch.__version__.startswith('2.0.1'); assert torch.cuda.is_available(); print('torch.__version__=' + torch.__version__); print('cuda=' + str(torch.version.cuda)); print('gpu=' + torch.cuda.get_device_name(0))"
)
Invoke-NativeChecked -FilePath $python -Arguments @(
    '-c',
    'import cv2, diffusers, mmcv, mmdet, mmengine, mmpose, omegaconf, transformers; print("MuseTalk imports OK")'
)

$requiredModels = @(
    'models\musetalkV15\musetalk.json',
    'models\musetalkV15\unet.pth',
    'models\sd-vae\config.json',
    'models\sd-vae\diffusion_pytorch_model.bin',
    'models\whisper\config.json',
    'models\whisper\pytorch_model.bin',
    'models\whisper\preprocessor_config.json',
    'models\dwpose\dw-ll_ucoco_384.pth',
    'models\syncnet\latentsync_syncnet.pt',
    'models\face-parse-bisent\79999_iter.pth',
    'models\face-parse-bisent\resnet18-5c106cde.pth'
)
foreach ($relativePath in $requiredModels) {
    Assert-File -Path (Join-Path $repository $relativePath)
}

$hashManifest = Join-Path $repository 'models\model-hashes.json'
Assert-File -Path $hashManifest
$hashRecords = Get-Content -LiteralPath $hashManifest -Raw | ConvertFrom-Json
foreach ($record in $hashRecords) {
    $modelPath = Join-Path $repository $record.path
    Assert-File -Path $modelPath
    $actualHash = (Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $record.sha256) {
        throw "Model SHA256 mismatch for $modelPath"
    }
}

Invoke-NativeChecked -FilePath 'git' -Arguments @('--version')
Invoke-NativeChecked -FilePath 'ffmpeg' -Arguments @('-version')
Invoke-NativeChecked -FilePath 'ffprobe' -Arguments @('-version')
Invoke-NativeChecked -FilePath 'nvidia-smi' -Arguments @('--query-gpu=name,memory.used,memory.total,driver_version', '--format=csv')

$listener = Get-NetTCPConnection -State Listen -LocalPort 8030 -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "MuseTalk port 8030 is listening (PID $($listener.OwningProcess -join ','))"
}
else {
    Write-Host 'MuseTalk port 8030 is free'
}
Write-Host "MuseTalk environment verified: Python $pythonVersion, commit $commit"
