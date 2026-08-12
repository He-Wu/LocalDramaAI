"""Loopback FastAPI boundary for one isolated official MuseTalk 1.5 child.

The service process never imports MuseTalk or Torch. Each request normalizes
caller-owned media into a disposable job directory, launches the official CLI,
then validates and atomically publishes one canonical audiovisual MP4 and its
manifest. The child process is the model lifetime boundary.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

from ctypes import wintypes

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
)

from app.services.media_probe import AVInfo, probe_av


DEFAULT_REPO = Path("E:/LocalDramaAI/MuseTalk")
DEFAULT_PYTHON = Path("E:/LocalDramaAI/env-musetalk/Scripts/python.exe")
DEFAULT_FFMPEG_BIN = Path("E:/LocalDramaAI/ffmpeg/bin")
DEFAULT_JOB_ROOT = Path("E:/LocalDramaAI/musetalk-jobs")
DEFAULT_TIMEOUT_SECONDS = 1800.0
MAX_LOG_CHARS = 1_000_000
CHILD_STOP_TIMEOUT_SECONDS = 10.0
SHELL_SAFE_JOB_ROOT = re.compile(
    r"^[A-Za-z]:[\\/](?:[A-Za-z0-9_.-]+(?:[\\/]|$))+$"
)
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


def validate_job_root(job_root: Path) -> Path:
    """Require a service-owned Windows path safe for upstream os.system calls."""
    path = Path(job_root)
    serialized = str(path)
    if not path.is_absolute():
        raise ValueError("MuseTalk job root must be absolute")
    if any(part in {".", ".."} for part in path.parts[1:]):
        raise ValueError("MuseTalk job root must not contain relative path segments")
    if SHELL_SAFE_JOB_ROOT.fullmatch(serialized) is None:
        raise ValueError(
            "MuseTalk job root must contain only shell-safe ASCII path characters"
        )
    return path


@dataclass(frozen=True)
class MuseTalkSettings:
    repo_path: Path
    python_executable: Path
    ffmpeg_bin: Path
    job_root: Path
    timeout_seconds: float
    repo_commit: str

    @classmethod
    def from_env(cls) -> "MuseTalkSettings":
        timeout = float(os.environ.get("LOCALDRAMA_MUSETALK_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LOCALDRAMA_MUSETALK_TIMEOUT must be positive")
        return cls(
            repo_path=Path(os.environ.get("LOCALDRAMA_MUSETALK_REPO", str(DEFAULT_REPO))),
            python_executable=Path(os.environ.get("LOCALDRAMA_MUSETALK_PYTHON", str(DEFAULT_PYTHON))),
            ffmpeg_bin=Path(os.environ.get("LOCALDRAMA_MUSETALK_FFMPEG_BIN", str(DEFAULT_FFMPEG_BIN))),
            job_root=validate_job_root(
                Path(os.environ.get("LOCALDRAMA_MUSETALK_JOB_ROOT", str(DEFAULT_JOB_ROOT)))
            ),
            timeout_seconds=timeout,
            repo_commit=os.environ.get("LOCALDRAMA_MUSETALK_REPO_COMMIT", "unlocked").strip() or "unlocked",
        )


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_path: str
    audio_path: str
    output_dir: str
    target_duration: StrictFloat = Field(gt=0, allow_inf_nan=False)
    batch_size: StrictInt = 4
    use_float16: StrictBool = True
    metadata: dict[str, Any]

    @field_validator("target_duration", mode="before")
    @classmethod
    def _require_float_duration(cls, value: Any) -> Any:
        if type(value) is not float:
            raise ValueError("target_duration must be a float")
        return value

    @field_validator("metadata")
    @classmethod
    def _validate_binding_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        required_keys = ("project_id", "shot_id", "input_assets")
        if set(value) != set(required_keys):
            raise ValueError("metadata must contain only project_id, shot_id, and input_assets")
        for key in ("project_id", "shot_id"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"metadata {key} must be a nonempty string")
        input_assets = value["input_assets"]
        if (
            not isinstance(input_assets, list)
            or len(input_assets) != 2
            or any(
                not isinstance(asset, str) or not asset.strip()
                for asset in input_assets
            )
        ):
            raise ValueError(
                "metadata input_assets must contain source-video and dialogue-audio IDs in order"
            )
        return {
            "project_id": value["project_id"],
            "shot_id": value["shot_id"],
            "input_assets": list(input_assets),
        }


@dataclass(frozen=True)
class VideoProbe:
    codec: str
    pixel_format: str
    width: int
    height: int
    fps: float
    duration: float
    frames: int | None


@dataclass(frozen=True)
class AudioProbe:
    codec: str
    sample_rate: int
    channels: int
    duration: float


generation_lock = asyncio.Lock()
active_generation_tasks: set[asyncio.Task[dict[str, Any]]] = set()
active_child: Any | None = None
app = FastAPI(title="LocalDramaAI MuseTalk", version="1.5")


def get_settings() -> MuseTalkSettings:
    """Load settings without checking external paths at import time."""
    return MuseTalkSettings.from_env()


def _strict_absolute_path(value: str, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return path


def validate_generate_request(request: GenerateRequest) -> None:
    video = _strict_absolute_path(request.video_path, "video_path")
    audio = _strict_absolute_path(request.audio_path, "audio_path")
    output_dir = _strict_absolute_path(request.output_dir, "output_dir")
    if video.suffix.lower() != ".mp4":
        raise ValueError("video_path must be an MP4 file")
    if audio.suffix.lower() != ".wav":
        raise ValueError("audio_path must be a WAV file")
    for label, path in (("video_path", video), ("audio_path", audio)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {path}")
        if path.stat().st_size <= 0:
            raise ValueError(f"{label} is empty: {path}")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"output_dir is not a directory: {output_dir}")
    if not math.isfinite(float(request.target_duration)) or request.target_duration <= 0:
        raise ValueError("target_duration must be positive")
    if isinstance(request.batch_size, bool) or request.batch_size != 4:
        raise ValueError("batch_size must be exactly 4")
    if request.use_float16 is not True:
        raise ValueError("use_float16 must be true")


def create_job_directory(job_root: Path) -> Path:
    job_root = validate_job_root(job_root)
    job_root.mkdir(parents=True, exist_ok=True)
    if not job_root.is_dir():
        raise RuntimeError(f"MuseTalk job root is not a directory: {job_root}")
    return Path(tempfile.mkdtemp(prefix=".musetalk-job-", dir=str(job_root)))


def write_inference_config(job_dir: Path, video_path: Path, audio_path: Path) -> Path:
    job_dir = Path(job_dir)
    task_path = job_dir / "task.yaml"
    payload = {"task_0": {"video_path": str(video_path), "audio_path": str(audio_path)}}
    task_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    loaded = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    if loaded != payload:
        raise RuntimeError("MuseTalk task YAML failed safe round-trip validation")
    return task_path


def expected_musetalk_output(result_dir: Path) -> Path:
    return Path(result_dir) / "v15" / "video_audio.mp4"


def build_musetalk_command(
    settings: MuseTalkSettings,
    inference_config: Path,
    result_dir: Path,
    *,
    ffmpeg_bin: Path | None = None,
) -> list[str]:
    return [
        str(settings.python_executable),
        "-m",
        "scripts.inference",
        "--inference_config",
        str(inference_config),
        "--result_dir",
        str(result_dir),
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
        str(ffmpeg_bin or settings.ffmpeg_bin),
    ]


def _spawn_child(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(command, **kwargs)


class _DirectChildOwner:
    """Last-resort owner used only when no OS process-tree primitive exists."""

    def stop(self, child: Any, timeout: float) -> None:
        try:
            running = child.poll() is None
        except Exception:
            running = True
        if running:
            try:
                child.kill()
            except Exception as exc:
                raise RuntimeError(f"failed to terminate MuseTalk child: {exc}") from exc
        try:
            child.wait(timeout=timeout)
        except Exception as exc:
            raise RuntimeError(f"MuseTalk child did not stop within {timeout:g} seconds") from exc
        if child.poll() is None:
            raise RuntimeError("MuseTalk child remains active after termination")


class _PosixProcessGroupOwner:
    def __init__(self, child: Any) -> None:
        self._process_group = int(child.pid)

    def stop(self, child: Any, timeout: float) -> None:
        try:
            os.killpg(self._process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise RuntimeError(f"failed to terminate MuseTalk process group: {exc}") from exc
        try:
            child.wait(timeout=timeout)
        except Exception as exc:
            raise RuntimeError(
                f"MuseTalk process group did not stop within {timeout:g} seconds"
            ) from exc


if os.name == "nt":
    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    class _JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]


    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL


    def _windows_error(action: str) -> RuntimeError:
        return RuntimeError(f"{action}: {ctypes.WinError(ctypes.get_last_error())}")


    class _WindowsJobOwner:
        """Own exactly one request's process tree through a Windows Job Object."""

        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

        def __init__(self, child: Any) -> None:
            self._handle = _kernel32.CreateJobObjectW(None, None)
            if not self._handle:
                raise _windows_error("could not create MuseTalk Job Object")
            try:
                limits = _JobObjectExtendedLimitInformation()
                limits.BasicLimitInformation.LimitFlags = (
                    self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                if not _kernel32.SetInformationJobObject(
                    self._handle,
                    self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                ):
                    raise _windows_error("could not configure MuseTalk Job Object")
                if not _kernel32.AssignProcessToJobObject(
                    self._handle, wintypes.HANDLE(int(child._handle))
                ):
                    raise _windows_error("could not assign MuseTalk child to Job Object")
            except Exception:
                _kernel32.CloseHandle(self._handle)
                self._handle = None
                _DirectChildOwner().stop(child, CHILD_STOP_TIMEOUT_SECONDS)
                raise

        def _active_processes(self) -> int:
            accounting = _JobObjectBasicAccountingInformation()
            if not _kernel32.QueryInformationJobObject(
                self._handle,
                self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                raise _windows_error("could not inspect MuseTalk Job Object")
            return int(accounting.ActiveProcesses)

        def stop(self, child: Any, timeout: float) -> None:
            if self._handle is None:
                raise RuntimeError("MuseTalk Job Object is already closed")
            error: BaseException | None = None
            try:
                if self._active_processes() > 0 and not _kernel32.TerminateJobObject(
                    self._handle, 1
                ):
                    raise _windows_error("could not terminate MuseTalk Job Object")
                deadline = time.monotonic() + timeout
                try:
                    child.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"MuseTalk child did not stop within {timeout:g} seconds"
                    ) from exc
                while self._active_processes() > 0:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"MuseTalk owned process tree did not stop within {timeout:g} seconds"
                        )
                    time.sleep(0.01)
            except BaseException as exc:
                error = exc
            finally:
                handle = self._handle
                self._handle = None
                if handle and not _kernel32.CloseHandle(handle) and error is None:
                    error = _windows_error("could not close MuseTalk Job Object")
            if error is not None:
                raise error


def _create_child_owner(child: Any) -> Any:
    if os.name == "nt" and hasattr(child, "_handle"):
        return _WindowsJobOwner(child)
    if os.name != "nt" and hasattr(child, "pid"):
        return _PosixProcessGroupOwner(child)
    return _DirectChildOwner()


def _windows_pid_is_active(pid: int) -> bool:
    """Return whether a Windows PID is active; used by cleanup verification."""
    if os.name != "nt":
        return False
    process = _kernel32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(_kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        _kernel32.CloseHandle(process)


def _write_bounded_log(log_path: Path, output: str | None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    bounded = (output or "")[-MAX_LOG_CHARS:]
    log_path.write_text(bounded, encoding="utf-8", errors="replace")


class _TailBuffer:
    """Thread-safe fixed-size character tail for a streaming child log."""

    def __init__(self, limit: int = MAX_LOG_CHARS) -> None:
        self._limit = limit
        self._value = ""
        self._lock = threading.Lock()

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._value = (self._value + chunk)[-self._limit :]

    def value(self) -> str:
        with self._lock:
            return self._value


def _drain_child_output(stream: Any, tail: _TailBuffer) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            tail.append(chunk)
    except Exception as exc:
        tail.append(f"\n[MuseTalk log reader failed: {exc}]\n")


def run_musetalk_command(
    command: list[str],
    *,
    cwd: Path | str,
    timeout: float,
    log_path: Path,
) -> None:
    """Run one child with explicit argv/cwd and retain only a bounded log."""
    global active_child
    if not isinstance(command, list) or not command:
        raise ValueError("MuseTalk command must be a nonempty argument list")
    child = None
    owner = None
    reader: threading.Thread | None = None
    tail = _TailBuffer()
    try:
        try:
            child = _spawn_child(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **({"start_new_session": True} if os.name != "nt" else {}),
            )
        except OSError as exc:
            raise RuntimeError(f"MuseTalk child could not start: {exc}") from exc
        owner = _create_child_owner(child)
        active_child = child
        if child.stdout is None:
            raise RuntimeError("MuseTalk child stdout pipe was not created")
        reader = threading.Thread(
            target=_drain_child_output,
            args=(child.stdout, tail),
            name="musetalk-log-reader",
            daemon=True,
        )
        reader.start()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"MuseTalk child timed out after {timeout:g} seconds") from exc
        if child.returncode != 0:
            raise RuntimeError(f"MuseTalk child exited with status {child.returncode}")
    finally:
        cleanup_error: BaseException | None = None
        if child is not None:
            try:
                (owner or _DirectChildOwner()).stop(
                    child, CHILD_STOP_TIMEOUT_SECONDS
                )
            except BaseException as exc:
                cleanup_error = exc
        if reader is not None:
            reader.join(timeout=5)
            if reader.is_alive() and child is not None and child.stdout is not None:
                try:
                    child.stdout.close()
                except Exception:
                    pass
                reader.join(timeout=1)
        if child is not None:
            _write_bounded_log(log_path, tail.value())
        active_child = None
        if cleanup_error is not None:
            raise cleanup_error


def _resolve_tool(bin_dir: Path, name: str) -> str | None:
    candidates = [Path(bin_dir) / f"{name}.exe", Path(bin_dir) / name]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return shutil.which(name)


def _run_ffmpeg(command: list[str], *, timeout: float = 120) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"FFmpeg timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"FFmpeg could not start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "")[-1200:]
        raise RuntimeError(f"FFmpeg failed with status {result.returncode}: {detail}")


def _probe_payload(path: Path, ffprobe_executable: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"media file is missing or empty: {path}")
    try:
        result = subprocess.run(
            [
                ffprobe_executable,
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                (
                    "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,"
                    "r_frame_rate,nb_frames,nb_read_frames,duration,sample_rate,channels:format=duration"
                ),
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot probe media file: {path}") from exc
    if result.returncode != 0:
        raise ValueError(f"invalid media file: {path}: {(result.stderr or '')[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned malformed metadata: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise ValueError(f"ffprobe returned incomplete metadata: {path}")
    return payload


def _positive_duration(stream: dict[str, Any], payload: dict[str, Any]) -> float:
    for value in (stream.get("duration"), (payload.get("format") or {}).get("duration")):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    raise ValueError("media stream has no positive duration")


def _stream(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    for item in payload["streams"]:
        if isinstance(item, dict) and item.get("codec_type") == kind:
            return item
    raise ValueError(f"media contains no playable {kind} stream")


def probe_video(path: Path, ffprobe_executable: str = "ffprobe") -> VideoProbe:
    payload = _probe_payload(Path(path), ffprobe_executable)
    stream = _stream(payload, "video")
    try:
        fps = float(Fraction(str(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))))
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("video stream metadata is incomplete") from exc
    if not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
        raise ValueError("video stream metadata is invalid")
    codec = str(stream.get("codec_name") or "").strip()
    pixel_format = str(stream.get("pix_fmt") or "").strip()
    if not codec or not pixel_format:
        raise ValueError("video codec metadata is incomplete")
    frames = None
    for key in ("nb_read_frames", "nb_frames"):
        try:
            candidate = int(stream.get(key))
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            frames = candidate
            break
    return VideoProbe(codec, pixel_format, width, height, fps, _positive_duration(stream, payload), frames)


def probe_audio(path: Path, ffprobe_executable: str = "ffprobe") -> AudioProbe:
    payload = _probe_payload(Path(path), ffprobe_executable)
    stream = _stream(payload, "audio")
    try:
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("audio stream metadata is incomplete") from exc
    codec = str(stream.get("codec_name") or "").strip()
    if not codec or sample_rate <= 0 or channels <= 0:
        raise ValueError("audio stream metadata is invalid")
    return AudioProbe(codec, sample_rate, channels, _positive_duration(stream, payload))


def _video_filter(target_duration: float) -> str:
    return (
        "scale=640:368:force_original_aspect_ratio=decrease,"
        "pad=640:368:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps=25,tpad=stop_mode=clone:stop_duration={target_duration:.6f}"
    )


def _audio_filter(target_duration: float) -> str:
    return f"apad=whole_dur={target_duration:.6f},atrim=0:{target_duration:.6f},asetpts=N/SR/TB"


def normalize_video(
    source: Path,
    destination: Path,
    target_duration: float,
    *,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> Path:
    probe_video(source, ffprobe_executable)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            _video_filter(target_duration),
            "-t",
            f"{target_duration:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "25",
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    info = probe_video(destination, ffprobe_executable)
    if (
        info.codec != "h264"
        or info.pixel_format != "yuv420p"
        or (info.width, info.height) != (640, 368)
        or abs(info.fps - 25.0) > 0.01
        or info.duration < target_duration - 0.04
    ):
        raise RuntimeError("normalized video does not match the locked MuseTalk profile")
    return destination


def normalize_audio(
    source: Path,
    destination: Path,
    target_duration: float,
    *,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> Path:
    probe_audio(source, ffprobe_executable)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            _audio_filter(target_duration),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    info = probe_audio(destination, ffprobe_executable)
    if (
        info.codec != "pcm_s16le"
        or info.sample_rate != 16000
        or info.channels != 1
        or abs(info.duration - target_duration) > 0.01
    ):
        raise RuntimeError("normalized audio does not match the locked MuseTalk profile")
    return destination


def _validate_canonical(info: AVInfo, target_duration: float) -> None:
    if (
        info.video.codec != "h264"
        or info.video.pixel_format != "yuv420p"
        or (info.video.width, info.video.height) != (640, 368)
        or abs(info.video.fps - 25.0) > 0.01
        or info.audio.codec != "aac"
        or info.audio.sample_rate != 16000
        or info.audio.channels != 1
    ):
        raise RuntimeError("canonical MuseTalk output does not match the locked A/V profile")
    coverage = min(info.duration, info.video.duration, info.audio.duration)
    if coverage < target_duration - 0.04:
        raise RuntimeError("canonical MuseTalk output does not cover the target duration")
    if abs(info.video.duration - info.audio.duration) > 0.08:
        raise RuntimeError("canonical MuseTalk output exceeds the A/V duration tolerance")


def canonicalize_output(
    upstream_video: Path,
    normalized_audio: Path,
    destination: Path,
    target_duration: float,
    *,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> tuple[Path, AVInfo]:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp4")
    try:
        probe_video(upstream_video, ffprobe_executable)
        _run_ffmpeg(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(upstream_video),
                "-i",
                str(normalized_audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                _video_filter(target_duration),
                "-af",
                _audio_filter(target_duration),
                "-t",
                f"{target_duration:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "25",
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-movflags",
                "+faststart",
                str(temp),
            ]
        )
        info = probe_av(temp, executable=ffprobe_executable)
        _validate_canonical(info, target_duration)
        os.replace(temp, destination)
        return destination, info
    except Exception as exc:
        temp.unlink(missing_ok=True)
        if isinstance(exc, RuntimeError) and str(exc).startswith("canonical"):
            raise
        raise RuntimeError(f"failed to canonicalize MuseTalk output: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_input(source: Path, destination: Path) -> str:
    """Copy one caller file into the private job and attest the staged bytes."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as caller_file, destination.open("xb") as staged_file:
            before = os.fstat(caller_file.fileno())
            shutil.copyfileobj(caller_file, staged_file, length=1024 * 1024)
            staged_file.flush()
            os.fsync(staged_file.fileno())
            after = os.fstat(caller_file.fileno())
        identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise RuntimeError(f"caller input changed while being staged: {source}")
        if destination.stat().st_size <= 0:
            raise RuntimeError(f"staged input is empty: {source}")
        return _sha256(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, payload: dict[str, Any]) -> Path:
    temp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.json")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        if temp.stat().st_size <= 0:
            raise RuntimeError("manifest serialization produced an empty file")
        os.replace(temp, path)
        return path
    finally:
        temp.unlink(missing_ok=True)


CommandRunner = Callable[..., None]


def run_generation(
    request: GenerateRequest,
    settings: MuseTalkSettings | None = None,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    validate_generate_request(request)
    settings = settings or get_settings()
    runner = command_runner or run_musetalk_command
    output_dir = Path(request.output_dir)
    source_video = Path(request.video_path).resolve()
    source_audio = Path(request.audio_path).resolve()
    job_dir = create_job_directory(settings.job_root)
    published: Path | None = None
    manifest_path: Path | None = None
    started = time.perf_counter()
    try:
        staged_video = job_dir / "source-video.mp4"
        staged_audio = job_dir / "source-audio.wav"
        source_video_sha256 = _stage_input(source_video, staged_video)
        source_audio_sha256 = _stage_input(source_audio, staged_audio)
        ffmpeg = _resolve_tool(settings.ffmpeg_bin, "ffmpeg")
        ffprobe = _resolve_tool(settings.ffmpeg_bin, "ffprobe")
        if ffmpeg is None or ffprobe is None:
            raise RuntimeError("FFmpeg and ffprobe are required for MuseTalk generation")
        normalized_video = normalize_video(
            staged_video,
            job_dir / "video.mp4",
            float(request.target_duration),
            ffmpeg_executable=ffmpeg,
            ffprobe_executable=ffprobe,
        )
        normalized_audio = normalize_audio(
            staged_audio,
            job_dir / "audio.wav",
            float(request.target_duration),
            ffmpeg_executable=ffmpeg,
            ffprobe_executable=ffprobe,
        )
        video_probe = probe_video(normalized_video, ffprobe)
        audio_probe = probe_audio(normalized_audio, ffprobe)
        task_path = write_inference_config(job_dir, normalized_video, normalized_audio)
        inference_config = yaml.safe_load(task_path.read_text(encoding="utf-8"))
        result_dir = job_dir / "results"
        command = build_musetalk_command(
            settings,
            task_path,
            result_dir,
            ffmpeg_bin=Path(ffmpeg).parent,
        )
        log_path = job_dir / "musetalk.log"
        runner(
            command,
            cwd=str(settings.repo_path),
            timeout=settings.timeout_seconds,
            log_path=log_path,
        )
        upstream = expected_musetalk_output(result_dir)
        if not upstream.is_file() or upstream.stat().st_size <= 0:
            raise RuntimeError(f"expected MuseTalk output is missing or empty: {upstream}")

        token = uuid.uuid4().hex
        final_path = output_dir / f"musetalk-{token}.mp4"
        published, output_probe = canonicalize_output(
            upstream,
            normalized_audio,
            final_path,
            float(request.target_duration),
            ffmpeg_executable=ffmpeg,
            ffprobe_executable=ffprobe,
        )
        sha256 = _sha256(published)
        generation_time = time.perf_counter() - started
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else ""
        manifest_path = output_dir / f"musetalk-{token}.manifest.json"
        manifest = {
            "provider": "musetalk",
            "provider_version": settings.repo_commit,
            "model": "musetalk-v1.5",
            "workflow": "musetalk_lipsync",
            "generation_time": generation_time,
            "command": command,
            "target_duration": float(request.target_duration),
            "batch_size": request.batch_size,
            "fp16": request.use_float16,
            "use_float16": request.use_float16,
            "output_path": str(published),
            "sha256": sha256,
            "output_sha256": sha256,
            "source_video": {
                "path": str(source_video),
                "sha256": source_video_sha256,
            },
            "source_audio": {
                "path": str(source_audio),
                "sha256": source_audio_sha256,
            },
            "inference_config": inference_config,
            "metadata": request.metadata,
            "probes": {
                "normalized_video": asdict(video_probe),
                "normalized_audio": asdict(audio_probe),
                "output": asdict(output_probe),
            },
            "log_tail": log_tail,
        }
        _atomic_json(manifest_path, manifest)
        return {
            "output_path": str(published),
            "manifest_path": str(manifest_path),
            "duration": output_probe.duration,
            "width": output_probe.video.width,
            "height": output_probe.video.height,
            "fps": output_probe.video.fps,
            "sha256": sha256,
            "generation_time": generation_time,
        }
    except Exception:
        if manifest_path is not None:
            manifest_path.unlink(missing_ok=True)
        if published is not None:
            published.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def _ready_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _service_busy() -> bool:
    return bool(active_generation_tasks) or generation_lock.locked() or active_child is not None


async def _run_generation_serialized(request: GenerateRequest) -> dict[str, Any]:
    async with generation_lock:
        return await asyncio.to_thread(run_generation, request)


def _generation_task_done(task: asyncio.Task[dict[str, Any]]) -> None:
    active_generation_tasks.discard(task)
    if not task.cancelled():
        task.exception()


def check_cuda(settings: MuseTalkSettings, timeout: float = 20.0) -> dict[str, Any]:
    if not _ready_file(settings.python_executable):
        return {"available": False, "device": None, "detail": "Python executable is missing"}
    script = (
        "import json,torch; print(json.dumps({'available': bool(torch.cuda.is_available()), "
        "'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))"
    )
    try:
        result = subprocess.run(
            [str(settings.python_executable), "-c", script],
            cwd=str(settings.repo_path) if settings.repo_path.is_dir() else None,
            capture_output=True,
            text=True,
            timeout=min(timeout, settings.timeout_seconds),
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "device": None, "detail": str(exc)}
    if result.returncode != 0:
        return {"available": False, "device": None, "detail": (result.stderr or "")[-500:]}
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"available": False, "device": None, "detail": "CUDA probe returned malformed output"}
    return {
        "available": payload.get("available") is True,
        "device": payload.get("device"),
        "detail": None,
    }


def build_runtime_health(settings: MuseTalkSettings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    inference = settings.repo_path / "scripts" / "inference.py"
    model_checks = {
        relative_path: _ready_file(settings.repo_path / relative_path)
        for relative_path in REQUIRED_MODEL_PATHS
    }
    models_ready = all(model_checks.values())
    ffmpeg = _resolve_tool(settings.ffmpeg_bin, "ffmpeg")
    ffprobe = _resolve_tool(settings.ffmpeg_bin, "ffprobe")
    cuda = check_cuda(settings)
    checks = {
        "repo": settings.repo_path.is_dir() and _ready_file(inference),
        "python": _ready_file(settings.python_executable),
        "model": models_ready,
        "config": model_checks["models/musetalkV15/musetalk.json"],
        "ffmpeg": ffmpeg is not None,
        "ffprobe": ffprobe is not None,
        "cuda": cuda.get("available") is True,
    }
    ready = all(checks.values())
    busy = _service_busy()
    return {
        "status": "ONLINE" if ready else "DEGRADED",
        "ready": ready,
        "busy": busy,
        "active_child": active_child is not None,
        "persistent_model": False,
        "repo_commit": settings.repo_commit,
        "checks": checks,
        "models": model_checks,
        "missing_models": [path for path, present in model_checks.items() if not present],
        "cuda": cuda,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        return build_runtime_health()
    except Exception as exc:
        return {
            "status": "DEGRADED",
            "ready": False,
            "busy": _service_busy(),
            "active_child": active_child is not None,
            "persistent_model": False,
            "detail": str(exc),
        }


@app.post("/generate")
async def generate(request: GenerateRequest) -> dict[str, Any]:
    try:
        validate_generate_request(request)
        task = asyncio.create_task(_run_generation_serialized(request))
        active_generation_tasks.add(task)
        task.add_done_callback(_generation_task_done)
        return await asyncio.shield(task)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MuseTalk generation failed: {exc}") from exc


@app.post("/unload")
def unload() -> dict[str, Any]:
    if _service_busy():
        raise HTTPException(status_code=409, detail="MuseTalk generation child is active")
    return {
        "status": "UNLOADED",
        "unloaded": True,
        "persistent_model": False,
        "active_child": False,
    }
