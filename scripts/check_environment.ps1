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
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.10.11') {
    throw "env-musetalk must use Python 3.10.11, got $pythonVersion"
}
Invoke-NativeChecked -FilePath $python -Arguments @(
    '-c',
    "from importlib.metadata import version; import torch; expected=('torch==2.0.1+cu118','torchvision==0.15.2+cu118','torchaudio==2.0.2+cu118','openmim==0.3.9','mmcv==2.0.1','mmdet==3.1.0','mmengine==0.10.7','mmpose==1.1.0'); actual=tuple(f'{name}=={version(name)}' for name in ('torch','torchvision','torchaudio','openmim','mmcv','mmdet','mmengine','mmpose')); assert actual == expected, f'package version mismatch: expected {expected}, got {actual}'; assert torch.version.cuda == '11.8'; assert torch.cuda.is_available(); print(*actual, 'cuda='+str(torch.version.cuda), 'gpu='+torch.cuda.get_device_name(0))"
)
Invoke-NativeChecked -FilePath $python -Arguments @(
    '-c',
    'import cv2, diffusers, mmcv, mmdet, mmengine, mmpose, omegaconf, transformers; print("MuseTalk imports OK")'
)

$hashManifest = Join-Path $repository 'models\model-hashes.json'
Assert-File -Path $hashManifest
$modelLockPath = Join-Path $PSScriptRoot 'musetalk-models.lock.json'
$modelVerifierPath = Join-Path $PSScriptRoot 'musetalk_model_verification.ps1'
Assert-File -Path $modelLockPath
Assert-File -Path $modelVerifierPath
$expectedModels = @(Get-Content -LiteralPath $modelLockPath -Raw | ConvertFrom-Json)
. $modelVerifierPath
Assert-MuseTalkModelFiles -Repository $repository -ManifestPath $hashManifest -ExpectedModels $expectedModels

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
