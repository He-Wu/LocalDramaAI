import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

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


def _settings(tmp_path: Path) -> MuseTalkSettings:
    ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
    return MuseTalkSettings(
        repo_path=tmp_path / "MuseTalk",
        python_executable=Path(sys.executable),
        ffmpeg_bin=ffmpeg.parent,
        timeout_seconds=5.0,
        repo_commit="test-commit",
    )


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
    }
    values.update(overrides)
    return GenerateRequest(**values)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"video_path": "relative.mp4"}, "video_path must be absolute"),
        ({"audio_path": ""}, "audio_path must not be empty"),
        ({"output_dir": "relative-output"}, "output_dir must be absolute"),
        ({"target_duration": 0}, "target_duration must be positive"),
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


def test_job_directories_and_published_names_are_unique(tmp_path):
    output_dir = tmp_path / "output"
    first = create_job_directory(output_dir)
    second = create_job_directory(output_dir)

    assert first != second
    assert first.parent == second.parent == output_dir
    assert first.is_dir() and second.is_dir()


def test_yaml_is_one_safe_task_and_expected_output_is_locked(tmp_path):
    job_dir = create_job_directory(tmp_path / "output")
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


class _FakeChild:
    def __init__(self, *, returncode=0, output="ok", timeout=False):
        self.returncode = returncode
        self.output = output
        self.timeout = timeout
        self.killed = False

    def communicate(self, timeout):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("musetalk", timeout)
        return self.output, None

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

    with pytest.raises(RuntimeError, match="expected MuseTalk output"):
        run_generation(request, _settings(tmp_path), command_runner=lambda *args, **kwargs: None)

    output = Path(request.output_dir)
    assert not list(output.glob("*.mp4"))
    assert not list(output.glob("*.manifest.json"))
    assert not list(output.glob(".musetalk-job-*"))


def test_happy_stub_creates_canonical_av_and_atomic_manifest(tmp_path):
    request = _request(
        tmp_path,
        video_path=str(_video(tmp_path / "source.mp4").resolve()),
        audio_path=str(_audio(tmp_path / "source.wav").resolve()),
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

    response = run_generation(request, _settings(tmp_path), command_runner=fake_runner)
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
    assert seen["cwd"] == str(_settings(tmp_path).repo_path)
    assert manifest["provider"] == "musetalk"
    assert manifest["provider_version"] == "test-commit"
    assert manifest["model"] == "musetalk-v1.5"
    assert manifest["workflow"] == "musetalk_lipsync"
    assert manifest["batch_size"] == 4 and manifest["fp16"] is True
    assert manifest["sha256"] == response["sha256"]
    assert manifest["command"] == seen["command"]
    assert not list(Path(request.output_dir).glob(".musetalk-job-*"))
    assert not list(Path(request.output_dir).glob("*.tmp.mp4"))
    assert not list(Path(request.output_dir).glob("*.tmp.json"))


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
        timeout_seconds=5,
        repo_commit="abc123",
    )
    for path in (
        settings.python_executable,
        settings.ffmpeg_bin / "ffmpeg.exe",
        settings.ffmpeg_bin / "ffprobe.exe",
        settings.repo_path / "scripts" / "inference.py",
        settings.repo_path / "models" / "musetalkV15" / "unet.pth",
        settings.repo_path / "models" / "musetalkV15" / "musetalk.json",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return settings


def test_health_reports_ready_and_degraded_truthfully(tmp_path, monkeypatch):
    settings = _ready_layout(tmp_path)
    monkeypatch.setattr(service, "check_cuda", lambda *args, **kwargs: {"available": True, "device": "GPU"})

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
