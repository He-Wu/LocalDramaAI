import subprocess

import pytest

from app.services.media_probe import probe_av


def _ffmpeg(path, *arguments):
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
        timeout=30,
        shell=False,
    )
    return path


def _av_fixture(tmp_path):
    return _ffmpeg(
        tmp_path / "av.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x368:rate=25:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000:duration=1",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "25",
        "-fps_mode",
        "cfr",
        "-c:a",
        "aac",
        "-ac",
        "1",
        "-shortest",
    )


def _video_only_fixture(tmp_path):
    return _ffmpeg(
        tmp_path / "video-only.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x368:rate=25:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
    )


def _audio_only_fixture(tmp_path):
    return _ffmpeg(
        tmp_path / "audio-only.m4a",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000:duration=1",
        "-c:a",
        "aac",
        "-ac",
        "1",
        "-vn",
    )


def test_probe_av_reports_real_video_audio_and_container_metadata(tmp_path):
    info = probe_av(_av_fixture(tmp_path))

    assert info.video.codec == "h264"
    assert info.video.pixel_format == "yuv420p"
    assert (info.video.width, info.video.height) == (640, 368)
    assert info.video.fps == pytest.approx(25.0)
    assert info.video.duration > 0
    if info.video.frames is not None:
        assert info.video.frames > 0
    assert info.audio.codec == "aac"
    assert info.audio.sample_rate > 0
    assert info.audio.channels == 1
    assert info.audio.duration > 0
    assert info.duration > 0


def test_probe_av_rejects_real_video_without_audio(tmp_path):
    with pytest.raises(ValueError, match="audio"):
        probe_av(_video_only_fixture(tmp_path))


def test_probe_av_rejects_real_audio_without_video(tmp_path):
    with pytest.raises(ValueError, match="video"):
        probe_av(_audio_only_fixture(tmp_path))


@pytest.mark.parametrize(
    ("name", "contents", "message"),
    [
        ("missing.mp4", None, "missing or empty"),
        ("empty.mp4", b"", "missing or empty"),
        ("malformed.mp4", b"not an MP4", "invalid media"),
    ],
)
def test_probe_av_rejects_invalid_files(tmp_path, name, contents, message):
    path = tmp_path / name
    if contents is not None:
        path.write_bytes(contents)

    with pytest.raises(ValueError, match=message):
        probe_av(path)
