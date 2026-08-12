[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI',
    [string]$ProjectRoot = '',
    [string]$MuseTalkCommit = '0a89dec45a0192b824e3cf4daf96c239440c5ed8'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$repository = Join-Path $Root 'MuseTalk'
$pythonHome = Join-Path $Root 'python310-musetalk'
$environment = Join-Path $Root 'env-musetalk'
$python = Join-Path $pythonHome 'python.exe'
$environmentPython = Join-Path $environment 'Scripts\python.exe'
$pythonInstaller = Join-Path $env:TEMP 'python-3.10.11-amd64.exe'
$pythonInstallerUrl = 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe'
$pythonInstallerSha256 = 'D8DEDE5005564B408BA50317108B765ED9C3C510342A598F9FD42681CBE0648B'
$officialRepository = 'https://github.com/TMElyralab/MuseTalk.git'
$officialConstraints = Join-Path $PSScriptRoot 'musetalk-requirements.constraints.txt'

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = ''
    )

    if ($WorkingDirectory) {
        Push-Location -LiteralPath $WorkingDirectory
    }
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
        }
    }
    finally {
        if ($WorkingDirectory) {
            Pop-Location
        }
    }
}

New-Item -ItemType Directory -Path $Root -Force | Out-Null

if (-not (Test-Path -LiteralPath $python)) {
    Invoke-WebRequest -Uri $pythonInstallerUrl -OutFile $pythonInstaller
    $actualHash = (Get-FileHash -LiteralPath $pythonInstaller -Algorithm SHA256).Hash
    if ($actualHash -ne $pythonInstallerSha256) {
        throw "Python installer SHA256 mismatch: expected $pythonInstallerSha256, got $actualHash"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $pythonInstaller
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Python installer signature is not valid: $($signature.Status)"
    }
    $install = Start-Process -FilePath $pythonInstaller -ArgumentList @(
        '/quiet',
        'InstallAllUsers=0',
        "TargetDir=$pythonHome",
        'Include_pip=1',
        'Include_launcher=0',
        'Include_test=0',
        'PrependPath=0',
        'Shortcuts=0'
    ) -Wait -PassThru
    if ($install.ExitCode -ne 0) {
        throw "Python 3.10.11 installer failed with exit code $($install.ExitCode)"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Dedicated Python executable was not created: $python"
}
$pythonVersion = (& $python -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.10.11') {
    throw "MuseTalk requires dedicated Python 3.10.11, found '$pythonVersion' at $python"
}

if (Test-Path -LiteralPath (Join-Path $repository '.git')) {
    $origin = (& git -C $repository remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $origin -ne $officialRepository) {
        throw "Existing MuseTalk checkout has an unexpected origin: $origin"
    }
    $dirty = & git -C $repository status --porcelain
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw "Existing MuseTalk checkout is dirty; preserve or remove those changes before setup"
    }
}
elseif (Test-Path -LiteralPath $repository) {
    throw "MuseTalk destination exists but is not a Git checkout: $repository"
}
else {
    Invoke-NativeChecked -FilePath 'git' -Arguments @('clone', '--filter=blob:none', '--no-checkout', $officialRepository, $repository)
}

Invoke-NativeChecked -FilePath 'git' -Arguments @('-C', $repository, 'fetch', '--depth', '1', 'origin', $MuseTalkCommit)
Invoke-NativeChecked -FilePath 'git' -Arguments @('-C', $repository, 'checkout', '--detach', $MuseTalkCommit)
$resolvedCommit = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $resolvedCommit -ne $MuseTalkCommit) {
    throw "MuseTalk checkout mismatch: expected $MuseTalkCommit, got $resolvedCommit"
}

if (-not (Test-Path -LiteralPath $environmentPython)) {
    Invoke-NativeChecked -FilePath $python -Arguments @('-m', 'venv', $environment)
}
$environmentVersion = (& $environmentPython -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0 -or $environmentVersion -ne '3.10.11') {
    throw "The existing env-musetalk is not Python 3.10.11: $environmentVersion"
}
if (-not (Test-Path -LiteralPath $officialConstraints -PathType Leaf)) {
    throw "MuseTalk constraints file is missing: $officialConstraints"
}

Invoke-NativeChecked -FilePath $environmentPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip==24.3.1', 'setuptools==75.6.0', 'wheel==0.45.1')
Invoke-NativeChecked -FilePath $environmentPython -Arguments @(
    '-m', 'pip', 'install',
    'torch==2.0.1', 'torchvision==0.15.2', 'torchaudio==2.0.2',
    '--index-url', 'https://download.pytorch.org/whl/cu118'
)
Invoke-NativeChecked -FilePath $environmentPython -Arguments @(
    '-m', 'pip', 'install',
    '-r', (Join-Path $repository 'requirements.txt'),
    '-c', $officialConstraints
)
Invoke-NativeChecked -FilePath $environmentPython -Arguments @('-m', 'pip', 'install', '--no-cache-dir', 'openmim==0.3.9')

$mim = Join-Path $environment 'Scripts\mim.exe'
if (-not (Test-Path -LiteralPath $mim)) {
    throw "OpenMIM executable is missing after installation: $mim"
}
Invoke-NativeChecked -FilePath $mim -Arguments @('install', 'mmengine==0.10.7')
Invoke-NativeChecked -FilePath $mim -Arguments @('install', 'mmcv==2.0.1')
Invoke-NativeChecked -FilePath $mim -Arguments @('install', 'mmdet==3.1.0')
Invoke-NativeChecked -FilePath $mim -Arguments @('install', 'mmpose==1.1.0')

$serviceRequirements = Join-Path $ProjectRoot 'ai_services\musetalk\requirements.lock.txt'
if (-not (Test-Path -LiteralPath $serviceRequirements)) {
    throw "MuseTalk service requirements are missing: $serviceRequirements"
}
Invoke-NativeChecked -FilePath $environmentPython -Arguments @('-m', 'pip', 'install', '-r', $serviceRequirements)
Invoke-NativeChecked -FilePath $environmentPython -Arguments @('-m', 'pip', 'check')
Invoke-NativeChecked -FilePath $environmentPython -Arguments @(
    '-c',
    "from importlib.metadata import version; import torch; expected=('torch==2.0.1+cu118','torchvision==0.15.2+cu118','torchaudio==2.0.2+cu118','openmim==0.3.9','mmcv==2.0.1','mmdet==3.1.0','mmengine==0.10.7','mmpose==1.1.0'); actual=tuple(f'{name}=={version(name)}' for name in ('torch','torchvision','torchaudio','openmim','mmcv','mmdet','mmengine','mmpose')); assert actual == expected, f'package version mismatch: expected {expected}, got {actual}'; assert torch.version.cuda == '11.8'; assert torch.cuda.is_available(); print(*actual, 'cuda='+str(torch.version.cuda), 'gpu='+torch.cuda.get_device_name(0))"
)
Invoke-NativeChecked -FilePath $environmentPython -Arguments @(
    '-c',
    'import cv2, diffusers, mmcv, mmdet, mmengine, mmpose, omegaconf, transformers; print("MuseTalk imports OK")'
)

Write-Host "MuseTalk environment ready at $environment"
Write-Host "MuseTalk commit: $resolvedCommit"
