[CmdletBinding()]
param(
    [string]$Root = 'E:\LocalDramaAI',
    [string]$HuggingFaceEndpoint = 'https://huggingface.co'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$repository = Join-Path $Root 'MuseTalk'
$models = Join-Path $repository 'models'
$curl = (Get-Command curl.exe -CommandType Application -ErrorAction Stop).Source
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
        ExpectedBytes = @([long]748, [long]3400074924)
        ExpectedSha256 = @(
            '5b6923aee04d71692e0e9846c471e0a4ea07a4f686d39545e472bd4ba17e1b47',
            '7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007'
        )
    },
    [pscustomobject]@{
        Repository = 'stabilityai/sd-vae-ft-mse'
        Revision = '31f26fdeee1355a5c34592e401dd41e45d25a493'
        LocalDirectory = (Join-Path $models 'sd-vae')
        Include = @('config.json', 'diffusion_pytorch_model.bin')
        Expected = @('sd-vae\config.json', 'sd-vae\diffusion_pytorch_model.bin')
        ExpectedBytes = @([long]547, [long]334707217)
        ExpectedSha256 = @(
            '92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e',
            '1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc'
        )
    },
    [pscustomobject]@{
        Repository = 'openai/whisper-tiny'
        Revision = '169d4a4341b33bc18d8881c4b69c2e104e1cc0af'
        LocalDirectory = (Join-Path $models 'whisper')
        Include = @('config.json', 'pytorch_model.bin', 'preprocessor_config.json')
        Expected = @('whisper\config.json', 'whisper\pytorch_model.bin', 'whisper\preprocessor_config.json')
        ExpectedBytes = @([long]1983, [long]151095027, [long]184990)
        ExpectedSha256 = @(
            'ffdccec4f3211f4c63310f2b7098f309fe70f3952cedc5e4d11e43f5b2379b98',
            '9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d',
            '9b5cd03a36fbb8a627c64d98a5b5b126ead95a77720723944487311f0110b666'
        )
    },
    [pscustomobject]@{
        Repository = 'yzd-v/DWPose'
        Revision = '1a7144101628d69ee7a3768d1ee3a094070dc388'
        LocalDirectory = (Join-Path $models 'dwpose')
        Include = @('dw-ll_ucoco_384.pth')
        Expected = @('dwpose\dw-ll_ucoco_384.pth')
        ExpectedBytes = @([long]406878486)
        ExpectedSha256 = @('0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07')
    },
    [pscustomobject]@{
        Repository = 'ByteDance/LatentSync'
        Revision = '405eda8eab9f65c1a6e0c292a5dee5a08089e2ae'
        LocalDirectory = (Join-Path $models 'syncnet')
        Include = @('latentsync_syncnet.pt')
        Expected = @('syncnet\latentsync_syncnet.pt')
        ExpectedBytes = @([long]1488019828)
        ExpectedSha256 = @('38fa63bad3ed2332f647c40a5dc616cb0e233db8579f698f62af4c41965c4da5')
    },
    [pscustomobject]@{
        Repository = 'ManyOtherFunctions/face-parse-bisent'
        Revision = '0073b233a5a3c4b1377d4dbf49245017938a72b5'
        LocalDirectory = (Join-Path $models 'face-parse-bisent')
        Include = @('79999_iter.pth', 'resnet18-5c106cde.pth')
        Expected = @('face-parse-bisent\79999_iter.pth', 'face-parse-bisent\resnet18-5c106cde.pth')
        ExpectedBytes = @([long]53289463, [long]46827520)
        ExpectedSha256 = @(
            '468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567',
            '5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8'
        )
    }
)

$modelLockPath = Join-Path $PSScriptRoot 'musetalk-models.lock.json'
if (-not (Test-Path -LiteralPath $modelLockPath -PathType Leaf)) {
    throw "Authoritative MuseTalk model lock is missing: $modelLockPath"
}
$lockedModels = @(Get-Content -LiteralPath $modelLockPath -Raw | ConvertFrom-Json)
$downloadDefinitions = @()
foreach ($download in $downloads) {
    for ($index = 0; $index -lt $download.Include.Count; $index++) {
        $downloadDefinitions += [pscustomobject]@{
            repository = [string]$download.Repository
            revision = [string]$download.Revision
            source = [string]$download.Include[$index]
            path = 'models/' + ([string]$download.Expected[$index]).Replace('\', '/')
            bytes = [long]$download.ExpectedBytes[$index]
            sha256 = [string]$download.ExpectedSha256[$index]
        }
    }
}
if ($lockedModels.Count -ne $downloadDefinitions.Count) {
    throw 'Download definition differs from the authoritative model lock: record count mismatch'
}
$lockByPath = @{}
foreach ($lockedModel in $lockedModels) {
    $lockedPath = [string]$lockedModel.path
    if (-not $lockedPath -or $lockByPath.ContainsKey($lockedPath)) {
        throw "Download definition differs from the authoritative model lock: missing or duplicate path $lockedPath"
    }
    $lockByPath[$lockedPath] = $lockedModel
}
foreach ($definition in $downloadDefinitions) {
    $lockedModel = $lockByPath[[string]$definition.path]
    if ($null -eq $lockedModel -or
        [string]$definition.repository -ne [string]$lockedModel.repository -or
        [string]$definition.revision -ne [string]$lockedModel.revision -or
        [string]$definition.source -ne [string]$lockedModel.source -or
        [long]$definition.bytes -ne [long]$lockedModel.bytes -or
        [string]$definition.sha256 -ne [string]$lockedModel.sha256) {
        throw "Download definition differs from the authoritative model lock: $($definition.path)"
    }
}

New-Item -ItemType Directory -Path $models -Force | Out-Null
$manifest = Join-Path $models 'model-hashes.json'
$verifiedRecords = @{}
$calculatedHashes = @{}
if (Test-Path -LiteralPath $manifest -PathType Leaf) {
    try {
        foreach ($record in @(Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json)) {
            $verifiedRecords[[string]$record.path] = $record
        }
    }
    catch {
        Write-Warning "Ignoring unreadable existing model hash manifest: $manifest"
    }
}

foreach ($download in $downloads) {
    for ($index = 0; $index -lt $download.Include.Count; $index++) {
        $sourcePath = $download.Include[$index]
        $relativePath = $download.Expected[$index]
        $expectedBytes = [long]$download.ExpectedBytes[$index]
        $expectedSha256 = [string]$download.ExpectedSha256[$index]
        $destination = Join-Path $models $relativePath
        $manifestPath = [System.IO.Path]::GetRelativePath($repository, $destination).Replace('\', '/')
        $record = $verifiedRecords[$manifestPath]
        $cached = $false
        if ($null -ne $record -and
            $record.repository -eq $download.Repository -and
            $record.revision -eq $download.Revision -and
            (Test-Path -LiteralPath $destination -PathType Leaf)) {
            $file = Get-Item -LiteralPath $destination
            if ($file.Length -eq $expectedBytes -and $file.Length -eq [long]$record.bytes) {
                $hash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
                $cached = $hash -eq [string]$record.sha256 -and
                    (-not $expectedSha256 -or $hash -eq $expectedSha256)
                if ($cached) {
                    $calculatedHashes[$manifestPath] = $hash
                }
            }
        }
        if ($cached) {
            Write-Host "Using verified cached model: $manifestPath"
            continue
        }

        $destinationDirectory = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        $partial = "$destination.$($download.Revision).download"
        $encodedSegments = @($sourcePath -split '/' | ForEach-Object { [Uri]::EscapeDataString($_) })
        $encodedPath = $encodedSegments -join '/'
        $url = "$($HuggingFaceEndpoint.TrimEnd('/'))/$($download.Repository)/resolve/$($download.Revision)/${encodedPath}?download=true"
        $arguments = @(
            '--fail',
            '--location',
            '--show-error',
            '--connect-timeout', '30',
            '--speed-limit', '1024',
            '--speed-time', '60',
            '--continue-at', '-',
            '--output', $partial,
            $url
        )
        $curlExitCode = 1
        $rangeResetAttempted = $false
        for ($attempt = 1; $attempt -le 6; $attempt++) {
            & $curl @arguments
            $curlExitCode = $LASTEXITCODE
            if ($curlExitCode -eq 0) {
                break
            }
            if ($curlExitCode -eq 33 -and -not $rangeResetAttempted -and
                (Test-Path -LiteralPath $partial -PathType Leaf) -and
                (Get-Item -LiteralPath $partial).Length -gt 0) {
                Write-Warning "The remote server rejected the partial range; restarting this file once"
                Remove-Item -LiteralPath $partial -Force
                $rangeResetAttempted = $true
            }
            if ($attempt -lt 6) {
                $partialBytes = if (Test-Path -LiteralPath $partial -PathType Leaf) {
                    (Get-Item -LiteralPath $partial).Length
                }
                else {
                    0
                }
                Write-Warning "Download attempt $attempt failed with curl exit $curlExitCode; retaining $partialBytes bytes and retrying"
                Start-Sleep -Seconds ([Math]::Min(30, [Math]::Pow(2, $attempt)))
            }
        }
        if ($curlExitCode -ne 0) {
            throw "Official model download failed for $($download.Repository)/$sourcePath at $($download.Revision); partial file retained at $partial"
        }
        if (-not (Test-Path -LiteralPath $partial -PathType Leaf)) {
            throw "Official model download did not produce a file: $partial"
        }
        $partialFile = Get-Item -LiteralPath $partial
        if ($partialFile.Length -ne $expectedBytes) {
            throw "Official model size mismatch for $sourcePath`: expected $expectedBytes bytes, got $($partialFile.Length); partial file retained at $partial"
        }
        $downloadedHash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expectedSha256 -and $downloadedHash -ne $expectedSha256) {
            throw "Official LFS SHA256 mismatch for $sourcePath`: expected $expectedSha256, got $downloadedHash; partial file retained at $partial"
        }
        Move-Item -LiteralPath $partial -Destination $destination -Force
        $calculatedHashes[$manifestPath] = $downloadedHash
    }
}

$hashRecords = @()
foreach ($download in $downloads) {
    for ($index = 0; $index -lt $download.Expected.Count; $index++) {
        $relativePath = $download.Expected[$index]
        $expectedBytes = [long]$download.ExpectedBytes[$index]
        $expectedSha256 = [string]$download.ExpectedSha256[$index]
        $path = Join-Path $models $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Expected MuseTalk model file is missing: $path"
        }
        $file = Get-Item -LiteralPath $path
        if ($file.Length -ne $expectedBytes) {
            throw "MuseTalk model size mismatch for $path`: expected $expectedBytes bytes, got $($file.Length)"
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
        $manifestPath = [System.IO.Path]::GetRelativePath($repository, $path).Replace('\', '/')
        $hash = $calculatedHashes[$manifestPath]
        if (-not $hash) {
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        if ($hash.Length -ne 64) {
            throw "Could not calculate SHA256 for MuseTalk model file: $path"
        }
        if ($expectedSha256 -and $hash -ne $expectedSha256) {
            throw "MuseTalk official LFS SHA256 mismatch for $path"
        }
        $hashRecords += [pscustomobject]@{
            repository = $download.Repository
            revision = $download.Revision
            path = $manifestPath
            bytes = $file.Length
            sha256 = $hash
        }
    }
}

$temporaryManifest = "$manifest.tmp"
$hashRecords | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryManifest -Encoding utf8
Move-Item -LiteralPath $temporaryManifest -Destination $manifest -Force
Write-Host "MuseTalk model files verified; SHA256 manifest: $manifest"
