from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PHASE8_SCRIPTS = (
    "setup_musetalk.ps1",
    "download_musetalk_models.ps1",
    "start_musetalk.ps1",
    "start_ai_services.ps1",
    "stop_all.ps1",
    "check_environment.ps1",
    "musetalk_model_verification.ps1",
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
    constraints = _text("musetalk-requirements.constraints.txt")
    assert "0a89dec45a0192b824e3cf4daf96c239440c5ed8" in setup
    assert "python-3.10.11-amd64.exe" in setup
    assert "D8DEDE5005564B408BA50317108B765ED9C3C510342A598F9FD42681CBE0648B" in setup
    assert "torch==2.0.1" in setup
    assert "torchvision==0.15.2" in setup
    assert "torchaudio==2.0.2" in setup
    assert "openmim==0.3.9" in setup
    assert "mmengine==0.10.7" in setup
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
    assert "$pythonVersion -ne '3.10.11'" in setup
    assert "$environmentVersion -ne '3.10.11'" in setup
    assert "musetalk-requirements.constraints.txt" in setup
    assert "'-c', $officialConstraints" in setup
    for exact_version in (
        "torch==2.0.1+cu118",
        "torchvision==0.15.2+cu118",
        "torchaudio==2.0.2+cu118",
        "mmcv==2.0.1",
        "mmdet==3.1.0",
        "mmengine==0.10.7",
        "mmpose==1.1.0",
    ):
        assert exact_version in setup
    for exact_requirement in (
        "gdown==6.1.0",
        "requests==2.28.2",
        "imageio==2.37.4",
        "imageio-ffmpeg==0.6.0",
        "omegaconf==2.3.1",
        "ffmpeg-python==0.2.0",
        "moviepy==1.0.3",
    ):
        assert exact_requirement in constraints


def test_model_downloads_use_official_pinned_repositories_and_hash_every_file():
    download = _text("download_musetalk_models.ps1")
    assert "musetalk-models.lock.json" in download
    assert "Download definition differs from the authoritative model lock" in download
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
    for official_lfs_sha256 in (
        "5b6923aee04d71692e0e9846c471e0a4ea07a4f686d39545e472bd4ba17e1b47",
        "7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007",
        "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e",
        "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc",
        "ffdccec4f3211f4c63310f2b7098f309fe70f3952cedc5e4d11e43f5b2379b98",
        "9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d",
        "9b5cd03a36fbb8a627c64d98a5b5b126ead95a77720723944487311f0110b666",
        "0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07",
        "38fa63bad3ed2332f647c40a5dc616cb0e233db8579f698f62af4c41965c4da5",
        "468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567",
        "5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8",
    ):
        assert official_lfs_sha256 in download
    assert "3400074924" in download
    assert "ExpectedBytes" in download
    assert "ExpectedSha256" in download
    assert "Get-FileHash" in download
    assert "model-hashes.json" in download
    assert "curl.exe" in download
    assert "--location" in download
    assert "--continue-at" in download
    assert "for ($attempt = 1; $attempt -le" in download
    assert "Start-Sleep" in download
    assert "--retry-all-errors" not in download
    assert ".download" in download
    assert "Move-Item" in download
    assert "/resolve/" in download
    assert "?download=true" in download
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
    assert "musetalk-service.pid" in stop
    assert "ExecutablePath" in stop
    assert "ParentProcessId" in stop
    assert "'scripts.inference'" not in stop


def test_start_requires_task5_online_and_ready_health_contract():
    start = _text("start_musetalk.ps1")
    assert "$health.status -eq 'ONLINE'" in start
    assert "$health.ready -eq $true" in start
    assert "$health.status -in @('ok', 'ready')" not in start


def test_start_forwards_a_shell_safe_job_root_from_configured_root():
    start = _text("start_musetalk.ps1")
    assert "$jobRoot = Join-Path $Root 'musetalk-jobs'" in start
    assert "LOCALDRAMA_MUSETALK_JOB_ROOT" in start
    assert "[A-Za-z0-9_.-]" in start
    assert "shell-safe" in start


def test_stop_all_preserves_unrelated_inference_process_and_tolerates_stale_pid(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "scripts.inference"],
    )
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-File",
                str(SCRIPTS / "stop_all.ps1"),
                "-Root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert unrelated.poll() is None, "unrelated scripts.inference process was killed"

        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        stale = subprocess.Popen([sys.executable, "-c", "pass"])
        stale_pid = stale.pid
        stale.wait(timeout=10)
        (run_dir / "musetalk-service.pid").write_text(str(stale_pid), encoding="ascii")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-File",
                str(SCRIPTS / "stop_all.ps1"),
                "-Root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert not (run_dir / "musetalk-service.pid").exists()
    finally:
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=10)


def test_environment_check_targets_only_dedicated_musetalk_runtime():
    check = _text("check_environment.ps1")
    assert "env-musetalk" in check
    assert "MuseTalk" in check
    assert "3.10.11" in check
    assert "from importlib.metadata import version" in check
    assert "torch.cuda.is_available" in check
    assert "musetalk-models.lock.json" in check
    assert "Assert-MuseTalkModelFiles" in check
    for exact_version in (
        "torch==2.0.1+cu118",
        "torchvision==0.15.2+cu118",
        "torchaudio==2.0.2+cu118",
        "mmcv==2.0.1",
        "mmdet==3.1.0",
        "mmengine==0.10.7",
        "mmpose==1.1.0",
    ):
        assert exact_version in check


def test_model_lock_contains_exact_authoritative_tuples():
    lock_path = SCRIPTS / "musetalk-models.lock.json"
    records = json.loads(lock_path.read_text(encoding="utf-8"))
    actual = {
        (
            record["path"],
            record["repository"],
            record["revision"],
            record["bytes"],
            record["sha256"],
        )
        for record in records
    }
    expected = {
        ("models/musetalkV15/musetalk.json", "TMElyralab/MuseTalk", "3ef28bc5cff08c90ad8178a25f1b570cd800170f", 748, "5b6923aee04d71692e0e9846c471e0a4ea07a4f686d39545e472bd4ba17e1b47"),
        ("models/musetalkV15/unet.pth", "TMElyralab/MuseTalk", "3ef28bc5cff08c90ad8178a25f1b570cd800170f", 3400074924, "7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007"),
        ("models/sd-vae/config.json", "stabilityai/sd-vae-ft-mse", "31f26fdeee1355a5c34592e401dd41e45d25a493", 547, "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e"),
        ("models/sd-vae/diffusion_pytorch_model.bin", "stabilityai/sd-vae-ft-mse", "31f26fdeee1355a5c34592e401dd41e45d25a493", 334707217, "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc"),
        ("models/whisper/config.json", "openai/whisper-tiny", "169d4a4341b33bc18d8881c4b69c2e104e1cc0af", 1983, "ffdccec4f3211f4c63310f2b7098f309fe70f3952cedc5e4d11e43f5b2379b98"),
        ("models/whisper/pytorch_model.bin", "openai/whisper-tiny", "169d4a4341b33bc18d8881c4b69c2e104e1cc0af", 151095027, "9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d"),
        ("models/whisper/preprocessor_config.json", "openai/whisper-tiny", "169d4a4341b33bc18d8881c4b69c2e104e1cc0af", 184990, "9b5cd03a36fbb8a627c64d98a5b5b126ead95a77720723944487311f0110b666"),
        ("models/dwpose/dw-ll_ucoco_384.pth", "yzd-v/DWPose", "1a7144101628d69ee7a3768d1ee3a094070dc388", 406878486, "0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07"),
        ("models/syncnet/latentsync_syncnet.pt", "ByteDance/LatentSync", "405eda8eab9f65c1a6e0c292a5dee5a08089e2ae", 1488019828, "38fa63bad3ed2332f647c40a5dc616cb0e233db8579f698f62af4c41965c4da5"),
        ("models/face-parse-bisent/79999_iter.pth", "ManyOtherFunctions/face-parse-bisent", "0073b233a5a3c4b1377d4dbf49245017938a72b5", 53289463, "468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567"),
        ("models/face-parse-bisent/resnet18-5c106cde.pth", "ManyOtherFunctions/face-parse-bisent", "0073b233a5a3c4b1377d4dbf49245017938a72b5", 46827520, "5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8"),
    }
    assert actual == expected
    assert len(records) == len(expected) == 11


def _run_model_verifier(tmp_path: Path, expected: list[dict], manifest: list[dict], files: dict[str, bytes]):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    repository = tmp_path / "MuseTalk"
    repository.mkdir(parents=True)
    for relative_path, contents in files.items():
        path = repository / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    expected_path = tmp_path / "expected.json"
    manifest_path = repository / "models" / "model-hashes.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    command = (
        ". $env:PHASE8_VERIFY_SCRIPT; "
        "$expected = @(Get-Content -LiteralPath $env:PHASE8_EXPECTED -Raw | ConvertFrom-Json); "
        "Assert-MuseTalkModelFiles -Repository $env:PHASE8_REPOSITORY "
        "-ManifestPath $env:PHASE8_MANIFEST -ExpectedModels $expected"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PHASE8_VERIFY_SCRIPT": str(SCRIPTS / "musetalk_model_verification.ps1"),
            "PHASE8_EXPECTED": str(expected_path),
            "PHASE8_REPOSITORY": str(repository),
            "PHASE8_MANIFEST": str(manifest_path),
        }
    )
    return subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=environment,
    )


def test_model_verifier_rejects_manifest_drift_escape_and_joint_tampering(tmp_path):
    original = b"abc"
    original_hash = hashlib.sha256(original).hexdigest()
    expected = [{
        "path": "models/test.bin",
        "source": "test.bin",
        "repository": "example/models",
        "revision": "a" * 40,
        "bytes": len(original),
        "sha256": original_hash,
    }]
    manifest = [{key: value for key, value in expected[0].items() if key != "source"}]
    assert _run_model_verifier(tmp_path / "valid", expected, manifest, {"models/test.bin": original}).returncode == 0

    cases = []
    cases.append(("missing", [], {"models/test.bin": original}))
    cases.append(("duplicate", manifest + manifest, {"models/test.bin": original}))
    cases.append(("extra", manifest + [{**manifest[0], "path": "models/extra.bin"}], {"models/test.bin": original, "models/extra.bin": b"abc"}))
    cases.append(("escape", [{**manifest[0], "path": "../outside.bin"}], {"models/test.bin": original}))
    tampered = b"xyz"
    tampered_hash = hashlib.sha256(tampered).hexdigest()
    cases.append(("joint-tamper", [{**manifest[0], "sha256": tampered_hash}], {"models/test.bin": tampered}))
    for name, changed_manifest, files in cases:
        result = _run_model_verifier(tmp_path / name, expected, changed_manifest, files)
        assert result.returncode != 0, f"{name} was accepted"
