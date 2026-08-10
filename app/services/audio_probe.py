from dataclasses import dataclass
from pathlib import Path
import wave


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration: float


def probe_wav(path: Path) -> WavInfo:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"WAV file is missing or empty: {path}")
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels(); sample_width = source.getsampwidth()
            sample_rate = source.getframerate(); frames = source.getnframes()
            if source.getcomptype() != "NONE":
                raise ValueError("WAV must contain uncompressed PCM")
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Invalid WAV file: {path}") from exc
    if channels < 1 or sample_rate < 1 or frames < 1 or sample_width < 1:
        raise ValueError(f"WAV contains no playable audio: {path}")
    return WavInfo(sample_rate, channels, sample_width, frames, frames / sample_rate)
