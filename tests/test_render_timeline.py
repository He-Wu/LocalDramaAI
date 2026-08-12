from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, Project, Scene, Shot
from app.services.render_timeline import (
    RenderProfile,
    assert_render_timeline_unchanged,
    build_render_timeline,
)
from app.services.video_probe import VideoInfo


def _write_wav(path: Path, duration: float = 1.0, sample_rate: int = 8_000) -> None:
    frames = round(duration * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * frames)


def _asset(
    project_id: str,
    asset_id: str,
    kind: str,
    path: Path,
) -> Asset:
    return Asset(id=asset_id, project_id=project_id, kind=kind, path=str(path))


@pytest.fixture
def seed_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    database = str(tmp_path / "phase9.db")
    create_schema(database)

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video-source")
    lipsync_path = tmp_path / "lipsync.mp4"
    lipsync_path.write_bytes(b"lipsync-source")
    audio_a_path = tmp_path / "a.wav"
    _write_wav(audio_a_path, 1.0)
    audio_b_path = tmp_path / "b.wav"
    _write_wav(audio_b_path, 0.5)

    project_id = "project-1"
    scene_later_id = "scene-later"
    scene_first_id = "scene-first"
    lipsync_shot_id = "shot-lipsync"
    video_shot_id = "shot-video"
    with session_scope(database) as session:
        session.add(Project(id=project_id, name="Phase 9"))
        session.flush()
        session.add_all(
            [
                _asset(project_id, "video-asset", "VIDEO", video_path),
                _asset(project_id, "lipsync-asset", "LIPSYNC", lipsync_path),
                _asset(project_id, "audio-a", "AUDIO", audio_a_path),
                _asset(project_id, "audio-b", "AUDIO", audio_b_path),
            ]
        )
        session.flush()
        session.add_all(
            [
                Scene(
                    id=scene_later_id,
                    project_id=project_id,
                    order=20,
                    title="Later",
                    description="Later scene",
                ),
                Scene(
                    id=scene_first_id,
                    project_id=project_id,
                    order=10,
                    title="First",
                    description="First scene",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Shot(
                    id=video_shot_id,
                    scene_id=scene_later_id,
                    order=2,
                    title="Video",
                    description="Video source",
                    duration=1.01,
                    video_asset_id="video-asset",
                    requires_lip_sync=False,
                    speaker_visible=True,
                    status="VIDEO_GENERATED",
                ),
                Shot(
                    id=lipsync_shot_id,
                    scene_id=scene_first_id,
                    character_id=None,
                    order=7,
                    title="Lip sync",
                    description="Lip-sync source",
                    duration=1.0,
                    storyboard_asset_id=None,
                    video_asset_id="video-asset",
                    lipsync_asset_id="lipsync-asset",
                    requires_lip_sync=True,
                    speaker_visible=True,
                    status="LIPSYNC_GENERATED",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Dialogue(
                    id="dialogue-b",
                    shot_id=video_shot_id,
                    order=9,
                    text="second",
                    audio_asset_id="audio-b",
                    duration=0.5,
                    start_time=0.5,
                    end_time=1.0,
                ),
                Dialogue(
                    id="dialogue-a",
                    shot_id=lipsync_shot_id,
                    order=5,
                    text="first",
                    audio_asset_id="audio-a",
                    duration=1.0,
                    start_time=None,
                    end_time=None,
                ),
            ]
        )

    monkeypatch.setattr(
        "app.services.render_timeline.probe_video",
        lambda _path: VideoInfo(
            codec="h264", width=640, height=368, fps=25.0, frames=100, duration=4.0
        ),
    )
    return SimpleNamespace(
        database=database,
        project_id=project_id,
        expected_order=[lipsync_shot_id, video_shot_id],
        lipsync_asset_id="lipsync-asset",
        video_asset_id="video-asset",
        scene_first_id=scene_first_id,
        video_path=video_path,
        lipsync_path=lipsync_path,
        audio_a_path=audio_a_path,
        audio_b_path=audio_b_path,
    )


def test_timeline_orders_selects_and_snapshots_required_assets(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)

    assert isinstance(timeline.profile, RenderProfile)
    assert timeline.subtitle_asset_id is None
    assert timeline.final_video_asset_id is None
    assert [scene.scene_id for scene in timeline.scenes] == [
        seed_project.scene_first_id,
        "scene-later",
    ]
    assert [shot.shot_id for shot in timeline.shots] == seed_project.expected_order
    assert [shot.video_asset_id for shot in timeline.shots] == [
        seed_project.lipsync_asset_id,
        seed_project.video_asset_id,
    ]
    assert [dialogue.dialogue_id for shot in timeline.shots for dialogue in shot.dialogues] == [
        "dialogue-a",
        "dialogue-b",
    ]

    first, second = timeline.shots
    assert first.scene_id == seed_project.scene_first_id
    assert first.character_id is None
    assert first.order == 7
    assert first.persisted_duration == 1.0
    assert first.status == "LIPSYNC_GENERATED"
    assert first.requires_lip_sync is True
    assert first.speaker_visible is True
    assert first.storyboard_asset_id is None
    assert first.source_video_asset_id == "video-asset"
    assert first.source_lipsync_asset_id == "lipsync-asset"
    assert first.video_asset_project_id == seed_project.project_id
    assert first.video_asset_kind == "LIPSYNC"
    assert first.video_raw_path == str(seed_project.lipsync_path)
    assert first.video_path == seed_project.lipsync_path.resolve()
    assert first.video_size == seed_project.lipsync_path.stat().st_size
    assert first.video_sha256 == hashlib.sha256(b"lipsync-source").hexdigest()
    assert (first.start_frame, first.frame_count) == (0, 25)
    assert (second.start_frame, second.frame_count) == (25, 26)
    assert timeline.total_frames == 51

    dialogue = first.dialogues[0]
    assert dialogue.order == 5
    assert dialogue.text == "first"
    assert dialogue.persisted_duration == 1.0
    assert dialogue.persisted_start_time is None
    assert dialogue.persisted_end_time is None
    assert dialogue.audio_asset_id == "audio-a"
    assert dialogue.audio_asset_project_id == seed_project.project_id
    assert dialogue.audio_asset_kind == "AUDIO"
    assert dialogue.audio_raw_path == str(seed_project.audio_a_path)
    assert dialogue.audio_path == seed_project.audio_a_path.resolve()
    assert dialogue.audio_size == seed_project.audio_a_path.stat().st_size
    assert dialogue.audio_sha256 == hashlib.sha256(seed_project.audio_a_path.read_bytes()).hexdigest()
    assert (dialogue.start_ms, dialogue.end_ms) == (0, 1_000)
    assert (second.dialogues[0].start_ms, second.dialogues[0].end_ms) == (1_500, 2_000)

    assert timeline.workflow_hash == hashlib.sha256(timeline.canonical_json.encode("utf-8")).hexdigest()
    payload = json.loads(timeline.canonical_json)
    assert payload["project_id"] == seed_project.project_id
    assert payload["scenes"][0]["shots"][0]["video_sha256"] == first.video_sha256
    with pytest.raises(FrozenInstanceError):
        first.order = 99


@pytest.mark.parametrize("duplicate", ["scene", "shot", "dialogue"])
def test_timeline_rejects_duplicate_order(seed_project, duplicate):
    with session_scope(seed_project.database) as session:
        if duplicate == "scene":
            session.add(
                Scene(
                    id="duplicate-scene",
                    project_id=seed_project.project_id,
                    order=10,
                    title="Duplicate",
                    description="Duplicate",
                )
            )
        elif duplicate == "shot":
            session.add(
                Shot(
                    id="duplicate-shot",
                    scene_id=seed_project.scene_first_id,
                    order=7,
                    title="Duplicate",
                    description="Duplicate",
                    duration=1.0,
                    video_asset_id="video-asset",
                )
            )
        else:
            session.add(
                Dialogue(
                    id="duplicate-dialogue",
                    shot_id="shot-lipsync",
                    order=5,
                    text="duplicate",
                    audio_asset_id="audio-b",
                    duration=0.5,
                )
            )

    with pytest.raises(ValueError, match=rf"duplicate {duplicate} order"):
        build_render_timeline(seed_project.database, seed_project.project_id)


def _update(seed_project, model, object_id: str, **values) -> None:
    with session_scope(seed_project.database) as session:
        record = session.get(model, object_id)
        assert record is not None
        for key, value in values.items():
            setattr(record, key, value)


@pytest.mark.parametrize(
    ("asset_id", "field", "value", "message"),
    [
        ("lipsync-asset", "project_id", "other-project", "project-owned"),
        ("lipsync-asset", "kind", "VIDEO", "LIPSYNC"),
        ("audio-a", "project_id", "other-project", "project-owned"),
        ("audio-a", "kind", "VIDEO", "AUDIO"),
    ],
)
def test_timeline_rejects_wrong_asset_ownership_or_kind(
    seed_project, asset_id, field, value, message
):
    if field == "project_id":
        with session_scope(seed_project.database) as session:
            session.add(Project(id="other-project", name="Other"))
    _update(seed_project, Asset, asset_id, **{field: value})

    with pytest.raises(ValueError, match=message):
        build_render_timeline(seed_project.database, seed_project.project_id)


@pytest.mark.parametrize("condition", ["missing", "empty", "corrupt"])
def test_timeline_rejects_missing_empty_or_corrupt_video(
    seed_project, monkeypatch, condition
):
    if condition == "missing":
        seed_project.lipsync_path.unlink()
    elif condition == "empty":
        seed_project.lipsync_path.write_bytes(b"")
    else:
        monkeypatch.setattr(
            "app.services.render_timeline.probe_video",
            lambda _path: (_ for _ in ()).throw(ValueError("invalid video file")),
        )

    with pytest.raises(ValueError, match="video|media|missing|empty"):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_timeline_rejects_video_shorter_than_editorial_duration(
    seed_project, monkeypatch
):
    monkeypatch.setattr(
        "app.services.render_timeline.probe_video",
        lambda _path: VideoInfo(
            codec="h264", width=640, height=368, fps=25.0, frames=24, duration=0.9
        ),
    )

    with pytest.raises(ValueError, match="editorial duration"):
        build_render_timeline(seed_project.database, seed_project.project_id)


@pytest.mark.parametrize(
    ("fps", "duration"),
    [(float("nan"), 4.0), (25.0, float("inf"))],
)
def test_timeline_rejects_nonfinite_video_probe_metadata(
    seed_project, monkeypatch, fps, duration
):
    monkeypatch.setattr(
        "app.services.render_timeline.probe_video",
        lambda _path: VideoInfo(
            codec="h264", width=640, height=368, fps=fps, frames=100, duration=duration
        ),
    )

    with pytest.raises(ValueError, match="invalid video probe"):
        build_render_timeline(seed_project.database, seed_project.project_id)


@pytest.mark.parametrize(
    ("model", "object_id", "field", "value", "message"),
    [
        (Shot, "shot-lipsync", "duration", 0.0, "Shot duration"),
        (Dialogue, "dialogue-a", "duration", 0.0, "Dialogue duration"),
        (Dialogue, "dialogue-a", "text", " \n ", "text"),
        (Dialogue, "dialogue-a", "start_time", 0.0, "both start_time and end_time"),
        (Dialogue, "dialogue-b", "start_time", -0.1, "nonnegative"),
        (Dialogue, "dialogue-b", "end_time", 0.4, "after start_time"),
        (Dialogue, "dialogue-a", "duration", 0.5, "WAV duration"),
    ],
)
def test_timeline_rejects_invalid_durations_text_or_times(
    seed_project, model, object_id, field, value, message
):
    _update(seed_project, model, object_id, **{field: value})

    with pytest.raises(ValueError, match=message):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_timeline_rejects_dialogue_overflow(seed_project):
    _update(seed_project, Dialogue, "dialogue-b", start_time=0.6, end_time=1.1)

    with pytest.raises(ValueError, match="Shot duration"):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_timeline_rejects_overlapping_dialogues(seed_project):
    _update(
        seed_project,
        Dialogue,
        "dialogue-b",
        start_time=0.1,
        end_time=0.6,
    )
    with session_scope(seed_project.database) as session:
        session.add(
            Dialogue(
                id="overlap",
                shot_id="shot-video",
                order=10,
                text="overlap",
                audio_asset_id="audio-b",
                duration=0.5,
                start_time=0.5,
                end_time=1.0,
            )
        )

    with pytest.raises(ValueError, match="overlap"):
        build_render_timeline(seed_project.database, seed_project.project_id)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_lipsync", "LIPSYNC"),
        ("multiple_dialogues", "exactly one Dialogue"),
        ("not_zero", "starting at zero"),
        ("missing_video", "VIDEO"),
    ],
)
def test_timeline_enforces_eligible_shot_source_and_dialogue_rules(
    seed_project, mutation, message
):
    if mutation == "missing_lipsync":
        _update(seed_project, Shot, "shot-lipsync", lipsync_asset_id=None)
    elif mutation == "multiple_dialogues":
        with session_scope(seed_project.database) as session:
            session.add(
                Dialogue(
                    id="extra-dialogue",
                    shot_id="shot-lipsync",
                    order=6,
                    text="extra",
                    audio_asset_id="audio-b",
                    duration=0.5,
                )
            )
    elif mutation == "not_zero":
        _update(
            seed_project,
            Shot,
            "shot-lipsync",
            duration=1.2,
        )
        _update(
            seed_project,
            Dialogue,
            "dialogue-a",
            start_time=0.1,
            end_time=1.1,
        )
    else:
        _update(seed_project, Shot, "shot-video", video_asset_id=None)

    with pytest.raises(ValueError, match=message):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_timeline_rejects_corrupt_wav(seed_project):
    seed_project.audio_a_path.write_bytes(b"not-a-wav")

    with pytest.raises(ValueError, match="Invalid WAV"):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_timeline_rejects_projects_without_shots(seed_project):
    with session_scope(seed_project.database) as session:
        for scene in session.execute(
            select(Scene).where(Scene.project_id == seed_project.project_id)
        ).scalars():
            session.delete(scene)

    with pytest.raises(ValueError, match="at least one Shot"):
        build_render_timeline(seed_project.database, seed_project.project_id)


def test_stale_timeline_rejects_concurrent_database_change(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)
    _update(seed_project, Dialogue, "dialogue-a", text="changed")

    with pytest.raises(
        RuntimeError, match="project timeline changed during Phase 9 render"
    ):
        assert_render_timeline_unchanged(seed_project.database, timeline)


def test_stale_timeline_accepts_unchanged_snapshot(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)

    assert_render_timeline_unchanged(seed_project.database, timeline)


def test_stale_timeline_rejects_changed_input_bytes(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)
    seed_project.lipsync_path.write_bytes(b"changed-source")

    with pytest.raises(
        RuntimeError, match="project timeline changed during Phase 9 render"
    ):
        assert_render_timeline_unchanged(seed_project.database, timeline)


def test_stale_timeline_starts_immediate_caller_publication_transaction(seed_project):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)

    with session_scope(seed_project.database) as session:
        assert_render_timeline_unchanged(session, timeline)
        assert session.in_transaction()


def test_stale_timeline_rejects_caller_sqlite_transaction_autobegun_by_read(
    seed_project,
):
    timeline = build_render_timeline(seed_project.database, seed_project.project_id)

    with session_scope(seed_project.database) as session:
        assert session.execute(
            select(Project.id).where(Project.id == seed_project.project_id)
        ).scalar_one() == seed_project.project_id
        assert session.in_transaction()

        with pytest.raises(
            RuntimeError,
            match="clean Session so it can start BEGIN IMMEDIATE",
        ):
            assert_render_timeline_unchanged(session, timeline)
