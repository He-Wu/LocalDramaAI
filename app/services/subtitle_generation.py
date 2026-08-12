"""Generate deterministic SubRip subtitle payloads."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from app.services.render_timeline import RenderTimeline


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def _validate_interval(start_ms: int, end_ms: int) -> None:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("Subtitle cue interval is invalid")


def cues_from_timeline(timeline: RenderTimeline) -> tuple[SubtitleCue, ...]:
    cues: list[SubtitleCue] = []
    previous_start = -1
    previous_end = 0
    timeline_end_ms = int(
        (Decimal(timeline.total_frames) * 1000 / Decimal(timeline.profile.fps))
        .to_integral_value(rounding=ROUND_HALF_UP)
    )
    for shot in timeline.shots:
        for dialogue in shot.dialogues:
            _validate_interval(dialogue.start_ms, dialogue.end_ms)
            if dialogue.end_ms > timeline_end_ms:
                raise ValueError("Subtitle cue exceeds timeline duration")
            if dialogue.start_ms < previous_start:
                raise ValueError("Subtitle cues must be ordered by time")
            if cues and dialogue.start_ms < previous_end:
                raise ValueError("Subtitle cues must not overlap")
            cues.append(
                SubtitleCue(
                    len(cues) + 1,
                    dialogue.start_ms,
                    dialogue.end_ms,
                    dialogue.text,
                )
            )
            previous_start = dialogue.start_ms
            previous_end = dialogue.end_ms
    return tuple(cues)


def _sanitize_text(text: str) -> str:
    text = "".join(
        character
        for character in text
        if character in "\r\n" or ord(character) >= 0x20
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    text = text.rstrip("\r\n")
    if not text.strip():
        raise ValueError("Subtitle text must be nonempty")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )


def format_srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def serialize_srt(cues: tuple[SubtitleCue, ...]) -> bytes:
    previous_start = -1
    previous_end = 0
    for expected_index, cue in enumerate(cues, start=1):
        if cue.index != expected_index:
            raise ValueError("Subtitle cue indices must be sequential")
        _validate_interval(cue.start_ms, cue.end_ms)
        if cue.start_ms < previous_start:
            raise ValueError("Subtitle cues must be ordered by time")
        if expected_index > 1 and cue.start_ms < previous_end:
            raise ValueError("Subtitle cues must not overlap")
        previous_start = cue.start_ms
        previous_end = cue.end_ms
    blocks = [
        (
            f"{cue.index}\r\n"
            f"{format_srt_timestamp(cue.start_ms)} --> "
            f"{format_srt_timestamp(cue.end_ms)}\r\n"
            f"{_sanitize_text(cue.text)}"
        )
        for cue in cues
    ]
    if not blocks:
        return b""
    return "\r\n\r\n".join(blocks).encode("utf-8") + b"\r\n"


def write_subtitle_atomic(output_path: Path, payload: bytes) -> None:
    output_path = Path(output_path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
