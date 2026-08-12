from __future__ import annotations

import hashlib
import json
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

import pytest

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot
from app.providers.ffmpeg_render_provider import (
    FFmpegIdentity,
    FFmpegRenderResult,
)
from app.services.media_probe import AVInfo, AudioStreamInfo, VideoStreamInfo
from app.services.render_generation import render_project
from app.services.render_timeline import (
    RenderProfile,
    RenderTimeline,
    TimelineDialogue,
    TimelineSceneSnapshot,
    TimelineShot,
    assert_render_timeline_unchanged as real_assert_render_timeline_unchanged,
    build_render_timeline as real_build_render_timeline,
)
from app.services.video_probe import VideoInfo


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class SeedProject:
    database: str
    project_id: str
    storage: Path
    timeline: RenderTimeline


class DeterministicProvider:
    def __init__(self, payload: bytes = b"canonical-final-video") -> None:
        self.payload = payload

    def render(self, timeline, srt, output_path, manifest_path):
        output_path = Path(output_path)
        manifest_path = Path(manifest_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self.payload)
        output_sha256 = _sha256(output_path)
        manifest_path.write_text(
            json.dumps(
                {
                    "provider": "ffmpeg",
                    "workflow": "final_render_v1",
                    "project_id": timeline.project_id,
                    "workflow_hash": timeline.workflow_hash,
                    "output_path": str(output_path.resolve()),
                    "output_sha256": output_sha256,
                    "srt_sha256": hashlib.sha256(srt).hexdigest(),
                    "cue_count": sum(len(shot.dialogues) for shot in timeline.shots),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        media = AVInfo(
            video=VideoStreamInfo("h264", "yuv420p", 640, 368, 25, 20, 0.8),
            audio=AudioStreamInfo("aac", 48_000, 2, 0.8),
            duration=0.8,
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
        )
        identity = FFmpegIdentity(
            executable=Path("C:/ffmpeg.exe"),
            version="test",
            configuration="--enable-libass",
            font_path=Path("C:/Windows/Fonts/msyh.ttc"),
            font_size=123,
            font_sha256="f" * 64,
        )
        return FFmpegRenderResult(
            output_path=output_path.resolve(),
            output_sha256=output_sha256,
            media=media,
            generation_time=0.25,
            manifest_path=manifest_path.resolve(),
            identity=identity,
        )


@pytest.fixture
def seed_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SeedProject:
    database = str(tmp_path / "phase9.db")
    create_schema(database)
    project_id = "project-1"
    video = tmp_path / "source.mp4"
    audio = tmp_path / "dialogue.wav"
    video.write_bytes(b"source-video")
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 3_200)
    with session_scope(database) as session:
        session.add(Project(id=project_id, name="Phase 9"))
        session.flush()
        session.add_all(
            [
                Asset(id="video-1", project_id=project_id, kind="VIDEO", path=str(video)),
                Asset(id="audio-1", project_id=project_id, kind="AUDIO", path=str(audio)),
            ]
        )
        session.flush()
        session.add(
            Scene(
                id="scene-1",
                project_id=project_id,
                order=0,
                title="Scene",
                description="Scene",
            )
        )
        session.flush()
        session.add(
            Shot(
                id="shot-1",
                scene_id="scene-1",
                order=0,
                title="Shot",
                description="Shot",
                duration=0.8,
                status="VIDEO_GENERATED",
                video_asset_id="video-1",
                requires_lip_sync=False,
                speaker_visible=False,
            )
        )
        session.flush()
        session.add(
            Dialogue(
                id="dialogue-1",
                shot_id="shot-1",
                order=0,
                text="你好",
                duration=0.4,
                start_time=0.0,
                end_time=0.4,
                audio_asset_id="audio-1",
            )
        )
    dialogue = TimelineDialogue(
        dialogue_id="dialogue-1", order=0, text="你好", persisted_duration=0.4,
        persisted_start_time=0.0, persisted_end_time=0.4,
        audio_asset_id="audio-1", audio_asset_project_id=project_id,
        audio_asset_kind="AUDIO", audio_raw_path=str(audio), audio_path=audio.resolve(),
        audio_size=audio.stat().st_size, audio_sha256=_sha256(audio), start_ms=0, end_ms=400,
    )
    shot = TimelineShot(
        shot_id="shot-1", scene_id="scene-1", character_id=None, order=0,
        persisted_duration=0.8, status="VIDEO_GENERATED", requires_lip_sync=False,
        speaker_visible=False, storyboard_asset_id=None, source_video_asset_id="video-1",
        source_lipsync_asset_id=None, video_asset_id="video-1",
        video_asset_project_id=project_id, video_asset_kind="VIDEO",
        video_raw_path=str(video), video_path=video.resolve(), video_size=video.stat().st_size,
        video_sha256=_sha256(video), start_frame=0, frame_count=20, dialogues=(dialogue,),
    )
    timeline = RenderTimeline(
        project_id=project_id, subtitle_asset_id=None, final_video_asset_id=None,
        profile=RenderProfile(), scenes=(TimelineSceneSnapshot("scene-1", 0, (shot,)),),
        shots=(shot,), total_frames=20, canonical_json='{"project_id":"project-1"}',
        workflow_hash="a" * 64,
    )
    monkeypatch.setattr("app.services.render_generation.build_render_timeline", lambda *_: timeline)
    monkeypatch.setattr("app.services.render_generation.assert_render_timeline_unchanged", lambda *_: None)
    monkeypatch.setattr(
        "app.services.render_timeline.probe_video",
        lambda _path: VideoInfo("h264", 640, 368, 25.0, 20, 0.8),
    )
    return SeedProject(database, project_id, tmp_path / "storage", timeline)


def test_render_project_persists_immutable_outputs_and_alias(seed_project: SeedProject):
    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    assert result.subtitle_asset.kind == "SUBTITLE"
    assert result.subtitle_asset.mime_type == "application/x-subrip"
    assert result.final_asset.kind == "FINAL_VIDEO"
    assert result.final_asset.mime_type == "video/mp4"
    assert result.published_path == (
        seed_project.storage / "projects" / seed_project.project_id / "output" / "final.mp4"
    ).resolve()
    assert _sha256(Path(result.final_asset.path)) == _sha256(result.published_path)
    assert result.alias_status == "READY"
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == result.subtitle_asset.id
        assert project.final_video_asset_id == result.final_asset.id


def test_render_project_persists_exact_asset_metadata_and_manifest_order(
    seed_project: SeedProject,
) -> None:
    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    assert result.subtitle_path.read_bytes() == (
        b"1\r\n00:00:00,000 --> 00:00:00,400\r\n\xe4\xbd\xa0\xe5\xa5\xbd\r\n"
    )
    subtitle_metadata = result.subtitle_asset.metadata_json
    assert subtitle_metadata["provider"] == "local"
    assert subtitle_metadata["workflow"] == "subtitle_srt_v1"
    assert subtitle_metadata["cue_count"] == 1
    assert subtitle_metadata["sha256"] == _sha256(result.subtitle_path)
    assert subtitle_metadata["workflow_hash"] == seed_project.timeline.workflow_hash
    final_metadata = result.final_asset.metadata_json
    assert final_metadata["provider"] == "ffmpeg"
    assert final_metadata["workflow"] == "final_render_v1"
    assert final_metadata["profile"] == {
        "width": 640,
        "height": 368,
        "fps": 25,
        "sample_rate": 48_000,
        "channels": 2,
    }
    assert final_metadata["cue_count"] == 1
    assert final_metadata["total_frames"] == 20
    assert final_metadata["duration_seconds"] == 0.8
    assert final_metadata["sha256"] == _sha256(Path(result.final_asset.path))
    assert final_metadata["immutable_path"] == result.final_asset.path
    assert final_metadata["published_path"] == str(result.published_path)
    assert final_metadata["ffmpeg"]["version"] == "test"
    assert final_metadata["font"]["sha256"] == "f" * 64
    assert final_metadata["timeline"] == [
        {
            "role": "VIDEO",
            "shot_id": "shot-1",
            "asset_id": "video-1",
            "path": str(seed_project.timeline.shots[0].video_path),
            "sha256": seed_project.timeline.shots[0].video_sha256,
            "start_frame": 0,
            "frame_count": 20,
            "audio": [
                {
                    "role": "AUDIO",
                    "asset_id": "audio-1",
                    "path": str(seed_project.timeline.shots[0].dialogues[0].audio_path),
                    "sha256": seed_project.timeline.shots[0].dialogues[0].audio_sha256,
                    "start_ms": 0,
                    "end_ms": 400,
                }
            ],
        }
    ]
    with session_scope(seed_project.database) as session:
        manifests = {
            manifest.asset_id: manifest
            for manifest in session.query(GenerationManifest).all()
        }
    subtitle_manifest = manifests[result.subtitle_asset.id]
    assert subtitle_manifest.provider == "local"
    assert subtitle_manifest.workflow_name == "subtitle_srt_v1"
    assert subtitle_manifest.workflow_hash == seed_project.timeline.workflow_hash
    assert subtitle_manifest.input_assets == ["audio-1"]
    assert subtitle_manifest.asset_id == subtitle_manifest.output_asset == result.subtitle_asset.id
    final_manifest = manifests[result.final_asset.id]
    assert final_manifest.provider == "ffmpeg"
    assert final_manifest.workflow_name == "final_render_v1"
    assert final_manifest.workflow_hash == seed_project.timeline.workflow_hash
    assert final_manifest.input_assets == [
        "video-1",
        "audio-1",
        result.subtitle_asset.id,
    ]
    assert final_manifest.asset_id == final_manifest.output_asset == result.final_asset.id


def test_zero_cue_manifest_does_not_claim_unconsumed_subtitle(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = replace(seed_project.timeline.shots[0], dialogues=())
    timeline = replace(
        seed_project.timeline,
        shots=(shot,),
        scenes=(TimelineSceneSnapshot("scene-1", 0, (shot,)),),
    )
    monkeypatch.setattr("app.services.render_generation.build_render_timeline", lambda *_: timeline)

    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    assert result.subtitle_path.read_bytes() == b""
    with session_scope(seed_project.database) as session:
        manifests = {
            manifest.asset_id: manifest
            for manifest in session.query(GenerationManifest).all()
        }
    assert manifests[result.subtitle_asset.id].input_assets == []
    assert manifests[result.final_asset.id].input_assets == ["video-1"]


def test_manifest_preserves_duplicate_audio_consumption_order(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = replace(
        seed_project.timeline.shots[0].dialogues[0],
        dialogue_id="dialogue-a",
        start_ms=0,
        end_ms=200,
    )
    second = replace(
        first,
        dialogue_id="dialogue-b",
        order=1,
        start_ms=200,
        end_ms=400,
    )
    shot = replace(seed_project.timeline.shots[0], dialogues=(first, second))
    timeline = replace(
        seed_project.timeline,
        shots=(shot,),
        scenes=(TimelineSceneSnapshot("scene-1", 0, (shot,)),),
    )
    monkeypatch.setattr("app.services.render_generation.build_render_timeline", lambda *_: timeline)

    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    with session_scope(seed_project.database) as session:
        manifests = {
            manifest.asset_id: manifest
            for manifest in session.query(GenerationManifest).all()
        }
    assert manifests[result.subtitle_asset.id].input_assets == ["audio-1", "audio-1"]
    assert manifests[result.final_asset.id].input_assets == [
        "video-1",
        "audio-1",
        "audio-1",
        result.subtitle_asset.id,
    ]


def test_provider_manifest_mismatch_is_precommit_failure(
    seed_project: SeedProject,
) -> None:
    class MismatchedProvider(DeterministicProvider):
        def render(self, *args, **kwargs):
            result = super().render(*args, **kwargs)
            result.manifest_path.write_text("{}", encoding="utf-8")
            return result

    with pytest.raises(RuntimeError, match="manifest does not match"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            MismatchedProvider(),
            seed_project.storage,
        )

    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id is None
        assert project.final_video_asset_id is None
        assert session.query(GenerationManifest).count() == 0


def test_stale_recheck_failure_preserves_project_pointers(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stale(*_args):
        raise RuntimeError("project timeline changed during Phase 9 render")

    monkeypatch.setattr("app.services.render_generation.assert_render_timeline_unchanged", stale)

    with pytest.raises(RuntimeError, match="timeline changed"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            DeterministicProvider(),
            seed_project.storage,
        )

    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id is None
        assert project.final_video_asset_id is None


def test_alias_failure_after_commit_returns_degraded_authoritative_result(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.render_generation as render_generation

    monkeypatch.setattr(
        render_generation,
        "_atomic_alias",
        lambda *_args: (_ for _ in ()).throw(OSError("alias disk failure")),
    )

    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    assert result.alias_status == "DEGRADED"
    assert "alias disk failure" in result.alias_error
    assert Path(result.final_asset.path).is_file()
    assert not result.published_path.exists()
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == result.subtitle_asset.id
        assert project.final_video_asset_id == result.final_asset.id


def test_subtitle_bytes_changed_during_provider_render_are_not_registered(
    seed_project: SeedProject,
) -> None:
    class SubtitleMutatingProvider(DeterministicProvider):
        def render(self, timeline, srt, output_path, manifest_path):
            result = super().render(timeline, srt, output_path, manifest_path)
            subtitle = next(Path(output_path).parents[1].joinpath("subtitles").glob("*.srt"))
            subtitle.write_bytes(b"corrupt")
            return result

    with pytest.raises(RuntimeError, match="subtitle.*mismatch"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            SubtitleMutatingProvider(),
            seed_project.storage,
        )

    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id is None
        assert project.final_video_asset_id is None


def test_stale_loser_reconciles_missing_current_alias_before_rejection(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(b"accepted-old-final"),
        seed_project.storage,
    )
    accepted_hash = _sha256(Path(first.final_asset.path))
    first.published_path.unlink()

    def stale(*_args):
        raise RuntimeError("project timeline changed during Phase 9 render")

    monkeypatch.setattr("app.services.render_generation.assert_render_timeline_unchanged", stale)

    with pytest.raises(RuntimeError, match="timeline changed"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            DeterministicProvider(b"rejected-new-final"),
            seed_project.storage,
        )

    assert _sha256(first.published_path) == accepted_hash
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == first.subtitle_asset.id
        assert project.final_video_asset_id == first.final_asset.id


def test_two_concurrent_renders_commit_exactly_one_coherent_winner(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)

    class BarrierProvider(DeterministicProvider):
        def render(self, *args, **kwargs):
            result = super().render(*args, **kwargs)
            barrier.wait(timeout=5)
            return result

    def pointer_stale_check(session, timeline):
        project = session.get(Project, timeline.project_id)
        if (
            project.subtitle_asset_id != timeline.subtitle_asset_id
            or project.final_video_asset_id != timeline.final_video_asset_id
        ):
            raise RuntimeError("project timeline changed during Phase 9 render")

    monkeypatch.setattr(
        "app.services.render_generation.assert_render_timeline_unchanged",
        pointer_stale_check,
    )

    def attempt(payload: bytes):
        try:
            return render_project(
                seed_project.database,
                seed_project.project_id,
                BarrierProvider(payload),
                seed_project.storage,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [b"winner-a", b"winner-b"]))

    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == len(losers) == 1
    assert winners[0].alias_status == "READY"
    assert "timeline changed" in str(losers[0])
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == winners[0].subtitle_asset.id
        assert project.final_video_asset_id == winners[0].final_asset.id
    assert _sha256(winners[0].published_path) == _sha256(Path(winners[0].final_asset.path))


def test_reconciliation_rejects_authoritative_asset_metadata_hash_mismatch(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(b"accepted-old-final"),
        seed_project.storage,
    )
    first.published_path.unlink()
    with session_scope(seed_project.database) as session:
        asset = session.get(Asset, first.final_asset.id)
        asset.metadata_json = {**asset.metadata_json, "sha256": "0" * 64}
    monkeypatch.setattr(
        "app.services.render_generation.assert_render_timeline_unchanged",
        lambda *_: (_ for _ in ()).throw(RuntimeError("should not reach stale check")),
    )

    with pytest.raises(RuntimeError, match="metadata.*hash"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            DeterministicProvider(b"rejected-new-final"),
            seed_project.storage,
        )

    assert not first.published_path.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "scene_order",
        "shot_order",
        "dialogue_order",
        "text",
        "duration",
        "flags",
        "links",
        "asset_path",
        "project_pointer",
        "input_bytes",
    ],
)
def test_real_stale_recheck_rejects_every_explicit_snapshot_change(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    monkeypatch.setattr(
        "app.services.render_generation.build_render_timeline",
        real_build_render_timeline,
    )
    monkeypatch.setattr(
        "app.services.render_generation.assert_render_timeline_unchanged",
        real_assert_render_timeline_unchanged,
    )
    alternate = tmp_path / "alternate.mp4"
    alternate.write_bytes(b"alternate")

    class MutatingProvider(DeterministicProvider):
        def render(self, *args, **kwargs):
            result = super().render(*args, **kwargs)
            if mutation == "input_bytes":
                seed_project.timeline.shots[0].video_path.write_bytes(b"changed input")
                return result
            with session_scope(seed_project.database) as session:
                if mutation == "scene_order":
                    session.get(Scene, "scene-1").order = 1
                elif mutation == "shot_order":
                    session.get(Shot, "shot-1").order = 1
                elif mutation == "dialogue_order":
                    session.get(Dialogue, "dialogue-1").order = 1
                elif mutation == "text":
                    session.get(Dialogue, "dialogue-1").text = "changed"
                elif mutation == "duration":
                    session.get(Dialogue, "dialogue-1").duration = 0.41
                elif mutation == "flags":
                    session.get(Shot, "shot-1").speaker_visible = True
                elif mutation == "links":
                    session.get(Shot, "shot-1").storyboard_asset_id = "audio-1"
                elif mutation == "asset_path":
                    session.get(Asset, "video-1").path = str(alternate)
                elif mutation == "project_pointer":
                    session.get(Project, seed_project.project_id).subtitle_asset_id = "audio-1"
            return result

    with pytest.raises(RuntimeError, match="timeline changed"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            MutatingProvider(),
            seed_project.storage,
        )

    with session_scope(seed_project.database) as session:
        assert session.query(Asset).filter(Asset.kind == "FINAL_VIDEO").count() == 0
        assert session.query(GenerationManifest).count() == 0
        project = session.get(Project, seed_project.project_id)
        assert project.final_video_asset_id is None
        if mutation == "project_pointer":
            assert project.subtitle_asset_id == "audio-1"
        else:
            assert project.subtitle_asset_id is None


def test_provider_failure_preserves_previously_accepted_output(
    seed_project: SeedProject,
) -> None:
    first = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(b"accepted-final"),
        seed_project.storage,
    )
    accepted_alias = first.published_path.read_bytes()

    class FailingProvider:
        def render(self, _timeline, _srt, output_path, _manifest_path):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"partial rejected bytes")
            raise RuntimeError("provider failure")

    with pytest.raises(RuntimeError, match="provider failure"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            FailingProvider(),
            seed_project.storage,
        )

    assert first.published_path.read_bytes() == accepted_alias
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == first.subtitle_asset.id
        assert project.final_video_asset_id == first.final_asset.id


def test_atomic_alias_replace_failure_returns_degraded_and_cleans_temp(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.render_generation as render_generation

    original_replace = render_generation.os.replace

    def fail_alias_replace(source, destination):
        if Path(destination).name == "final.mp4":
            raise OSError("atomic replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(render_generation.os, "replace", fail_alias_replace)

    result = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(),
        seed_project.storage,
    )

    assert result.alias_status == "DEGRADED"
    assert "atomic replace failure" in result.alias_error
    assert not result.published_path.exists()
    assert not list(result.published_path.parent.glob("*.tmp"))
    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.final_video_asset_id == result.final_asset.id


def test_alias_reconciliation_failure_is_precommit_and_preserves_old_pointer(
    seed_project: SeedProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = render_project(
        seed_project.database,
        seed_project.project_id,
        DeterministicProvider(b"accepted-final"),
        seed_project.storage,
    )
    first.published_path.unlink()
    import app.services.render_generation as render_generation

    monkeypatch.setattr(
        render_generation,
        "_atomic_alias",
        lambda *_args: (_ for _ in ()).throw(OSError("reconcile failure")),
    )

    with pytest.raises(OSError, match="reconcile failure"):
        render_project(
            seed_project.database,
            seed_project.project_id,
            DeterministicProvider(b"rejected-final"),
            seed_project.storage,
        )

    with session_scope(seed_project.database) as session:
        project = session.get(Project, seed_project.project_id)
        assert project.subtitle_asset_id == first.subtitle_asset.id
        assert project.final_video_asset_id == first.final_asset.id
        assert session.query(Asset).filter(Asset.kind == "FINAL_VIDEO").count() == 1


def test_project_lock_serializes_publication_critical_section(
    seed_project: SeedProject,
    tmp_path: Path,
) -> None:
    import app.services.render_generation as render_generation

    lock_path = tmp_path / "project.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with render_generation._project_lock(lock_path):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second():
        assert first_entered.wait(timeout=5)
        with render_generation._project_lock(lock_path):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        second_future = pool.submit(second)
        assert first_entered.wait(timeout=5)
        assert not second_entered.wait(timeout=0.15)
        release_first.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    assert second_entered.is_set()
