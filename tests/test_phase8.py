import asyncio
import copy
import hashlib
import json
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot


TARGET_DURATION = 1.0
PROVIDER_VERSION = "musetalk-1.5-test"


def _ffmpeg(path: Path, *arguments: str) -> Path:
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


def _video(
    path: Path,
    *,
    duration: float = 1.125,
    width: int = 640,
    height: int = 368,
    fps: int = 16,
    codec: str = "libx264",
) -> Path:
    return _ffmpeg(
        path,
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x294766:s={width}x{height}:r={fps}:d={duration:.6f}",
        "-c:v",
        codec,
        "-preset",
        "ultrafast" if codec == "libx264" else "medium",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-fps_mode",
        "cfr",
        "-an",
    )


def _wav(path: Path, *, duration: float = TARGET_DURATION, sample_rate: int = 24000) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * int(duration * sample_rate))
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture(scope="session")
def phase8_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase8-media")
    malformed_video = root / "malformed.mp4"
    malformed_video.write_bytes(b"not a playable video")
    malformed_audio = root / "malformed.wav"
    malformed_audio.write_bytes(b"not a PCM WAV")
    return {
        "video": _video(root / "source.mp4"),
        "short_video": _video(root / "short-source.mp4", duration=0.5),
        "wrong_codec_video": _video(root / "wrong-codec.mp4", codec="mpeg4"),
        "wrong_size_video": _video(root / "wrong-size.mp4", width=320, height=240),
        "wrong_fps_video": _video(root / "wrong-fps.mp4", fps=25),
        "malformed_video": malformed_video,
        "audio": _wav(root / "speech.wav"),
        "other_audio": _wav(root / "other-speech.wav"),
        "malformed_audio": malformed_audio,
    }


@dataclass(frozen=True)
class SeededShot:
    database: str
    project_id: str
    other_project_id: str
    shot_id: str
    source_asset_id: str
    audio_asset_id: str
    dialogue_id: str | None


def _seed_shot(
    tmp_path: Path,
    phase8_media,
    *,
    requires_lip_sync: bool = True,
    speaker_visible: bool = True,
    dialogue_count: int = 1,
    source_link: bool = True,
    audio_link: bool = True,
    source_kind: str = "VIDEO",
    audio_kind: str = "AUDIO",
    source_owned: bool = True,
    audio_owned: bool = True,
    source_path: Path | None = None,
    audio_path: Path | None = None,
    dialogue_duration: float | None = TARGET_DURATION,
) -> SeededShot:
    database = str(tmp_path / "phase8.db")
    create_schema(database)
    source_path = phase8_media["video"] if source_path is None else source_path
    audio_path = phase8_media["audio"] if audio_path is None else audio_path

    with session_scope(database) as session:
        project = Project(name="Phase 8")
        other_project = Project(name="Other project")
        session.add_all([project, other_project])
        session.flush()
        scene = Scene(project_id=project.id, order=1, title="Close-up", description="Dialogue")
        session.add(scene)
        session.flush()
        source = Asset(
            project_id=project.id if source_owned else other_project.id,
            kind=source_kind,
            path=str(source_path),
            mime_type="video/mp4",
        )
        audio = Asset(
            project_id=project.id if audio_owned else other_project.id,
            kind=audio_kind,
            path=str(audio_path),
            mime_type="audio/wav",
        )
        session.add_all([source, audio])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            order=1,
            title="Speaking close-up",
            description="The visible speaker delivers one line",
            duration=TARGET_DURATION,
            video_asset_id=source.id if source_link else None,
            requires_lip_sync=requires_lip_sync,
            speaker_visible=speaker_visible,
            status="VIDEO_GENERATED",
        )
        session.add(shot)
        session.flush()
        dialogues = []
        for order in range(1, dialogue_count + 1):
            dialogue = Dialogue(
                shot_id=shot.id,
                order=order,
                text=f"Line {order}",
                duration=dialogue_duration,
                audio_asset_id=audio.id if audio_link else None,
            )
            session.add(dialogue)
            dialogues.append(dialogue)
        session.flush()
        return SeededShot(
            database=database,
            project_id=project.id,
            other_project_id=other_project.id,
            shot_id=shot.id,
            source_asset_id=source.id,
            audio_asset_id=audio.id,
            dialogue_id=dialogues[0].id if dialogues else None,
        )


def _run_lipsync(seed: SeededShot, provider, output_dir: Path, *, project_id: str | None = None, shot_id: str | None = None):
    from app.services.lipsync_generation import generate_shot_lipsync

    return asyncio.run(
        generate_shot_lipsync(
            seed.database,
            project_id or seed.project_id,
            shot_id or seed.shot_id,
            provider,
            output_dir,
        )
    )


def _database_snapshot(seed: SeededShot) -> dict:
    with session_scope(seed.database) as session:
        shot = session.get(Shot, seed.shot_id)
        return {
            "asset_count": session.query(Asset).count(),
            "manifest_count": session.query(GenerationManifest).count(),
            "video_asset_id": shot.video_asset_id,
            "lipsync_asset_id": shot.lipsync_asset_id,
            "status": shot.status,
            "requires_lip_sync": shot.requires_lip_sync,
            "speaker_visible": shot.speaker_visible,
        }


def _assert_no_lipsync_success(seed: SeededShot) -> None:
    with session_scope(seed.database) as session:
        shot = session.get(Shot, seed.shot_id)
        assert shot.lipsync_asset_id is None
        assert shot.status != "LIPSYNC_GENERATED"
        assert session.query(Asset).filter_by(kind="LIPSYNC").count() == 0
        assert session.query(GenerationManifest).filter_by(provider="musetalk").count() == 0


def _valid_manifest(**overrides) -> dict:
    manifest = {
        "provider": "musetalk",
        "provider_version": PROVIDER_VERSION,
        "model_name": "musetalk-v1.5",
        "workflow_name": "musetalk_lipsync",
        "generation_time": 0.25,
    }
    manifest.update(overrides)
    return manifest


def _bound_manifest(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    metadata: dict,
) -> dict:
    output_sha256 = _sha256(output_path) if output_path.is_file() else "0" * 64
    return {
        **_valid_manifest(),
        "model": "musetalk-v1.5",
        "workflow": "musetalk_lipsync",
        "target_duration": metadata["target_duration"],
        "metadata": {
            "project_id": metadata["project_id"],
            "shot_id": metadata["shot_id"],
            "input_assets": list(metadata["input_assets"]),
        },
        "source_video": {
            "path": str(video_path.resolve()),
            "sha256": _sha256(video_path),
        },
        "source_audio": {
            "path": str(audio_path.resolve()),
            "sha256": _sha256(audio_path),
        },
        "output_path": str(output_path.resolve()),
        "sha256": output_sha256,
        "output_sha256": output_sha256,
    }


class BoundaryProvider:
    def __init__(self, *, error: Exception | None = None):
        self.error = error or RuntimeError("provider boundary sentinel")
        self.calls = []

    async def generate(self, video_path, audio_path, output_dir, metadata):
        self.calls.append((video_path, audio_path, output_dir, metadata))
        raise self.error


class DeterministicMuseTalkProvider:
    def __init__(
        self,
        *,
        mode: str = "valid",
        manifest: object | None = None,
        manifest_mutation: str | None = None,
        mutate=None,
        barrier: threading.Barrier | None = None,
    ):
        self.mode = mode
        self.manifest = manifest
        self.manifest_mutation = manifest_mutation
        self.mutate = mutate
        self.barrier = barrier
        self.calls = []

    async def generate(self, video_path: Path, audio_path: Path, output_dir: Path, metadata: dict):
        assert video_path.is_absolute() and video_path.is_file()
        assert audio_path.is_absolute() and audio_path.is_file()
        assert output_dir.is_absolute() and output_dir.is_dir()
        self.calls.append((video_path, audio_path, output_dir, metadata))
        if self.mode == "exception":
            raise RuntimeError("MuseTalk failed")

        output = (
            output_dir.parent / "escaped-lipsync.mp4"
            if self.mode == "escaped_output"
            else output_dir / ("lipsync.mkv" if self.mode == "matroska" else "lipsync.mp4")
        )
        manifest_path = (
            output_dir.parent / "escaped-lipsync.manifest.json"
            if self.mode == "escaped_manifest"
            else output_dir / "lipsync.manifest.json"
        )
        target = float(metadata["target_duration"])

        if self.mode == "missing_output":
            pass
        elif self.mode == "malformed_output":
            output.write_bytes(b"not an MP4")
        elif self.mode == "video_only":
            _ffmpeg(
                output,
                "-i",
                str(video_path),
                "-vf",
                "fps=25,scale=640:368",
                "-t",
                f"{target:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "25",
                "-an",
            )
        elif self.mode in {"short", "av_delta"}:
            video_duration = target - 0.2 if self.mode == "short" else target + 0.15
            audio_duration = target - 0.2 if self.mode == "short" else target
            _ffmpeg(
                output,
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x294766:s=640x368:r=25:d={video_duration:.6f}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=48000:cl=mono:d={audio_duration:.6f}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "25",
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "1",
            )
        else:
            width, height = (320, 240) if self.mode == "wrong_size" else (640, 368)
            fps = 24 if self.mode == "wrong_fps" else 25
            video_codec = "mpeg4" if self.mode == "wrong_video_codec" else "libx264"
            pixel_format = "yuv444p" if self.mode == "wrong_pixel_format" else "yuv420p"
            audio_codec = "libmp3lame" if self.mode == "wrong_audio_codec" else "aac"
            _ffmpeg(
                output,
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                f"fps={fps},scale={width}:{height}",
                "-t",
                f"{target:.6f}",
                "-c:v",
                video_codec,
                "-preset",
                "ultrafast" if video_codec == "libx264" else "medium",
                "-pix_fmt",
                pixel_format,
                "-r",
                str(fps),
                "-fps_mode",
                "cfr",
                "-c:a",
                audio_codec,
                "-ar",
                "48000",
                "-ac",
                "1",
                "-shortest",
            )

        if self.mode != "missing_manifest":
            if self.mode == "malformed_manifest":
                manifest_path.write_text("{not JSON", encoding="utf-8")
            elif self.mode == "empty_manifest":
                manifest_path.write_bytes(b"")
            else:
                manifest = (
                    _bound_manifest(video_path, audio_path, output, metadata)
                    if self.manifest is None
                    else copy.deepcopy(self.manifest)
                )
                if self.manifest_mutation == "project_id":
                    manifest["metadata"]["project_id"] = "other-project"
                elif self.manifest_mutation == "shot_id":
                    manifest["metadata"]["shot_id"] = "other-shot"
                elif self.manifest_mutation == "input_assets":
                    manifest["metadata"]["input_assets"] = list(reversed(metadata["input_assets"]))
                elif self.manifest_mutation == "target_duration":
                    manifest["target_duration"] = target + 0.5
                elif self.manifest_mutation == "source_video_path":
                    manifest["source_video"]["path"] = str(audio_path.resolve())
                elif self.manifest_mutation == "source_video_sha256":
                    manifest["source_video"]["sha256"] = "0" * 64
                elif self.manifest_mutation == "source_audio_path":
                    manifest["source_audio"]["path"] = str(video_path.resolve())
                elif self.manifest_mutation == "source_audio_sha256":
                    manifest["source_audio"]["sha256"] = "0" * 64
                elif self.manifest_mutation == "output_path":
                    manifest["output_path"] = str(video_path.resolve())
                elif self.manifest_mutation == "output_sha256":
                    manifest["output_sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        if self.mutate is not None:
            self.mutate()
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        returned_manifest = (
            Path("..") / manifest_path.name
            if self.mode == "escaped_manifest"
            else manifest_path
        )
        return output, returned_manifest


@pytest.mark.parametrize(
    ("requires_lip_sync", "speaker_visible", "eligible"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_lipsync_gate_matrix_has_no_side_effects_for_ineligible_shots(
    tmp_path,
    phase8_media,
    requires_lip_sync,
    speaker_visible,
    eligible,
):
    seed = _seed_shot(
        tmp_path,
        phase8_media,
        requires_lip_sync=requires_lip_sync,
        speaker_visible=speaker_visible,
    )
    provider = BoundaryProvider()
    before = _database_snapshot(seed)

    if eligible:
        with pytest.raises(RuntimeError, match="provider boundary sentinel"):
            _run_lipsync(seed, provider, tmp_path / "output")
        assert len(provider.calls) == 1
    else:
        assert _run_lipsync(seed, provider, tmp_path / "output") is None
        assert provider.calls == []

    assert _database_snapshot(seed) == before


def test_lipsync_happy_path_persists_validated_output_and_preserves_source(tmp_path, phase8_media):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider()

    asset = _run_lipsync(seed, provider, tmp_path / "relative-output")

    assert len(provider.calls) == 1
    video_path, audio_path, output_dir, metadata = provider.calls[0]
    assert video_path == phase8_media["video"].resolve()
    assert audio_path == phase8_media["audio"].resolve()
    assert output_dir == (tmp_path / "relative-output").resolve()
    assert metadata == {
        "project_id": seed.project_id,
        "shot_id": seed.shot_id,
        "input_assets": [seed.source_asset_id, seed.audio_asset_id],
        "target_duration": TARGET_DURATION,
    }

    with session_scope(seed.database) as session:
        shot = session.get(Shot, seed.shot_id)
        persisted = session.get(Asset, asset.id)
        source = session.get(Asset, seed.source_asset_id)
        manifest = session.query(GenerationManifest).filter_by(asset_id=asset.id).one()

        assert shot.video_asset_id == seed.source_asset_id
        assert source.path == str(phase8_media["video"])
        assert shot.lipsync_asset_id == asset.id
        assert shot.status == "LIPSYNC_GENERATED"
        assert persisted.kind == "LIPSYNC"
        assert persisted.mime_type == "video/mp4"
        assert Path(persisted.path).is_absolute()
        assert persisted.metadata_json == {
            "manifest_path": str((tmp_path / "relative-output" / "lipsync.manifest.json").resolve()),
            "provider": "musetalk",
            "provider_version": PROVIDER_VERSION,
            "model_name": "musetalk-v1.5",
            "workflow_name": "musetalk_lipsync",
            "generation_time": 0.25,
            "source_video_asset_id": seed.source_asset_id,
            "source_video_sha256": _sha256(phase8_media["video"]),
            "dialogue_id": seed.dialogue_id,
            "audio_asset_id": seed.audio_asset_id,
            "source_audio_sha256": _sha256(phase8_media["audio"]),
            "input_assets": [seed.source_asset_id, seed.audio_asset_id],
            "target_duration": TARGET_DURATION,
            "output_sha256": _sha256(Path(persisted.path)),
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": pytest.approx(TARGET_DURATION, abs=0.04),
            "video_duration": pytest.approx(TARGET_DURATION, abs=0.04),
            "audio_duration": pytest.approx(TARGET_DURATION, abs=0.04),
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "width": 640,
            "height": 368,
            "fps": pytest.approx(25.0),
            "audio_codec": "aac",
            "sample_rate": 48000,
            "channels": 1,
        }
        assert manifest.provider == "musetalk"
        assert manifest.provider_version == PROVIDER_VERSION
        assert manifest.model_name == "musetalk-v1.5"
        assert manifest.workflow_name == "musetalk_lipsync"
        assert manifest.generation_time == pytest.approx(0.25)
        assert manifest.seed is None
        assert manifest.input_assets == [seed.source_asset_id, seed.audio_asset_id]
        assert manifest.asset_id == asset.id
        assert manifest.output_asset == asset.id


@pytest.mark.parametrize("identity", ["wrong_project", "missing_shot"])
def test_lipsync_rejects_wrong_project_or_shot_before_gate(tmp_path, phase8_media, identity):
    seed = _seed_shot(
        tmp_path,
        phase8_media,
        requires_lip_sync=False,
        speaker_visible=False,
    )
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises(ValueError, match="shot does not belong to project"):
        _run_lipsync(
            seed,
            provider,
            tmp_path / "output",
            project_id=seed.other_project_id if identity == "wrong_project" else None,
            shot_id="missing-shot" if identity == "missing_shot" else None,
        )

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize("dialogue_count", [0, 2])
def test_lipsync_requires_exactly_one_dialogue(tmp_path, phase8_media, dialogue_count):
    seed = _seed_shot(tmp_path, phase8_media, dialogue_count=dialogue_count)
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises(ValueError, match="exactly one dialogue"):
        _run_lipsync(seed, provider, tmp_path / "output")

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_source_link", "source video"),
        ("missing_audio_link", "audio"),
        ("missing_source_file", "source video"),
        ("missing_audio_file", "WAV"),
        ("malformed_source", "video"),
        ("malformed_audio", "WAV"),
    ],
)
def test_lipsync_rejects_missing_or_malformed_inputs_before_provider(tmp_path, phase8_media, case, message):
    source_path = phase8_media["video"]
    audio_path = phase8_media["audio"]
    if case == "missing_source_file":
        source_path = tmp_path / "missing-source.mp4"
    elif case == "missing_audio_file":
        audio_path = tmp_path / "missing-audio.wav"
    elif case == "malformed_source":
        source_path = phase8_media["malformed_video"]
    elif case == "malformed_audio":
        audio_path = phase8_media["malformed_audio"]
    seed = _seed_shot(
        tmp_path,
        phase8_media,
        source_link=case != "missing_source_link",
        audio_link=case != "missing_audio_link",
        source_path=source_path,
        audio_path=audio_path,
    )
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises((ValueError, FileNotFoundError), match=message):
        _run_lipsync(seed, provider, tmp_path / "output")

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("source_kind", "source video asset"),
        ("source_owner", "source video asset"),
        ("audio_kind", "dialogue audio asset"),
        ("audio_owner", "dialogue audio asset"),
    ],
)
def test_lipsync_rejects_wrong_input_asset_kind_or_ownership(tmp_path, phase8_media, case, message):
    seed = _seed_shot(
        tmp_path,
        phase8_media,
        source_kind="AUDIO" if case == "source_kind" else "VIDEO",
        source_owned=case != "source_owner",
        audio_kind="VIDEO" if case == "audio_kind" else "AUDIO",
        audio_owned=case != "audio_owner",
    )
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises(ValueError, match=message):
        _run_lipsync(seed, provider, tmp_path / "output")

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    "source_name",
    ["wrong_codec_video", "wrong_size_video", "wrong_fps_video", "short_video"],
)
def test_lipsync_rejects_invalid_or_short_source_profile(tmp_path, phase8_media, source_name):
    seed = _seed_shot(tmp_path, phase8_media, source_path=phase8_media[source_name])
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises(ValueError, match="source video"):
        _run_lipsync(seed, provider, tmp_path / "output")

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize("duration", [None, 0.95])
def test_lipsync_rejects_missing_or_mismatched_persisted_wav_duration(tmp_path, phase8_media, duration):
    seed = _seed_shot(tmp_path, phase8_media, dialogue_duration=duration)
    provider = BoundaryProvider(error=AssertionError("provider must not run"))

    with pytest.raises(ValueError, match="duration"):
        _run_lipsync(seed, provider, tmp_path / "output")

    assert provider.calls == []
    _assert_no_lipsync_success(seed)


def test_lipsync_accepts_persisted_wav_duration_at_exact_tolerance(tmp_path, phase8_media):
    seed = _seed_shot(tmp_path, phase8_media, dialogue_duration=0.98)
    provider = DeterministicMuseTalkProvider()

    asset = _run_lipsync(seed, provider, tmp_path / "output")

    assert asset.kind == "LIPSYNC"
    with session_scope(seed.database) as session:
        assert session.get(Shot, seed.shot_id).lipsync_asset_id == asset.id


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("exception", "MuseTalk failed"),
        ("missing_output", "output"),
        ("missing_manifest", "manifest"),
        ("empty_manifest", "manifest"),
        ("malformed_manifest", "manifest"),
    ],
)
def test_lipsync_provider_failures_persist_nothing(tmp_path, phase8_media, mode, message):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider(mode=mode)

    with pytest.raises((RuntimeError, ValueError), match=message):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize("mode", ["escaped_output", "escaped_manifest"])
def test_lipsync_rejects_provider_paths_outside_output_directory(tmp_path, phase8_media, mode):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider(mode=mode)

    with pytest.raises(RuntimeError, match="outside output_dir"):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    "manifest_mutation",
    [
        "project_id",
        "shot_id",
        "input_assets",
        "target_duration",
        "source_video_path",
        "source_video_sha256",
        "source_audio_path",
        "source_audio_sha256",
        "output_path",
        "output_sha256",
    ],
)
def test_lipsync_rejects_manifest_not_bound_to_request_or_artifacts(
    tmp_path,
    phase8_media,
    manifest_mutation,
):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider(manifest_mutation=manifest_mutation)

    with pytest.raises(RuntimeError, match="manifest"):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        _valid_manifest(provider="other"),
        _valid_manifest(provider_version=""),
        _valid_manifest(model_name="musetalk-v1"),
        _valid_manifest(workflow_name="other_workflow"),
        _valid_manifest(generation_time=-0.01),
        _valid_manifest(generation_time=True),
    ],
)
def test_lipsync_rejects_invalid_manifest_provenance(tmp_path, phase8_media, manifest):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider(manifest=manifest)

    with pytest.raises(RuntimeError, match="manifest"):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("malformed_output", "media"),
        ("video_only", "audio"),
        ("wrong_video_codec", "H.264"),
        ("wrong_pixel_format", "yuv420p"),
        ("wrong_size", "640x368"),
        ("wrong_fps", "25 FPS"),
        ("wrong_audio_codec", "AAC"),
        ("short", "shorter"),
        ("av_delta", "synchronization"),
        ("matroska", "MP4/MOV"),
    ],
)
def test_lipsync_rejects_invalid_final_media_without_registration(tmp_path, phase8_media, mode, message):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider(mode=mode)

    with pytest.raises((RuntimeError, ValueError), match=message):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


@pytest.mark.parametrize(
    "mutation",
    [
        "requires_lip_sync",
        "speaker_visible",
        "source_id",
        "shot_duration",
        "shot_status",
        "audio_id",
        "dialogue_duration",
        "dialogue_count",
    ],
)
def test_lipsync_rechecks_snapshot_after_provider_and_rejects_concurrent_mutation(
    tmp_path,
    phase8_media,
    mutation,
):
    seed = _seed_shot(tmp_path, phase8_media)
    with session_scope(seed.database) as session:
        alternate_source = Asset(
            project_id=seed.project_id,
            kind="VIDEO",
            path=str(phase8_media["video"]),
            mime_type="video/mp4",
        )
        alternate_audio = Asset(
            project_id=seed.project_id,
            kind="AUDIO",
            path=str(phase8_media["other_audio"]),
            mime_type="audio/wav",
        )
        session.add_all([alternate_source, alternate_audio])
        session.flush()
        alternate_source_id = alternate_source.id
        alternate_audio_id = alternate_audio.id

    def mutate():
        with session_scope(seed.database) as session:
            shot = session.get(Shot, seed.shot_id)
            dialogue = session.get(Dialogue, seed.dialogue_id)
            if mutation == "requires_lip_sync":
                shot.requires_lip_sync = False
            elif mutation == "speaker_visible":
                shot.speaker_visible = False
            elif mutation == "source_id":
                shot.video_asset_id = alternate_source_id
            elif mutation == "shot_duration":
                shot.duration = 1.2
            elif mutation == "shot_status":
                shot.status = "RENDERING"
            elif mutation == "audio_id":
                dialogue.audio_asset_id = alternate_audio_id
            elif mutation == "dialogue_duration":
                dialogue.duration = 0.9
            elif mutation == "dialogue_count":
                session.add(Dialogue(
                    shot_id=shot.id,
                    order=2,
                    text="Concurrent line",
                    audio_asset_id=seed.audio_asset_id,
                    duration=TARGET_DURATION,
                ))

    provider = DeterministicMuseTalkProvider(mutate=mutate)

    with pytest.raises(RuntimeError, match="changed during lip sync generation"):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)


def test_lipsync_serializes_two_stale_generations_and_persists_exactly_one(
    tmp_path,
    phase8_media,
    monkeypatch,
):
    from app.services import lipsync_generation

    seed = _seed_shot(tmp_path, phase8_media)
    provider_barrier = threading.Barrier(2)
    stale_check_barrier = threading.Barrier(2)
    original_assert = lipsync_generation._assert_snapshot_unchanged

    def synchronized_stale_check(session, snapshot, *args, **kwargs):
        shot = original_assert(session, snapshot, *args, **kwargs)
        try:
            stale_check_barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return shot

    monkeypatch.setattr(
        lipsync_generation,
        "_assert_snapshot_unchanged",
        synchronized_stale_check,
    )

    def generate(index):
        provider = DeterministicMuseTalkProvider(barrier=provider_barrier)
        try:
            return _run_lipsync(seed, provider, tmp_path / f"output-{index}")
        except Exception as exc:  # the losing call is part of the asserted result
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(generate, index) for index in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]

    successes = [outcome for outcome in outcomes if isinstance(outcome, Asset)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "changed during lip sync generation" in str(failures[0])

    with session_scope(seed.database) as session:
        shot = session.get(Shot, seed.shot_id)
        assets = session.query(Asset).filter_by(kind="LIPSYNC").all()
        manifests = session.query(GenerationManifest).filter_by(provider="musetalk").all()
        assert len(assets) == 1
        assert len(manifests) == 1
        assert shot.lipsync_asset_id == assets[0].id == manifests[0].asset_id
        assert manifests[0].output_asset == assets[0].id


def test_lipsync_rolls_back_asset_flush_when_later_database_work_fails(
    tmp_path,
    phase8_media,
    monkeypatch,
):
    seed = _seed_shot(tmp_path, phase8_media)
    provider = DeterministicMuseTalkProvider()
    original_flush = Session.flush

    def fail_after_lipsync_asset_flush(session, *args, **kwargs):
        has_pending_lipsync = any(
            isinstance(value, Asset) and value.kind == "LIPSYNC"
            for value in session.new
        )
        result = original_flush(session, *args, **kwargs)
        if has_pending_lipsync:
            raise RuntimeError("injected database failure after Asset flush")
        return result

    monkeypatch.setattr(Session, "flush", fail_after_lipsync_asset_flush)

    with pytest.raises(RuntimeError, match="after Asset flush"):
        _run_lipsync(seed, provider, tmp_path / "output")

    _assert_no_lipsync_success(seed)
