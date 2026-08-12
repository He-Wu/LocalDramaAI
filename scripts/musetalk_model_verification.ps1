function Assert-MuseTalkModelFiles {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][object[]]$ExpectedModels
    )

    $repositoryRoot = [System.IO.Path]::GetFullPath($Repository).TrimEnd('\', '/')
    $modelsRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'models')).TrimEnd('\', '/')
    $modelsPrefix = $modelsRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "MuseTalk model manifest is missing: $ManifestPath"
    }

    try {
        $manifestRecords = @(Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json)
    }
    catch {
        throw "MuseTalk model manifest is not valid JSON: $ManifestPath"
    }
    if ($ExpectedModels.Count -eq 0) {
        throw 'The authoritative MuseTalk model lock is empty'
    }
    if ($manifestRecords.Count -ne $ExpectedModels.Count) {
        throw "MuseTalk model manifest must contain exactly $($ExpectedModels.Count) records; found $($manifestRecords.Count)"
    }

    $expectedByPath = @{}
    foreach ($expected in $ExpectedModels) {
        $expectedPath = [string]$expected.path
        if (-not $expectedPath -or $expectedByPath.ContainsKey($expectedPath)) {
            throw "The authoritative MuseTalk model lock has a missing or duplicate path: $expectedPath"
        }
        $expectedFullPath = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $expectedPath))
        if (-not $expectedFullPath.StartsWith($modelsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "The authoritative MuseTalk model path escapes the models directory: $expectedPath"
        }
        if ([long]$expected.bytes -le 0 -or [string]$expected.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "The authoritative MuseTalk model tuple is invalid: $expectedPath"
        }
        $expectedByPath[$expectedPath] = $expected
    }

    $seenPaths = @{}
    foreach ($record in $manifestRecords) {
        $recordPath = [string]$record.path
        if (-not $recordPath -or $seenPaths.ContainsKey($recordPath)) {
            throw "MuseTalk model manifest has a missing or duplicate path: $recordPath"
        }
        $recordFullPath = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $recordPath))
        if (-not $recordFullPath.StartsWith($modelsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "MuseTalk model manifest path escapes the models directory: $recordPath"
        }
        if (-not $expectedByPath.ContainsKey($recordPath)) {
            throw "MuseTalk model manifest contains an unexpected path: $recordPath"
        }
        $seenPaths[$recordPath] = $true
        $expected = $expectedByPath[$recordPath]
        if ([string]$record.repository -ne [string]$expected.repository -or
            [string]$record.revision -ne [string]$expected.revision -or
            [long]$record.bytes -ne [long]$expected.bytes -or
            [string]$record.sha256 -ne [string]$expected.sha256) {
            throw "MuseTalk model manifest tuple differs from the authoritative lock: $recordPath"
        }
        if (-not (Test-Path -LiteralPath $recordFullPath -PathType Leaf)) {
            throw "Required MuseTalk model file is missing: $recordFullPath"
        }
        $file = Get-Item -LiteralPath $recordFullPath
        if ($file.Length -ne [long]$expected.bytes) {
            throw "MuseTalk model byte count mismatch for $recordPath"
        }
        $actualHash = (Get-FileHash -LiteralPath $recordFullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne [string]$expected.sha256) {
            throw "MuseTalk model SHA256 mismatch for $recordPath"
        }
    }

    foreach ($expectedPath in $expectedByPath.Keys) {
        if (-not $seenPaths.ContainsKey($expectedPath)) {
            throw "MuseTalk model manifest is missing the authoritative path: $expectedPath"
        }
    }
}
