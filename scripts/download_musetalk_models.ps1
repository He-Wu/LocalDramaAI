[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$repository = Join-Path $Root 'MuseTalk'
$models = Join-Path $repository 'models'
$environment = Join-Path $Root 'env-musetalk'
$python = Join-Path $environment 'Scripts\python.exe'
$huggingFace = Join-Path $environment 'Scripts\huggingface-cli.exe'
if (-not (Test-Path -LiteralPath $huggingFace)) {
    $huggingFace = Join-Path $environment 'Scripts\hf.exe'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Run setup_musetalk.ps1 first; Python is missing: $python"
}
if (-not (Test-Path -LiteralPath $huggingFace)) {
    throw "Hugging Face CLI is missing from env-musetalk: $huggingFace"
}
if (-not (Test-Path -LiteralPath (Join-Path $repository '.git'))) {
    throw "Official MuseTalk checkout is missing: $repository"
}

$downloads = @(
    [pscustomobject]@{
        Repository = 'TMElyralab/MuseTalk'
        Revision = '3ef28bc5cff08c90ad8178a25f1b570cd800170f'
        LocalDirectory = $models
        Include = @('musetalkV15/musetalk.json', 'musetalkV15/unet.pth')
        Expected = @('musetalkV15\musetalk.json', 'musetalkV15\unet.pth')
    },
    [pscustomobject]@{
        Repository = 'stabilityai/sd-vae-ft-mse'
        Revision = '31f26fdeee1355a5c34592e401dd41e45d25a493'
        LocalDirectory = (Join-Path $models 'sd-vae')
        Include = @('config.json', 'diffusion_pytorch_model.bin')
        Expected = @('sd-vae\config.json', 'sd-vae\diffusion_pytorch_model.bin')
    },
    [pscustomobject]@{
        Repository = 'openai/whisper-tiny'
        Revision = '169d4a4341b33bc18d8881c4b69c2e104e1cc0af'
        LocalDirectory = (Join-Path $models 'whisper')
        Include = @('config.json', 'pytorch_model.bin', 'preprocessor_config.json')
        Expected = @('whisper\config.json', 'whisper\pytorch_model.bin', 'whisper\preprocessor_config.json')
    },
    [pscustomobject]@{
        Repository = 'yzd-v/DWPose'
        Revision = '1a7144101628d69ee7a3768d1ee3a094070dc388'
        LocalDirectory = (Join-Path $models 'dwpose')
        Include = @('dw-ll_ucoco_384.pth')
        Expected = @('dwpose\dw-ll_ucoco_384.pth')
    },
    [pscustomobject]@{
        Repository = 'ByteDance/LatentSync'
        Revision = '405eda8eab9f65c1a6e0c292a5dee5a08089e2ae'
        LocalDirectory = (Join-Path $models 'syncnet')
        Include = @('latentsync_syncnet.pt')
        Expected = @('syncnet\latentsync_syncnet.pt')
    },
    [pscustomobject]@{
        Repository = 'ManyOtherFunctions/face-parse-bisent'
        Revision = '0073b233a5a3c4b1377d4dbf49245017938a72b5'
        LocalDirectory = (Join-Path $models 'face-parse-bisent')
        Include = @('79999_iter.pth', 'resnet18-5c106cde.pth')
        Expected = @('face-parse-bisent\79999_iter.pth', 'face-parse-bisent\resnet18-5c106cde.pth')
    }
)

New-Item -ItemType Directory -Path $models -Force | Out-Null
foreach ($download in $downloads) {
    New-Item -ItemType Directory -Path $download.LocalDirectory -Force | Out-Null
    $arguments = @(
        'download',
        $download.Repository,
        '--revision', $download.Revision,
        '--local-dir', $download.LocalDirectory,
        '--include'
    ) + $download.Include
    & $huggingFace @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Hugging Face download failed for $($download.Repository) at $($download.Revision)"
    }
}

$hashRecords = @()
foreach ($download in $downloads) {
    foreach ($relativePath in $download.Expected) {
        $path = Join-Path $models $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Expected MuseTalk model file is missing: $path"
        }
        $file = Get-Item -LiteralPath $path
        if ($file.Length -le 0) {
            throw "MuseTalk model file is empty: $path"
        }
        $prefixBuffer = New-Object byte[] 128
        $stream = [System.IO.File]::OpenRead($path)
        try {
            $prefixLength = $stream.Read($prefixBuffer, 0, $prefixBuffer.Length)
        }
        finally {
            $stream.Dispose()
        }
        $prefixText = [System.Text.Encoding]::ASCII.GetString($prefixBuffer, 0, $prefixLength)
        if ($prefixText.StartsWith('version https://git-lfs.github.com/spec/')) {
            throw "MuseTalk model file is an unresolved Git LFS pointer: $path"
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash.Length -ne 64) {
            throw "Could not calculate SHA256 for MuseTalk model file: $path"
        }
        $hashRecords += [pscustomobject]@{
            repository = $download.Repository
            revision = $download.Revision
            path = [System.IO.Path]::GetRelativePath($repository, $path).Replace('\', '/')
            bytes = $file.Length
            sha256 = $hash
        }
    }
}

$manifest = Join-Path $models 'model-hashes.json'
$temporaryManifest = "$manifest.tmp"
$hashRecords | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryManifest -Encoding utf8
Move-Item -LiteralPath $temporaryManifest -Destination $manifest -Force
Write-Host "MuseTalk model files verified; SHA256 manifest: $manifest"
