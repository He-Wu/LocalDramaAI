import json
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.providers.ffmpeg_provider import FFmpegProvider


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * int(seconds * 24000))
    return path


def _video(
    provider: FFmpegProvider,
    path: Path,
    color: tuple[int, int, int],
    *,
    seconds: float = 1.0,
    size: tuple[int, int] = (640, 368),
) -> Path:
    frame = path.with_suffix(".png")
    Image.new("RGB", size, color).save(frame)
    return provider.image_to_mp4(frame, path, duration=seconds, fps=16)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _sample_frame(path: Path, timestamp: float, output_path: Path) -> tuple[int, int, int]:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    with Image.open(output_path) as frame:
        return frame.convert("RGB").getpixel((320, 184))


def test_mux_and_concat_real_shots_preserve_streams_profile_and_order(tmp_path):
    provider = FFmpegProvider()
    media_dir = tmp_path / "director's cut"
    media_dir.mkdir()
    first_video = _video(provider, media_dir / "first shot.mp4", (220, 20, 20))
    second_video = _video(provider, media_dir / "second shot.mp4", (20, 20, 220))
    first_audio = _wav(media_dir / "first audio.wav")
    second_audio = _wav(media_dir / "second audio.wav")

    first_muxed = provider.mux_audio(first_video, first_audio, media_dir / "first muxed.mp4")
    second_muxed = provider.mux_audio(second_video, second_audio, media_dir / "second muxed.mp4")
    final = provider.concat([first_muxed, second_muxed], media_dir / "final movie.mp4")

    probe = _probe(final)
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert video_stream["codec_name"] == "h264"
    assert (video_stream["width"], video_stream["height"]) == (640, 368)
    assert audio_stream["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) >= 1.9

    first_pixel = _sample_frame(final, 0.25, tmp_path / "first-sample.png")
    second_pixel = _sample_frame(final, 1.25, tmp_path / "second-sample.png")
    assert first_pixel[0] > first_pixel[2] + 100
    assert second_pixel[2] > second_pixel[0] + 100


def test_mux_pads_and_normalizes_short_audio_to_preserve_video_tail(tmp_path):
    provider = FFmpegProvider()
    video = _video(
        provider,
        tmp_path / "long visual.mp4",
        (20, 120, 20),
        seconds=1.3,
    )
    audio = _wav(tmp_path / "short dialogue.wav", seconds=1.0)

    output = provider.mux_audio(video, audio, tmp_path / "muxed.mp4")

    video_duration = float(_probe(video)["format"]["duration"])
    probe = _probe(output)
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert float(probe["format"]["duration"]) == pytest.approx(video_duration, abs=0.08)
    assert float(video_stream["duration"]) == pytest.approx(video_duration, abs=0.08)
    assert audio_stream["codec_name"] == "aac"
    assert audio_stream["sample_rate"] == "48000"
    assert audio_stream["channels"] == 2
    assert audio_stream["channel_layout"] == "stereo"


def test_run_captures_output_uses_timeout_and_reports_stderr_tail(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=9, stderr="x" * 1500 + "useful tail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="useful tail"):
        FFmpegProvider()._run(["ffmpeg", "-version"], timeout=17)
    assert captured == {
        "args": ["ffmpeg", "-version"],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 17,
            "encoding": "utf-8",
            "errors": "replace",
        },
    }


def test_run_handles_invalid_utf8_error_from_chinese_path(tmp_path):
    script_dir = tmp_path / "中文目录"
    script_dir.mkdir()
    script = script_dir / "错误.py"
    script.write_text(
        "import sys\nsys.stderr.buffer.write(b'\\xff\\xff useful error')\nsys.exit(3)\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="useful error"):
        FFmpegProvider()._run([sys.executable, str(script)])


def test_mux_timeout_is_runtime_error_and_cleans_temporary_output(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())
    captured_args = None

    def time_out(args, **kwargs):
        nonlocal captured_args
        captured_args = args
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="stalled")

    monkeypatch.setattr(subprocess, "run", time_out)
    with pytest.raises(RuntimeError, match="timed out.*stalled"):
        provider.mux_audio(video, audio, output)

    assert captured_args is not None and "-nostdin" in captured_args
    assert captured_args[captured_args.index("-b:a") + 1] == "192k"
    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


@pytest.mark.parametrize("missing", ["video", "audio"])
def test_mux_rejects_missing_or_empty_input_before_launch(tmp_path, monkeypatch, missing):
    provider = FFmpegProvider()
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    target = video if missing == "video" else audio
    if missing == "video":
        target.unlink()
    else:
        target.write_bytes(b"")
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )

    with pytest.raises(ValueError, match=missing):
        provider.mux_audio(video, audio, tmp_path / "output.mp4")


def test_concat_rejects_empty_list_and_invalid_input_before_launch(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )
    with pytest.raises(ValueError, match="at least one"):
        provider.concat([], tmp_path / "output.mp4")

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="concat input"):
        provider.concat([empty], tmp_path / "output.mp4")


def test_mux_failure_preserves_destination_and_cleans_temporary_output(tmp_path):
    provider = FFmpegProvider()
    video = _video(provider, tmp_path / "valid.mp4", (20, 120, 20))
    invalid_audio = tmp_path / "invalid.wav"
    invalid_audio.write_bytes(b"not audio")
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        provider.mux_audio(video, invalid_audio, output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_failure_preserves_destination_and_cleans_manifest_and_output(tmp_path):
    ffmpeg = FFmpegProvider()
    valid = ffmpeg.mux_audio(
        _video(ffmpeg, tmp_path / "valid-video.mp4", (20, 120, 20)),
        _wav(tmp_path / "valid-audio.wav"),
        tmp_path / "valid.mp4",
    )
    provider = FFmpegProvider(executable=sys.executable)
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        provider.concat([valid], output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_rejects_incompatible_video_profile_before_output_allocation(tmp_path):
    provider = FFmpegProvider()
    audio = _wav(tmp_path / "audio.wav")
    first = provider.mux_audio(
        _video(provider, tmp_path / "wide.mp4", (20, 120, 20)),
        audio,
        tmp_path / "wide-muxed.mp4",
    )
    second = provider.mux_audio(
        _video(
            provider,
            tmp_path / "small.mp4",
            (120, 20, 20),
            size=(320, 184),
        ),
        audio,
        tmp_path / "small-muxed.mp4",
    )
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep me")
    files_before = set(tmp_path.iterdir())

    with pytest.raises(ValueError, match="profile.*width"):
        provider.concat([first, second], output)

    assert output.read_bytes() == b"keep me"
    assert set(tmp_path.iterdir()) == files_before


def test_concat_requires_video_and_audio_streams(tmp_path):
    provider = FFmpegProvider()
    video_only = _video(provider, tmp_path / "video-only.mp4", (20, 120, 20))
    output = tmp_path / "output.mp4"

    with pytest.raises(ValueError, match="audio stream"):
        provider.concat([video_only], output)

    assert not output.exists()


def test_concat_requires_video_stream(tmp_path):
    provider = FFmpegProvider()
    audio_only = _wav(tmp_path / "audio-only.wav")

    with pytest.raises(ValueError, match="video stream"):
        provider.concat([audio_only], tmp_path / "output.mp4")


@pytest.mark.parametrize(
    "field",
    [
        "video.codec",
        "video.width",
        "video.height",
        "video.pixel_format",
        "video.frame_rate",
        "video.time_base",
        "audio.codec",
        "audio.sample_rate",
        "audio.channels",
        "audio.channel_layout",
        "audio.time_base",
    ],
)
def test_concat_checks_every_signature_field_before_temp_allocation(
    tmp_path,
    monkeypatch,
    field,
):
    provider = FFmpegProvider()
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    baseline = {
        "video.codec": "h264",
        "video.width": 640,
        "video.height": 368,
        "video.pixel_format": "yuv420p",
        "video.frame_rate": "16/1",
        "video.time_base": "1/16384",
        "audio.codec": "aac",
        "audio.sample_rate": "48000",
        "audio.channels": 2,
        "audio.channel_layout": "stereo",
        "audio.time_base": "1/48000",
    }
    mismatched = baseline | {field: "different"}
    signatures = iter([baseline, mismatched])
    monkeypatch.setattr(provider, "_media_signature", lambda path: next(signatures))
    monkeypatch.setattr(
        provider,
        "_temporary_path",
        lambda *args, **kwargs: pytest.fail("temporary output allocated before validation"),
    )

    with pytest.raises(ValueError, match="profile mismatch"):
        provider.concat([first, second], tmp_path / "output.mp4")


def test_concat_cleans_output_temp_if_manifest_allocation_fails(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    valid = tmp_path / "valid.mp4"
    valid.write_bytes(b"video")
    leaked_temp = tmp_path / ".output.tmp.mp4"

    def allocate(output_path, suffix=None):
        if suffix is None:
            leaked_temp.write_bytes(b"")
            return leaked_temp
        raise OSError("manifest allocation failed")

    monkeypatch.setattr(provider, "_media_signature", lambda path: {}, raising=False)
    monkeypatch.setattr(provider, "_temporary_path", allocate)
    with pytest.raises(OSError, match="manifest allocation failed"):
        provider.concat([valid], tmp_path / "output.mp4")

    assert not leaked_temp.exists()


def test_concat_manifest_escapes_literal_backslashes():
    class PosixPathWithBackslash:
        def resolve(self):
            return self

        def as_posix(self):
            return "root/back\\slash/clip.mp4"

    assert FFmpegProvider._quote_manifest_path(PosixPathWithBackslash()) == (
        "'root/back'\\\\'slash/clip.mp4'"
    )


def test_concat_rejects_newline_path_before_launch(tmp_path, monkeypatch):
    provider = FFmpegProvider()
    unsafe = tmp_path / "unsafe\nname.mp4"
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **kwargs: pytest.fail("FFmpeg launched"),
        raising=False,
    )

    with pytest.raises(ValueError, match="newline"):
        provider.concat([unsafe], tmp_path / "output.mp4")
