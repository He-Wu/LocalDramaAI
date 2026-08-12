"""Probe playable video and audio streams with one ffprobe invocation.

Per-stream durations prefer ffprobe's stream metadata. Containers that omit it
use the positive format duration as the effective stream duration.
"""

import json
import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class VideoStreamInfo:
    codec: str
    pixel_format: str
    width: int
    height: int
    fps: float
    frames: int | None
    duration: float


@dataclass(frozen=True)
class AudioStreamInfo:
    codec: str
    sample_rate: int
    channels: int
    duration: float


@dataclass(frozen=True)
class AVInfo:
    video: VideoStreamInfo
    audio: AudioStreamInfo
    duration: float
    format_name: str


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _metadata_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = value.strip()
    return parsed if parsed and parsed not in {"N/A", "unknown"} else None


def _positive_fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        try:
            parsed = float(Fraction(str(value)))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return None


def _optional_frames(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    return _positive_int(value)


def _video_info(stream: dict, format_duration: float) -> VideoStreamInfo | None:
    codec = _metadata_text(stream.get("codec_name"))
    pixel_format = _metadata_text(stream.get("pix_fmt"))
    width = _positive_int(stream.get("width"))
    height = _positive_int(stream.get("height"))
    fps = _positive_fps(stream)
    duration = _positive_float(stream.get("duration")) or format_duration
    if None in (codec, pixel_format, width, height, fps):
        return None
    return VideoStreamInfo(
        codec=codec,
        pixel_format=pixel_format,
        width=width,
        height=height,
        fps=fps,
        frames=_optional_frames(stream.get("nb_frames")),
        duration=duration,
    )


def _audio_info(stream: dict, format_duration: float) -> AudioStreamInfo | None:
    codec = _metadata_text(stream.get("codec_name"))
    sample_rate = _positive_int(stream.get("sample_rate"))
    channels = _positive_int(stream.get("channels"))
    duration = _positive_float(stream.get("duration")) or format_duration
    if None in (codec, sample_rate, channels):
        return None
    return AudioStreamInfo(
        codec=codec,
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
    )


def probe_av(path: Path, executable: str = "ffprobe") -> AVInfo:
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"media file is missing or empty: {path}")
    except OSError as exc:
        raise ValueError(f"media file cannot be read: {path}") from exc

    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                (
                    "stream=codec_type,codec_name,pix_fmt,width,height,"
                    "avg_frame_rate,r_frame_rate,nb_frames,duration,sample_rate,channels:"
                    "format=duration,format_name"
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
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffprobe timed out while probing media: {path}") from exc
    except OSError as exc:
        raise ValueError(f"cannot run ffprobe executable {executable!r}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()[-500:] if result.stderr else ""
        detail = f": {stderr}" if stderr else ""
        raise ValueError(f"invalid media file: {path}{detail}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"ffprobe returned malformed JSON for media: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"ffprobe returned incomplete metadata for media: {path}")
    streams = payload.get("streams")
    format_metadata = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_metadata, dict):
        raise ValueError(f"ffprobe returned incomplete metadata for media: {path}")

    format_duration = _positive_float(format_metadata.get("duration"))
    if format_duration is None:
        raise ValueError(f"media has no positive container duration: {path}")
    format_name = _metadata_text(format_metadata.get("format_name"))
    if format_name is None:
        raise ValueError(f"media has no recognized container format: {path}")

    video = None
    audio = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if video is None and stream.get("codec_type") == "video":
            video = _video_info(stream, format_duration)
        elif audio is None and stream.get("codec_type") == "audio":
            audio = _audio_info(stream, format_duration)
        if video is not None and audio is not None:
            break

    if video is None:
        raise ValueError(f"media contains no playable video stream: {path}")
    if audio is None:
        raise ValueError(f"media contains no playable audio stream: {path}")
    return AVInfo(
        video=video,
        audio=audio,
        duration=format_duration,
        format_name=format_name,
    )
