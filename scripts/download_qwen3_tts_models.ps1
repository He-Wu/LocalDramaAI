$ErrorActionPreference = 'Stop'
$env:HF_ENDPOINT = 'https://huggingface.co'
$env:HF_XET_HIGH_PERFORMANCE = '1'
$hf = 'E:\LocalDramaAI\env-comfyui\Scripts\hf.exe'
$modelsRoot = 'E:\LocalDramaAI\Models'
$downloads = @(
    @{ Repo = 'Qwen/Qwen3-TTS-12Hz-0.6B-Base'; Directory = "$modelsRoot\Qwen3-TTS-12Hz-0.6B-Base" },
    @{ Repo = 'Qwen/Qwen3-TTS-Tokenizer-12Hz'; Directory = "$modelsRoot\Qwen3-TTS-Tokenizer-12Hz" }
)
foreach ($item in $downloads) {
    New-Item -ItemType Directory -Force -Path $item.Directory | Out-Null
    & $hf download $item.Repo --local-dir $item.Directory
    if ($LASTEXITCODE -ne 0) { throw "Model download failed: $($item.Repo)" }
}
$referenceDir = 'E:\LocalDramaAI\Storage\shared\voices'
New-Item -ItemType Directory -Force -Path $referenceDir | Out-Null
& curl.exe -L --fail --retry 5 'https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav' -o "$referenceDir\qwen3_clone_reference.wav"
if ($LASTEXITCODE -ne 0) { throw 'Reference audio download failed.' }
Write-Host 'Qwen3-TTS models and official reference audio downloaded.'
