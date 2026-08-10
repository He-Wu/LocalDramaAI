from pathlib import Path
import os
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PHASE8_SCRIPTS = (
    "setup_musetalk.ps1",
    "download_musetalk_models.ps1",
    "start_musetalk.ps1",
    "start_ai_services.ps1",
    "stop_all.ps1",
    "check_environment.ps1",
)


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_phase8_powershell_scripts_parse():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "PowerShell is required by this Windows-first project"
    parser = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$env:PHASE8_SCRIPT, [ref]$null, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    for name in PHASE8_SCRIPTS:
        path = SCRIPTS / name
        assert path.is_file(), f"missing lifecycle script: {name}"
        environment = os.environ.copy()
        environment["PHASE8_SCRIPT"] = str(path)
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", parser],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        assert result.returncode == 0, f"{name} parse failed: {result.stderr}"


def test_setup_is_pinned_and_isolated():
    setup = _text("setup_musetalk.ps1")
    assert "0a89dec45a0192b824e3cf4daf96c239440c5ed8" in setup
    assert "python-3.10.11-amd64.exe" in setup
    assert "D8DEDE5005564B408BA50317108B765ED9C3C510342A598F9FD42681CBE0648B" in setup
    assert "torch==2.0.1" in setup
    assert "torchvision==0.15.2" in setup
    assert "torchaudio==2.0.2" in setup
    assert "mmcv==2.0.1" in setup
    assert "mmdet==3.1.0" in setup
    assert "mmpose==1.1.0" in setup
    assert "env-musetalk" in setup
    assert "env-tts" not in setup
    assert "env-comfyui" not in setup
    assert "Get-FileHash" in setup
    assert "Get-AuthenticodeSignature" in setup
    assert "$LASTEXITCODE" in setup
    assert "pip', 'check" in setup


def test_model_downloads_use_official_pinned_repositories_and_hash_every_file():
    download = _text("download_musetalk_models.ps1")
    expected = {
        "TMElyralab/MuseTalk": "3ef28bc5cff08c90ad8178a25f1b570cd800170f",
        "stabilityai/sd-vae-ft-mse": "31f26fdeee1355a5c34592e401dd41e45d25a493",
        "openai/whisper-tiny": "169d4a4341b33bc18d8881c4b69c2e104e1cc0af",
        "yzd-v/DWPose": "1a7144101628d69ee7a3768d1ee3a094070dc388",
        "ByteDance/LatentSync": "405eda8eab9f65c1a6e0c292a5dee5a08089e2ae",
        "ManyOtherFunctions/face-parse-bisent": "0073b233a5a3c4b1377d4dbf49245017938a72b5",
    }
    for repository, revision in expected.items():
        assert repository in download
        assert revision in download
    for filename in (
        "musetalkV15/musetalk.json",
        "musetalkV15/unet.pth",
        "diffusion_pytorch_model.bin",
        "pytorch_model.bin",
        "preprocessor_config.json",
        "dw-ll_ucoco_384.pth",
        "latentsync_syncnet.pt",
        "79999_iter.pth",
        "resnet18-5c106cde.pth",
    ):
        assert filename in download
    assert "Get-FileHash" in download
    assert "model-hashes.json" in download
    assert "env-tts" not in download
    assert "env-comfyui" not in download


def test_musetalk_lifecycle_is_loopback_hidden_and_stops_children():
    start = _text("start_musetalk.ps1")
    aggregate = _text("start_ai_services.ps1")
    stop = _text("stop_all.ps1")
    assert "127.0.0.1" in start and "8030" in start
    assert "ai_services.musetalk.service:app" in start
    assert "LOCALDRAMA_MUSETALK_REPO" in start
    assert "-WindowStyle" in start and "Hidden" in start
    assert "start_musetalk.ps1" in aggregate
    assert "ai_services.musetalk.service:app" in stop
    assert "scripts.inference" in stop


def test_environment_check_targets_only_dedicated_musetalk_runtime():
    check = _text("check_environment.ps1")
    assert "env-musetalk" in check
    assert "MuseTalk" in check
    assert "3.10" in check
    assert "torch.__version__" in check
    assert "torch.cuda.is_available" in check
    assert "musetalkV15" in check
    assert "latentsync_syncnet.pt" in check
    assert "dw-ll_ucoco_384.pth" in check
    assert "79999_iter.pth" in check
