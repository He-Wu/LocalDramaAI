import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int
    height: int
    fps: float
    frames: int | None
    duration: float


def probe_video(path: Path, executable: str = "ffprobe") -> VideoInfo:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"video file is missing or empty: {path}")
    result = subprocess.run(
        [
            executable, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError(f"invalid video file: {path}: {result.stderr[-500:]}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        duration = float(payload["format"]["duration"])
        fps = float(Fraction(stream["r_frame_rate"]))
        frame_value = stream.get("nb_frames")
        frames = int(frame_value) if frame_value not in (None, "N/A") else None
        info = VideoInfo(
            codec=str(stream["codec_name"]),
            width=int(stream["width"]),
            height=int(stream["height"]),
            fps=fps,
            frames=frames,
            duration=duration,
        )
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"video probe returned incomplete metadata: {path}") from exc
    if info.duration <= 0 or info.fps <= 0 or info.width <= 0 or info.height <= 0:
        raise ValueError(f"video contains no playable stream: {path}")
    return info
