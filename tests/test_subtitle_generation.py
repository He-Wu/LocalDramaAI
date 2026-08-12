import os
from pathlib import Path

import pytest

from app.services.render_timeline import (
    RenderProfile,
    RenderTimeline,
    TimelineDialogue,
    TimelineShot,
)
from app.services.subtitle_generation import (
    SubtitleCue,
    cues_from_timeline,
    format_srt_timestamp,
    serialize_srt,
    write_subtitle_atomic,
)


def _timeline_with_dialogues(*dialogues: TimelineDialogue) -> RenderTimeline:
    shot = TimelineShot(
        shot_id="shot-1",
        scene_id="scene-1",
        character_id=None,
        order=0,
        persisted_duration=10.0,
        status="READY",
        requires_lip_sync=False,
        speaker_visible=False,
        storyboard_asset_id=None,
        source_video_asset_id="video-1",
        source_lipsync_asset_id=None,
        video_asset_id="video-1",
        video_asset_project_id="project-1",
        video_asset_kind="VIDEO",
        video_raw_path="video.mp4",
        video_path=Path("video.mp4"),
        video_size=1,
        video_sha256="0" * 64,
        start_frame=0,
        frame_count=250,
        dialogues=tuple(dialogues),
    )
    return RenderTimeline(
        project_id="project-1",
        subtitle_asset_id=None,
        final_video_asset_id=None,
        profile=RenderProfile(),
        scenes=(),
        shots=(shot,),
        total_frames=250,
        canonical_json="{}",
        workflow_hash="0" * 64,
    )


def _dialogue(order: int, text: str, start_ms: int, end_ms: int) -> TimelineDialogue:
    return TimelineDialogue(
        dialogue_id=f"dialogue-{order}",
        order=order,
        text=text,
        persisted_duration=(end_ms - start_ms) / 1_000,
        persisted_start_time=start_ms / 1_000,
        persisted_end_time=end_ms / 1_000,
        audio_asset_id=f"audio-{order}",
        audio_asset_project_id="project-1",
        audio_asset_kind="AUDIO",
        audio_raw_path=f"audio-{order}.wav",
        audio_path=Path(f"audio-{order}.wav"),
        audio_size=1,
        audio_sha256="0" * 64,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def test_serialize_srt_uses_exact_utf8_crlf_and_offsets():
    cues = (
        SubtitleCue(1, 0, 2480, "你好，欢迎来到本地短剧。"),
        SubtitleCue(2, 2800, 3800, "第二个镜头。"),
    )

    assert serialize_srt(cues) == (
        "1\r\n00:00:00,000 --> 00:00:02,480\r\n"
        "你好，欢迎来到本地短剧。\r\n\r\n"
        "2\r\n00:00:02,800 --> 00:00:03,800\r\n"
        "第二个镜头。\r\n"
    ).encode("utf-8")


def test_format_srt_timestamp_supports_more_than_one_hour():
    assert format_srt_timestamp(3_723_004) == "01:02:03,004"


def test_serialize_srt_normalizes_multiline_text_to_crlf():
    cue = SubtitleCue(1, 0, 1_000, "第一行\r\n第二行\r第三行\n第四行")

    assert serialize_srt((cue,)) == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\n"
        "第一行\r\n第二行\r\n第三行\r\n第四行\r\n"
    ).encode("utf-8")


def test_serialize_srt_removes_trailing_newline_ambiguity():
    cue = SubtitleCue(1, 0, 1_000, "第一行\r\n")

    assert serialize_srt((cue,)) == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\n第一行\r\n"
    ).encode("utf-8")


def test_serialize_srt_removes_c0_controls_and_preserves_unicode():
    cue = SubtitleCue(1, 0, 1_000, "\x00你\t好\x07 😀\x1f")

    assert serialize_srt((cue,)) == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\n你好 😀\r\n"
    ).encode("utf-8")


def test_serialize_srt_escapes_ass_and_html_style_metacharacters():
    cue = SubtitleCue(1, 0, 1_000, r"& <b>危险</b> {\an8}")

    assert serialize_srt((cue,)) == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\n"
        r"&amp; &lt;b&gt;危险&lt;/b&gt; &#123;\an8&#125;" "\r\n"
    ).encode("utf-8")


def test_serialize_srt_rejects_control_only_text():
    cue = SubtitleCue(1, 0, 1_000, "\x00\t\x07\x1f")

    with pytest.raises(ValueError, match="Subtitle text must be nonempty"):
        serialize_srt((cue,))


def test_serialize_srt_empty_cues_is_empty_bytes():
    assert serialize_srt(()) == b""


def test_cues_from_timeline_allows_a_shot_without_dialogue():
    assert cues_from_timeline(_timeline_with_dialogues()) == ()


def test_cues_from_timeline_numbers_chronological_nonoverlapping_dialogues():
    timeline = _timeline_with_dialogues(
        _dialogue(7, "第一句", 0, 1_000),
        _dialogue(9, "第二句", 1_000, 2_500),
    )

    assert cues_from_timeline(timeline) == (
        SubtitleCue(1, 0, 1_000, "第一句"),
        SubtitleCue(2, 1_000, 2_500, "第二句"),
    )


def test_cues_from_timeline_rejects_dialogues_out_of_time_order():
    timeline = _timeline_with_dialogues(
        _dialogue(0, "第二句", 1_000, 2_000),
        _dialogue(1, "第一句", 0, 500),
    )

    with pytest.raises(ValueError, match="Subtitle cues must be ordered by time"):
        cues_from_timeline(timeline)


def test_cues_from_timeline_rejects_overlapping_dialogues():
    timeline = _timeline_with_dialogues(
        _dialogue(0, "第一句", 0, 1_500),
        _dialogue(1, "第二句", 1_000, 2_000),
    )

    with pytest.raises(ValueError, match="Subtitle cues must not overlap"):
        cues_from_timeline(timeline)


def test_write_subtitle_atomic_uses_unique_same_directory_temps_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "字幕.srt"
    replaced_from: list[Path] = []
    real_replace = os.replace

    def record_replace(source, destination):
        source_path = Path(source)
        assert source_path.parent == output.parent
        assert Path(destination) == output
        assert source_path != output
        replaced_from.append(source_path)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)

    write_subtitle_atomic(output, b"first")
    write_subtitle_atomic(output, b"second")

    assert output.read_bytes() == b"second"
    assert len(replaced_from) == 2
    assert replaced_from[0] != replaced_from[1]
    assert not any(path.exists() for path in replaced_from)


def test_write_subtitle_atomic_preserves_existing_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "subtitles.srt"
    output.write_bytes(b"existing")

    def fail_replace(source, destination):
        assert Path(source).read_bytes() == b"candidate"
        assert Path(destination) == output
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_subtitle_atomic(output, b"candidate")

    assert output.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [output]


def test_write_subtitle_atomic_preserves_original_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "subtitles.srt"

    def fail_replace(source, destination):
        raise OSError("replace failed")

    def fail_unlink(self, *args, **kwargs):
        raise OSError("cleanup failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(OSError, match="replace failed"):
        write_subtitle_atomic(output, b"candidate")
