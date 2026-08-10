$ErrorActionPreference = 'Stop'
$env:HF_ENDPOINT = 'https://huggingface.co'
$comfyRoot = 'E:\LocalDramaAI\ComfyUI'
$hf = 'E:\LocalDramaAI\env-comfyui\Scripts\hf.exe'
$stageRoot = 'E:\LocalDramaAI\model-staging-wan22'

$models = @(
    @{ Repo = 'Comfy-Org/Wan_2.2_ComfyUI_Repackaged'; File = 'split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors'; Destination = "$comfyRoot\models\diffusion_models\wan2.2_ti2v_5B_fp16.safetensors"; Sha256 = '456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e' },
    @{ Repo = 'Comfy-Org/Wan_2.2_ComfyUI_Repackaged'; File = 'split_files/vae/wan2.2_vae.safetensors'; Destination = "$comfyRoot\models\vae\wan2.2_vae.safetensors"; Sha256 = 'e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156' },
    @{ Repo = 'Comfy-Org/Wan_2.1_ComfyUI_repackaged'; File = 'split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors'; Destination = "$comfyRoot\models\text_encoders\umt5_xxl_fp8_e4m3fn_scaled.safetensors"; Sha256 = 'c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68' }
)

foreach ($model in $models) {
    $repoStage = Join-Path $stageRoot ($model.Repo -replace '/', '_')
    New-Item -ItemType Directory -Force -Path $repoStage | Out-Null
    & $hf download $model.Repo $model.File --local-dir $repoStage
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $($model.File)" }
    $staged = Join-Path $repoStage $model.File
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $staged).Hash.ToLowerInvariant()
    if ($actualHash -ne $model.Sha256) { throw "SHA256 mismatch: $($model.File)" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $model.Destination) | Out-Null
    Move-Item -Force -LiteralPath $staged -Destination $model.Destination
}
Write-Host 'Wan2.2 model files downloaded and SHA256 verified.'
