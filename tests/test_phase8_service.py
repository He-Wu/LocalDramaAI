import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_services.musetalk import service
from ai_services.musetalk.service import (
    GenerateRequest,
    MuseTalkSettings,
    build_musetalk_command,
    build_runtime_health,
    create_job_directory,
    expected_musetalk_output,
    normalize_audio,
    normalize_video,
    probe_audio,
    probe_video,
    run_generation,
    run_musetalk_command,
    validate_generate_request,
    write_inference_config,
)
from app.services.media_probe import AVInfo, AudioStreamInfo, VideoStreamInfo


REQUIRED_MODEL_PATHS = (
    "models/musetalkV15/unet.pth",
    "models/musetalkV15/musetalk.json",
    "models/sd-vae/config.json",
    "models/sd-vae/diffusion_pytorch_model.bin",
    "models/whisper/config.json",
    "models/whisper/pytorch_model.bin",
    "models/whisper/preprocessor_config.json",
    "models/dwpose/dw-ll_ucoco_384.pth",
    "models/syncnet/latentsync_syncnet.pt",
    "models/face-parse-bisent/79999_iter.pth",
    "models/face-parse-bisent/resnet18-5c106cde.pth",
)


def _ffmpeg(path: Path, *arguments: str) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *arguments,
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        shell=False,
    )
    return path


def _video(path: Path, *, seconds: float = 0.24) -> Path:
    return _ffmpeg(
        path,
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=320x240:rate=10:duration={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-an",
    )


def _audio(path: Path, *, seconds: float = 0.2) -> Path:
    return _ffmpeg(
        path,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=22050:duration={seconds}",
        "-c:a",
        "pcm_s16le",
        "-ac",
        "2",
        "-vn",
    )


def _safe_job_root(tmp_path: Path) -> Path:
    token = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return (Path.cwd() / ".pytest-musetalk-jobs" / token).resolve()


@pytest.fixture(autouse=True)
def _cleanup_safe_job_root(tmp_path):
    job_root = _safe_job_root(tmp_path)
    yield
    shutil.rmtree(job_root, ignore_errors=True)
    try:
        job_root.parent.rmdir()
    except OSError:
        pass


def _settings(tmp_path: Path) -> MuseTalkSettings:
    ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
    return MuseTalkSettings(
        repo_path=tmp_path / "MuseTalk",
        python_executable=Path(sys.executable),
        ffmpeg_bin=ffmpeg.parent,
        job_root=_safe_job_root(tmp_path),
        timeout_seconds=5.0,
        repo_commit="test-commit",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(tmp_path: Path, **overrides) -> GenerateRequest:
    video = tmp_path / "source.mp4"
    audio = tmp_path / "source.wav"
    if not video.exists():
        video.write_bytes(b"video")
    if not audio.exists():
        audio.write_bytes(b"audio")
    values = {
        "video_path": str(video.resolve()),
        "audio_path": str(audio.resolve()),
        "output_dir": str((tmp_path / "published").resolve()),
        "target_duration": 0.4,
        "batch_size": 4,
        "use_float16": True,
        "metadata": {
            "project_id": "project-default",
            "shot_id": "shot-default",
            "input_assets": ["video-asset", "audio-asset"],
        },
    }
    values.update(overrides)
    return GenerateRequest(**values)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"video_path": "relative.mp4"}, "video_path must be absolute"),
        ({"audio_path": ""}, "audio_path must not be empty"),
        ({"output_dir": "relative-output"}, "output_dir must be absolute"),
        ({"batch_size": 8}, "batch_size must be exactly 4"),
        ({"use_float16": False}, "use_float16 must be true"),
    ],
)
def test_generate_validation_is_strict(tmp_path, override, message):
    request = _request(tmp_path, **override)

    with pytest.raises(ValueError, match=message):
        validate_generate_request(request)


def test_generate_validation_rejects_missing_or_empty_inputs(tmp_path):
    missing = (tmp_path / "missing.mp4").resolve()
    request = _request(tmp_path, video_path=str(missing))
    with pytest.raises(FileNotFoundError):
        validate_generate_request(request)

    empty = (tmp_path / "empty.wav").resolve()
    empty.write_bytes(b"")
    request = _request(tmp_path, audio_path=str(empty))
    with pytest.raises(ValueError, match="empty"):
        validate_generate_request(request)


def test_generate_validation_requires_source_mp4_and_wav_paths(tmp_path):
    wrong_video = (tmp_path / "source.mkv").resolve()
    wrong_audio = (tmp_path / "source.mp3").resolve()
    wrong_video.write_bytes(b"video")
    wrong_audio.write_bytes(b"audio")

    with pytest.raises(ValueError, match="video_path must be an MP4"):
        validate_generate_request(_request(tmp_path, video_path=str(wrong_video)))
    with pytest.raises(ValueError, match="audio_path must be a WAV"):
        validate_generate_request(_request(tmp_path, audio_path=str(wrong_audio)))


@pytest.mark.parametrize(
    "invalid_duration",
    ["0.4", True, 1, 0.0, -1.0, float("inf"), float("nan")],
)
def test_generate_model_rejects_coerced_target_duration(tmp_path, invalid_duration):
    with pytest.raises(ValidationError):
        _request(tmp_path, target_duration=invalid_duration)


def test_generate_model_requires_metadata_to_be_an_object(tmp_path):
    with pytest.raises(ValidationError):
        _request(tmp_path, metadata=[{"project_id": "project-1"}])


@pytest.mark.parametrize(
    "metadata",
    [
        {"project_id": "project", "shot_id": "shot"},
        {
            "project_id": "project",
            "shot_id": "shot",
            "input_assets": ["video", "audio"],
            "unexpected": "value",
        },
        {"project_id": 7, "shot_id": "shot", "input_assets": ["video", "audio"]},
        {"project_id": "project", "shot_id": "shot", "input_assets": "video"},
        {"project_id": "project", "shot_id": "shot", "input_assets": ["video"]},
        {
            "project_id": "project",
            "shot_id": "shot",
            "input_assets": ["video", "audio", "extra"],
        },
        {"project_id": "project", "shot_id": "shot", "input_assets": ["video", 8]},
    ],
)
def test_generate_model_requires_exact_binding_metadata_shape(tmp_path, metadata):
    with pytest.raises(ValidationError):
        _request(tmp_path, metadata=metadata)


def test_generate_model_preserves_ordered_binding_metadata(tmp_path):
    metadata = {
        "project_id": "project",
        "shot_id": "shot",
        "input_assets": ["source-video", "dialogue-audio"],
    }

    request = _request(tmp_path, metadata=metadata)

    assert request.metadata == metadata
    assert request.metadata["input_assets"] == ["source-video", "dialogue-audio"]


def test_job_directories_and_published_names_are_unique(tmp_path):
    job_root = _safe_job_root(tmp_path)
    first = create_job_directory(job_root)
    second = create_job_directory(job_root)

    assert first != second
    assert first.parent == second.parent == job_root
    assert first.is_dir() and second.is_dir()


def test_yaml_is_one_safe_task_and_expected_output_is_locked(tmp_path):
    job_dir = create_job_directory(_safe_job_root(tmp_path))
    video = job_dir / "video.mp4"
    audio = job_dir / "audio.wav"
    task_path = write_inference_config(job_dir, video, audio)

    assert yaml.safe_load(task_path.read_text(encoding="utf-8")) == {
        "task_0": {"video_path": str(video), "audio_path": str(audio)}
    }
    assert expected_musetalk_output(job_dir / "results") == (
        job_dir / "results" / "v15" / "video_audio.mp4"
    )


def test_command_builder_returns_exact_official_arguments(tmp_path):
    settings = _settings(tmp_path)
    task = tmp_path / "task.yaml"
    results = tmp_path / "results"

    assert build_musetalk_command(settings, task, results) == [
        str(settings.python_executable),
        "-m",
        "scripts.inference",
        "--inference_config",
        str(task),
        "--result_dir",
        str(results),
        "--unet_model_path",
        "models/musetalkV15/unet.pth",
        "--unet_config",
        "models/musetalkV15/musetalk.json",
        "--version",
        "v15",
        "--use_float16",
        "--batch_size",
        "4",
        "--extra_margin",
        "10",
        "--parsing_mode",
        "jaw",
        "--left_cheek_width",
        "90",
        "--right_cheek_width",
        "90",
        "--ffmpeg_path",
        str(settings.ffmpeg_bin),
    ]


def test_service_lock_pins_official_musetalk_dependency_compatibility():
    lock_path = Path(service.__file__).with_name("requirements.lock.txt")
    pins = {
        line.strip()
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "pydantic==2.11.10" in pins
    assert "typer==0.15.4" in pins
    assert "rich==13.4.2" in pins
    assert "pydantic==2.12.5" not in pins


class _FakeChild:
    def __init__(self, *, returncode=0, output="ok", timeout=False, wait_error=None):
        self.returncode = returncode
        self.stdout = io.StringIO(output)
        self.timeout = timeout
        self.wait_error = wait_error
        self.killed = False
        self.waited = False
        self.communicate_calls = 0

    def communicate(self, timeout):
        self.communicate_calls += 1
        raise AssertionError("communicate() must not buffer unbounded child output")

    def wait(self, timeout=None):
        if self.wait_error is not None and not self.killed:
            raise self.wait_error
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("musetalk", timeout)
        self.waited = True
        return self.returncode

    def poll(self):
        return self.returncode if self.killed or self.waited else None

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_command_execution_uses_list_cwd_shell_false_and_bounded_log(tmp_path, monkeypatch):
    seen = {}

    def fake_spawn(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return _FakeChild(output="x" * 100)

    monkeypatch.setattr(service, "_spawn_child", fake_spawn)
    log = tmp_path / "musetalk.log"
    command = ["python", "-m", "scripts.inference"]
    run_musetalk_command(command, cwd=tmp_path, timeout=2, log_path=log)

    assert seen["command"] is command
    assert seen["cwd"] == str(tmp_path)
    assert seen["shell"] is False
    assert seen["text"] is True
    assert log.read_text(encoding="utf-8") == "x" * 100
    assert service.active_child is None


def test_command_execution_incrementally_keeps_only_fixed_log_tail(tmp_path, monkeypatch):
    output = "discard-me\n" * (service.MAX_LOG_CHARS // 5) + "final-tail"
    child = _FakeChild(output=output)
    monkeypatch.setattr(service, "_spawn_child", lambda *args, **kwargs: child)
    log = tmp_path / "bounded.log"

    run_musetalk_command(["python"], cwd=tmp_path, timeout=2, log_path=log)

    retained = log.read_text(encoding="utf-8")
    assert retained == output[-service.MAX_LOG_CHARS :]
    assert len(retained) == service.MAX_LOG_CHARS
    assert retained.endswith("final-tail")
    assert child.communicate_calls == 0


class _Cancelled(BaseException):
    pass


def test_command_execution_kills_child_and_flushes_log_on_cancellation(tmp_path, monkeypatch):
    child = _FakeChild(output="last output", wait_error=_Cancelled())
    monkeypatch.setattr(service, "_spawn_child", lambda *args, **kwargs: child)
    log = tmp_path / "cancelled.log"

    with pytest.raises(_Cancelled):
        run_musetalk_command(["python"], cwd=tmp_path, timeout=2, log_path=log)

    assert child.killed is True
    assert log.read_text(encoding="utf-8") == "last output"
    assert service.active_child is None


@pytest.mark.parametrize("mode", ["nonzero", "timeout", "spawn"])
def test_command_execution_rejects_child_failures(tmp_path, monkeypatch, mode):
    if mode == "nonzero":
        monkeypatch.setattr(service, "_spawn_child", lambda *args, **kwargs: _FakeChild(returncode=7, output="bad"))
        message = "exited with status 7"
    elif mode == "timeout":
        child = _FakeChild(timeout=True)
        monkeypatch.setattr(service, "_spawn_child", lambda *args, **kwargs: child)
        message = "timed out"
    else:
        def fail_spawn(*args, **kwargs):
            raise OSError("cannot spawn")

        monkeypatch.setattr(service, "_spawn_child", fail_spawn)
        message = "could not start"

    with pytest.raises(RuntimeError, match=message):
        run_musetalk_command(["python"], cwd=tmp_path, timeout=0.01, log_path=tmp_path / "run.log")
    assert service.active_child is None


def test_real_ffmpeg_normalizes_video_and_pads_audio_exactly(tmp_path):
    source_video = _video(tmp_path / "source.mp4")
    source_audio = _audio(tmp_path / "source.wav")
    target = 0.4
    normalized_video = normalize_video(source_video, tmp_path / "video.mp4", target)
    normalized_audio = normalize_audio(source_audio, tmp_path / "audio.wav", target)

    video = probe_video(normalized_video)
    audio = probe_audio(normalized_audio)
    assert (video.codec, video.pixel_format) == ("h264", "yuv420p")
    assert (video.width, video.height) == (640, 368)
    assert video.fps == pytest.approx(25.0)
    assert video.duration >= target - 0.04
    assert (audio.codec, audio.sample_rate, audio.channels) == ("pcm_s16le", 16000, 1)
    assert audio.duration == pytest.approx(target, abs=0.01)


def test_zero_exit_without_expected_output_is_failure_and_publishes_nothing(tmp_path):
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "source.wav").resolve()),
    )

    settings = _settings(tmp_path)
    with pytest.raises(RuntimeError, match="expected MuseTalk output"):
        run_generation(request, settings, command_runner=lambda *args, **kwargs: None)

    output = Path(request.output_dir)
    assert not list(output.glob("*.mp4"))
    assert not list(output.glob("*.manifest.json"))
    assert not list(settings.job_root.glob(".musetalk-job-*"))


def test_generation_cleans_job_when_tool_resolution_fails(tmp_path, monkeypatch):
    request = _request(tmp_path)

    def fail_resolution(*args, **kwargs):
        raise OSError("cannot inspect FFmpeg")

    monkeypatch.setattr(service, "_resolve_tool", fail_resolution)

    settings = _settings(tmp_path)
    with pytest.raises(OSError, match="cannot inspect FFmpeg"):
        run_generation(request, settings)

    assert not list(settings.job_root.glob(".musetalk-job-*"))


def test_happy_stub_creates_canonical_av_and_atomic_manifest(tmp_path):
    caller_metadata = {
        "project_id": "project-42",
        "shot_id": "shot-7",
        "input_assets": ["source-video-asset", "dialogue-audio-asset"],
    }
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "source.wav").resolve()),
        metadata=caller_metadata,
    )
    seen = {}

    def fake_runner(command, *, cwd, timeout, log_path):
        seen.update(command=command, cwd=cwd, timeout=timeout, log_path=log_path)
        task_path = Path(command[command.index("--inference_config") + 1])
        task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["task_0"]
        result_dir = Path(command[command.index("--result_dir") + 1])
        upstream = expected_musetalk_output(result_dir)
        upstream.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task["video_path"], upstream)

    settings = _settings(tmp_path)
    response = run_generation(request, settings, command_runner=fake_runner)
    output = Path(response["output_path"])
    manifest_path = Path(response["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    info = service.probe_av(output)

    assert output.is_file() and output.stat().st_size > 0
    assert manifest_path.is_file() and manifest_path.stat().st_size > 0
    assert (info.video.codec, info.video.pixel_format) == ("h264", "yuv420p")
    assert (info.video.width, info.video.height) == (640, 368)
    assert info.video.fps == pytest.approx(25.0)
    assert (info.audio.codec, info.audio.channels) == ("aac", 1)
    assert info.duration >= request.target_duration - 0.04
    assert abs(info.video.duration - info.audio.duration) <= 0.08
    assert seen["cwd"] == str(settings.repo_path)
    assert manifest["provider"] == "musetalk"
    assert manifest["provider_version"] == "test-commit"
    assert manifest["model"] == "musetalk-v1.5"
    assert manifest["workflow"] == "musetalk_lipsync"
    assert manifest["batch_size"] == 4 and manifest["fp16"] is True
    assert manifest["metadata"] == caller_metadata
    assert manifest["metadata"]["project_id"] == "project-42"
    assert manifest["metadata"]["shot_id"] == "shot-7"
    assert manifest["metadata"]["input_assets"] == [
        "source-video-asset",
        "dialogue-audio-asset",
    ]
    assert manifest["target_duration"] == request.target_duration
    source_video = Path(request.video_path).resolve()
    source_audio = Path(request.audio_path).resolve()
    assert manifest["source_video"] == {
        "path": str(source_video),
        "sha256": _sha256(source_video),
    }
    assert manifest["source_audio"] == {
        "path": str(source_audio),
        "sha256": _sha256(source_audio),
    }
    assert (
        manifest["sha256"]
        == manifest["output_sha256"]
        == response["sha256"]
        == _sha256(output)
    )
    assert Path(manifest["output_path"]).is_absolute()
    assert set(manifest["inference_config"]) == {"task_0"}
    assert Path(manifest["inference_config"]["task_0"]["video_path"]).name == "video.mp4"
    assert Path(manifest["inference_config"]["task_0"]["audio_path"]).name == "audio.wav"
    assert manifest["command"] == seen["command"]
    assert not list(settings.job_root.glob(".musetalk-job-*"))
    assert not list(Path(request.output_dir).glob("*.tmp.mp4"))
    assert not list(Path(request.output_dir).glob("*.tmp.json"))


def test_generation_passes_resolved_path_ffmpeg_parent_to_official_cli(tmp_path):
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "source.wav").resolve()),
    )
    settings = _settings(tmp_path)
    settings = MuseTalkSettings(
        repo_path=settings.repo_path,
        python_executable=settings.python_executable,
        ffmpeg_bin=tmp_path / "configured-but-missing",
        job_root=settings.job_root,
        timeout_seconds=settings.timeout_seconds,
        repo_commit=settings.repo_commit,
    )
    resolved_parent = Path(shutil.which("ffmpeg") or "ffmpeg").parent
    seen = {}

    def fake_runner(command, **kwargs):
        seen["command"] = command
        task_path = Path(command[command.index("--inference_config") + 1])
        task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["task_0"]
        result_dir = Path(command[command.index("--result_dir") + 1])
        upstream = expected_musetalk_output(result_dir)
        upstream.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task["video_path"], upstream)

    run_generation(request, settings, command_runner=fake_runner)

    command = seen["command"]
    assert Path(command[command.index("--ffmpeg_path") + 1]) == resolved_parent


@pytest.mark.parametrize(
    "job_root",
    [
        "relative/jobs",
        "E:/LocalDramaAI/jobs with spaces",
        "E:/LocalDramaAI/jobs&whoami",
        "E:/LocalDramaAI/jobs%TEMP%",
        "E:/LocalDramaAI/jobs;unsafe",
    ],
)
def test_settings_rejects_non_shell_safe_job_root(monkeypatch, job_root):
    monkeypatch.setenv("LOCALDRAMA_MUSETALK_JOB_ROOT", job_root)

    with pytest.raises(ValueError, match="job root"):
        MuseTalkSettings.from_env()


def test_settings_accepts_absolute_shell_safe_job_root(monkeypatch):
    job_root = Path("E:/LocalDramaAI/musetalk-jobs")
    monkeypatch.setenv("LOCALDRAMA_MUSETALK_JOB_ROOT", str(job_root))

    settings = MuseTalkSettings.from_env()

    assert settings.job_root == job_root


def test_generation_hides_unsafe_caller_paths_from_official_cli(tmp_path):
    unsafe_parent = tmp_path / "caller path & data"
    unsafe_parent.mkdir()
    source_video = _video(unsafe_parent / "source video & portrait.mp4")
    source_audio = _audio(unsafe_parent / "dialogue audio & voice.wav")
    output_dir = (unsafe_parent / "published output & final").resolve()
    request = _request(
        tmp_path,
        video_path=str(source_video.resolve()),
        audio_path=str(source_audio.resolve()),
        output_dir=str(output_dir),
    )
    job_root = (Path.cwd() / ".pytest-musetalk-jobs" / uuid.uuid4().hex).resolve()
    base_settings = _settings(tmp_path)
    settings_values = vars(base_settings).copy()
    settings_values["job_root"] = job_root
    settings = SimpleNamespace(**settings_values)
    seen = {}

    def fake_runner(command, *, cwd, timeout, log_path):
        task_path = Path(command[command.index("--inference_config") + 1])
        task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["task_0"]
        result_dir = Path(command[command.index("--result_dir") + 1])
        upstream = expected_musetalk_output(result_dir)
        upstream.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task["video_path"], upstream)
        seen.update(
            command=command,
            task=task,
            task_path=task_path,
            result_dir=result_dir,
            upstream=upstream,
            log_path=Path(log_path),
        )

    try:
        response = run_generation(request, settings, command_runner=fake_runner)
        manifest = json.loads(Path(response["manifest_path"]).read_text(encoding="utf-8"))

        official_paths = [
            seen["task_path"],
            seen["result_dir"],
            seen["upstream"],
            seen["log_path"],
            Path(seen["task"]["video_path"]),
            Path(seen["task"]["audio_path"]),
        ]
        assert all(path.is_relative_to(job_root) for path in official_paths)
        assert all(" " not in str(path) and "&" not in str(path) for path in official_paths)
        for caller_path in (source_video.resolve(), source_audio.resolve(), output_dir):
            assert all(str(caller_path) not in str(argument) for argument in seen["command"])
            assert all(str(caller_path) != value for value in seen["task"].values())

        assert Path(response["output_path"]).parent == output_dir
        assert manifest["source_video"]["path"] == str(source_video.resolve())
        assert manifest["source_audio"]["path"] == str(source_audio.resolve())
        assert manifest["output_path"] == response["output_path"]
        assert manifest["output_sha256"] == _sha256(Path(response["output_path"]))
        assert not list(job_root.glob(".musetalk-job-*"))
    finally:
        shutil.rmtree(job_root, ignore_errors=True)
        try:
            job_root.parent.rmdir()
        except OSError:
            pass


def test_invalid_upstream_never_leaves_partial_publication(tmp_path):
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "source.wav").resolve()),
    )

    def invalid_runner(command, **kwargs):
        result_dir = Path(command[command.index("--result_dir") + 1])
        upstream = expected_musetalk_output(result_dir)
        upstream.parent.mkdir(parents=True, exist_ok=True)
        upstream.write_bytes(b"not a video")

    with pytest.raises(RuntimeError, match="canonicalize"):
        run_generation(request, _settings(tmp_path), command_runner=invalid_runner)

    output = Path(request.output_dir)
    assert not list(output.glob("*.mp4"))
    assert not list(output.glob("*.manifest.json"))
    assert not list(output.glob("*.tmp.*"))


def test_canonical_probe_rejects_wrong_audio_sample_rate():
    info = AVInfo(
        video=VideoStreamInfo("h264", "yuv420p", 640, 368, 25.0, 10, 0.4),
        audio=AudioStreamInfo("aac", 48000, 1, 0.4),
        duration=0.4,
    )

    with pytest.raises(RuntimeError, match="locked A/V profile"):
        service._validate_canonical(info, 0.4)


def _ready_layout(tmp_path: Path) -> MuseTalkSettings:
    settings = MuseTalkSettings(
        repo_path=tmp_path / "MuseTalk",
        python_executable=tmp_path / "env" / "Scripts" / "python.exe",
        ffmpeg_bin=tmp_path / "ffmpeg" / "bin",
        job_root=_safe_job_root(tmp_path),
        timeout_seconds=5,
        repo_commit="abc123",
    )
    base_paths = (
        settings.python_executable,
        settings.ffmpeg_bin / "ffmpeg.exe",
        settings.ffmpeg_bin / "ffprobe.exe",
        settings.repo_path / "scripts" / "inference.py",
    )
    for path in (*base_paths, *(settings.repo_path / relative for relative in REQUIRED_MODEL_PATHS)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return settings


def test_health_reports_ready_and_degraded_truthfully(tmp_path, monkeypatch):
    settings = _ready_layout(tmp_path)
    monkeypatch.setattr(
        service,
        "check_cuda",
        lambda *args, **kwargs: {"available": True, "device": "GPU"},
    )

    ready = build_runtime_health(settings)
    assert ready["status"] == "ONLINE" and ready["ready"] is True
    assert ready["cuda"]["available"] is True
    assert ready["persistent_model"] is False

    inference = settings.repo_path / "scripts" / "inference.py"
    inference.unlink()
    missing_repo = build_runtime_health(settings)
    assert missing_repo["status"] == "DEGRADED" and missing_repo["ready"] is False
    assert missing_repo["checks"]["repo"] is False
    inference.write_bytes(b"x")

    (settings.repo_path / "models" / "musetalkV15" / "unet.pth").unlink()
    degraded = build_runtime_health(settings)
    assert degraded["status"] == "DEGRADED" and degraded["ready"] is False
    assert degraded["checks"]["model"] is False


@pytest.mark.parametrize("relative_path", REQUIRED_MODEL_PATHS)
def test_health_requires_every_official_model(tmp_path, monkeypatch, relative_path):
    settings = _ready_layout(tmp_path)
    monkeypatch.setattr(service, "check_cuda", lambda *args, **kwargs: {"available": True, "device": "GPU"})
    missing = settings.repo_path / relative_path
    missing.unlink()

    health = build_runtime_health(settings)

    assert health["status"] == "DEGRADED"
    assert health["ready"] is False
    assert health["checks"]["model"] is False
    assert relative_path in health["missing_models"]


def test_health_endpoint_serializes_runtime_health(monkeypatch):
    expected = {
        "status": "ONLINE",
        "ready": True,
        "busy": False,
        "active_child": False,
        "persistent_model": False,
        "checks": {"repo": True, "cuda": True},
    }
    monkeypatch.setattr(service, "build_runtime_health", lambda: expected)

    with TestClient(service.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == expected


def test_generate_endpoint_returns_422_for_strict_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "generation_lock", asyncio.Lock())
    payload = _request(tmp_path).model_dump()
    payload["target_duration"] = "0.4"

    with TestClient(service.app) as client:
        response = client.post("/generate", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "target_duration"]


def test_generate_endpoint_maps_runtime_failure_to_500(tmp_path, monkeypatch):
    request = _request(tmp_path)
    monkeypatch.setattr(service, "generation_lock", asyncio.Lock())
    monkeypatch.setattr(
        service,
        "run_generation",
        lambda request: (_ for _ in ()).throw(RuntimeError("official CLI failed")),
    )

    with TestClient(service.app) as client:
        response = client.post("/generate", json=request.model_dump())

    assert response.status_code == 500
    assert response.json() == {"detail": "MuseTalk generation failed: official CLI failed"}


def test_generate_endpoint_success_runs_real_normalize_and_canonical_mux(tmp_path, monkeypatch):
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "endpoint-source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "endpoint-source.wav").resolve()),
        metadata={
            "project_id": "endpoint-project",
            "shot_id": "endpoint-shot",
            "input_assets": ["video", "audio"],
        },
    )
    settings = _settings(tmp_path)

    def fake_cli(command, **kwargs):
        task_path = Path(command[command.index("--inference_config") + 1])
        task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["task_0"]
        upstream = expected_musetalk_output(Path(command[command.index("--result_dir") + 1]))
        upstream.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task["video_path"], upstream)

    monkeypatch.setattr(service, "generation_lock", asyncio.Lock())
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "run_musetalk_command", fake_cli)

    with TestClient(service.app) as client:
        response = client.post("/generate", json=request.model_dump())

    assert response.status_code == 200
    payload = response.json()
    output = Path(payload["output_path"])
    manifest = Path(payload["manifest_path"])
    assert output.is_file() and output.stat().st_size > 0
    assert manifest.is_file() and manifest.stat().st_size > 0
    assert service.probe_av(output).audio.codec == "aac"
    assert json.loads(manifest.read_text(encoding="utf-8"))["metadata"] == request.metadata


@pytest.mark.asyncio
async def test_generate_endpoint_serializes_concurrent_requests(tmp_path, monkeypatch):
    request = _request(tmp_path)
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "calls": 0}

    def fake_generation(request):
        with state_lock:
            state["active"] += 1
            state["calls"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            call = state["calls"]
        time.sleep(0.05)
        with state_lock:
            state["active"] -= 1
        return {"output_path": f"output-{call}.mp4", "manifest_path": f"manifest-{call}.json"}

    monkeypatch.setattr(service, "generation_lock", asyncio.Lock())
    monkeypatch.setattr(service, "run_generation", fake_generation)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.post("/generate", json=request.model_dump()),
            client.post("/generate", json=request.model_dump()),
        )

    assert first.status_code == second.status_code == 200
    assert state == {"active": 0, "maximum": 1, "calls": 2}
    assert first.json()["output_path"] != second.json()["output_path"]


@pytest.mark.asyncio
async def test_cancelled_generate_keeps_lock_until_worker_finishes(tmp_path, monkeypatch):
    request = _request(tmp_path)
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "calls": 0}

    def blocking_generation(request):
        with state_lock:
            state["active"] += 1
            state["calls"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            call = state["calls"]
            (first_started if call == 1 else second_started).set()
        assert release.wait(timeout=2)
        with state_lock:
            state["active"] -= 1
        return {"output_path": f"output-{call}.mp4", "manifest_path": f"manifest-{call}.json"}

    monkeypatch.setattr(service, "generation_lock", asyncio.Lock())
    monkeypatch.setattr(service, "active_generation_tasks", set(), raising=False)
    monkeypatch.setattr(service, "active_child", None)
    monkeypatch.setattr(service, "run_generation", blocking_generation)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/generate", json=request.model_dump()))
        assert await asyncio.to_thread(first_started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        unload_while_cancelled = await client.post("/unload")
        second = asyncio.create_task(client.post("/generate", json=request.model_dump()))
        overlapped = await asyncio.to_thread(second_started.wait, 0.2)
        try:
            observed = (unload_while_cancelled.status_code, overlapped, state["maximum"])
        finally:
            release.set()
            second_response = await second

        await asyncio.sleep(0)
        unload_after_completion = await client.post("/unload")

    assert observed == (409, False, 1)
    assert second_response.status_code == 200
    assert state == {"active": 0, "maximum": 1, "calls": 2}
    assert unload_after_completion.status_code == 200


def test_unload_is_truthful_when_idle_or_active(monkeypatch):
    client = TestClient(service.app)
    monkeypatch.setattr(service, "active_child", None)
    idle = client.post("/unload")
    assert idle.status_code == 200
    assert idle.json() == {
        "status": "UNLOADED",
        "unloaded": True,
        "persistent_model": False,
        "active_child": False,
    }

    monkeypatch.setattr(service, "active_child", object())
    active = client.post("/unload")
    assert active.status_code == 409
    assert "active" in active.json()["detail"].lower()
