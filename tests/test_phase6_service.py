import wave
from pathlib import Path

import pytest

from ai_services.qwen3_tts.service import GenerateRequest, atomic_publish_wav, validate_generate_request


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000); output.writeframes(b"\x00\x00" * 2400)
    return path


def test_generate_request_rejects_empty_text():
    with pytest.raises(ValueError):
        validate_generate_request(GenerateRequest(text="", language="Chinese", reference_audio="ref.wav", reference_transcript="你好", output_path="out.wav"))


def test_atomic_publish_wav_replaces_only_after_valid_file(tmp_path):
    source = _wav(tmp_path / "source.tmp.wav"); destination = tmp_path / "out.wav"
    result = atomic_publish_wav(source, destination)
    assert result == destination and destination.exists() and not source.exists()
