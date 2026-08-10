"""Validate and persist one MuseTalk lip-sync generation for a dialogue shot."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

from sqlalchemy import text

from app.db.session import session_scope
from app.models import Asset, Dialogue, GenerationManifest, Scene, Shot
from app.services.audio_probe import probe_wav
from app.services.media_probe import AVInfo, probe_av
from app.services.video_probe import probe_video


_SOURCE_WIDTH = 640
_SOURCE_HEIGHT = 368
_SOURCE_FPS = 16.0
_OUTPUT_WIDTH = 640
_OUTPUT_HEIGHT = 368
_OUTPUT_FPS = 25.0
_WAV_DURATION_TOLERANCE = 0.02
_OUTPUT_DURATION_TOLERANCE = 1 / _OUTPUT_FPS
_OUTPUT_AV_TOLERANCE = 0.08
_TOLERANCE_EPSILON = 1e-9
_MODEL_NAME = "musetalk-v1.5"
_WORKFLOW_NAME = "musetalk_lipsync"
_MP4_MOV_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}


@dataclass(frozen=True)
class _InputSnapshot:
    project_id: str
    shot_id: str
    scene_id: str
    requires_lip_sync: bool
    speaker_visible: bool
    source_asset_id: str
    source_path: str
    dialogue_id: str
    audio_asset_id: str
    audio_path: str
    dialogue_duration: float
    shot_duration: float
    shot_status: str
    lipsync_asset_id: str | None


@dataclass(frozen=True)
class _ProviderProvenance:
    provider_version: str
    generation_time: float
    seed: int | None


@dataclass(frozen=True)
class _ValidatedInputs:
    video_path: Path
    audio_path: Path
    video_sha256: str
    audio_sha256: str


def _owned_shot(
    session,
    project_id: str,
    shot_id: str,
    *,
    for_update: bool = False,
) -> Shot | None:
    query = (
        session.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Shot.id == shot_id, Scene.project_id == project_id)
    )
    if for_update:
        query = query.with_for_update()
    return query.one_or_none()


def _asset_by_id(session, asset_id: str, *, for_update: bool = False) -> Asset | None:
    query = session.query(Asset).filter(Asset.id == asset_id)
    if for_update:
        query = query.with_for_update()
    return query.one_or_none()


def _positive_duration(value: object, label: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must have a positive duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"{label} must have a positive duration")
    return duration


def _input_file(value: object, label: str) -> Path:
    try:
        path = Path(value).resolve()
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
    except (OSError, TypeError, ValueError) as exc:
        raise FileNotFoundError(f"{label} file is missing or empty") from exc
    if not exists or size <= 0:
        raise FileNotFoundError(f"{label} file is missing or empty: {path}")
    return path


def _provider_file(value: object, output_dir: Path, label: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"lip sync provider returned an invalid {label} path") from exc
    if not path.is_absolute():
        path = output_dir / path
    try:
        path = path.resolve()
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
    except OSError as exc:
        raise RuntimeError(f"lip sync provider {label} cannot be read") from exc
    try:
        path.relative_to(output_dir)
    except ValueError as exc:
        raise RuntimeError(
            f"lip sync provider {label} is outside output_dir: {path}"
        ) from exc
    if not exists or size <= 0:
        raise RuntimeError(f"lip sync provider {label} is missing or empty: {path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"cannot hash media artifact: {path}") from exc
    return digest.hexdigest()


def _manifest_alias(payload: dict, keys: tuple[str, ...], expected: str, label: str) -> None:
    values = [payload[key] for key in keys if key in payload]
    if not values or any(value != expected for value in values):
        raise RuntimeError(f"lip sync provider manifest has invalid {label} provenance")


def _manifest_path(value: object, expected: Path, label: str) -> None:
    if not isinstance(value, str):
        raise RuntimeError(f"lip sync provider manifest has invalid {label} path")
    try:
        path = Path(value)
        if not path.is_absolute() or path.resolve() != expected:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"lip sync provider manifest has invalid {label} path"
        ) from exc


def _read_provider_manifest(
    path: Path,
    snapshot: _InputSnapshot,
    inputs: _ValidatedInputs,
    output_path: Path,
    output_sha256: str,
) -> tuple[dict, _ProviderProvenance]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("lip sync provider manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("lip sync provider manifest must be a JSON object")

    provider_version = payload.get("provider_version")
    if payload.get("provider") != "musetalk":
        raise RuntimeError("lip sync provider manifest has invalid provider provenance")
    if not isinstance(provider_version, str) or not provider_version.strip():
        raise RuntimeError("lip sync provider manifest has invalid provider_version provenance")
    _manifest_alias(payload, ("model_name", "model"), _MODEL_NAME, "model")
    _manifest_alias(
        payload,
        ("workflow_name", "workflow"),
        _WORKFLOW_NAME,
        "workflow",
    )

    generation_time = payload.get("generation_time")
    if isinstance(generation_time, bool) or not isinstance(generation_time, (int, float)):
        raise RuntimeError("lip sync provider manifest has invalid generation_time provenance")
    generation_time = float(generation_time)
    if not math.isfinite(generation_time) or generation_time < 0:
        raise RuntimeError("lip sync provider manifest has invalid generation_time provenance")

    seed = payload.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise RuntimeError("lip sync provider manifest has invalid seed provenance")

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("lip sync provider manifest is missing request metadata")
    if metadata.get("project_id") != snapshot.project_id:
        raise RuntimeError("lip sync provider manifest project_id does not match request")
    if metadata.get("shot_id") != snapshot.shot_id:
        raise RuntimeError("lip sync provider manifest shot_id does not match request")
    if metadata.get("input_assets") != [
        snapshot.source_asset_id,
        snapshot.audio_asset_id,
    ]:
        raise RuntimeError("lip sync provider manifest input_assets do not match request")

    target_duration = payload.get("target_duration")
    if (
        isinstance(target_duration, bool)
        or not isinstance(target_duration, Real)
        or not math.isfinite(float(target_duration))
        or abs(float(target_duration) - snapshot.shot_duration) > _TOLERANCE_EPSILON
    ):
        raise RuntimeError("lip sync provider manifest target_duration does not match request")

    source_video = payload.get("source_video")
    if not isinstance(source_video, dict):
        raise RuntimeError("lip sync provider manifest is missing source_video provenance")
    _manifest_path(source_video.get("path"), inputs.video_path, "source_video")
    if source_video.get("sha256") != inputs.video_sha256:
        raise RuntimeError("lip sync provider manifest source_video SHA256 does not match")

    source_audio = payload.get("source_audio")
    if not isinstance(source_audio, dict):
        raise RuntimeError("lip sync provider manifest is missing source_audio provenance")
    _manifest_path(source_audio.get("path"), inputs.audio_path, "source_audio")
    if source_audio.get("sha256") != inputs.audio_sha256:
        raise RuntimeError("lip sync provider manifest source_audio SHA256 does not match")

    _manifest_path(payload.get("output_path"), output_path, "output")
    if payload.get("output_sha256") != output_sha256:
        raise RuntimeError("lip sync provider manifest output SHA256 does not match")
    if "sha256" in payload and payload.get("sha256") != output_sha256:
        raise RuntimeError("lip sync provider manifest SHA256 alias does not match output")
    return payload, _ProviderProvenance(
        provider_version=provider_version.strip(),
        generation_time=generation_time,
        seed=seed,
    )


def _validate_source(snapshot: _InputSnapshot) -> _ValidatedInputs:
    video_path = _input_file(snapshot.source_path, "source video")
    try:
        video = probe_video(video_path)
    except ValueError as exc:
        raise ValueError(f"source video is not playable: {exc}") from exc
    if video.codec != "h264":
        raise ValueError(f"source video must be H.264, got {video.codec}")
    if (video.width, video.height) != (_SOURCE_WIDTH, _SOURCE_HEIGHT):
        raise ValueError(
            f"source video must be {_SOURCE_WIDTH}x{_SOURCE_HEIGHT}, "
            f"got {video.width}x{video.height}"
        )
    if abs(video.fps - _SOURCE_FPS) > 0.01:
        raise ValueError(f"source video must be 16 FPS, got {video.fps:g}")
    if video.frames is not None and video.frames <= 0:
        raise ValueError("source video contains no playable frames")

    audio_path = _input_file(snapshot.audio_path, "dialogue WAV")
    try:
        wav = probe_wav(audio_path)
    except ValueError as exc:
        raise ValueError(f"dialogue WAV is not valid PCM audio: {exc}") from exc
    if (
        abs(wav.duration - snapshot.dialogue_duration)
        > _WAV_DURATION_TOLERANCE + _TOLERANCE_EPSILON
    ):
        raise ValueError("persisted dialogue duration does not match measured WAV duration")

    required_duration = max(
        snapshot.shot_duration,
        snapshot.dialogue_duration,
        wav.duration,
    )
    source_coverage = video.duration
    if video.frames is not None:
        source_coverage = min(source_coverage, video.frames / video.fps)
    if source_coverage + (1 / _SOURCE_FPS) + _TOLERANCE_EPSILON < required_duration:
        raise ValueError(
            "source video is shorter than the Shot target or dialogue speech: "
            f"{source_coverage:.4f}s < {required_duration:.4f}s"
        )
    return _ValidatedInputs(
        video_path=video_path,
        audio_path=audio_path,
        video_sha256=_sha256(video_path),
        audio_sha256=_sha256(audio_path),
    )


def _validate_output(path: Path, target_duration: float) -> AVInfo:
    try:
        media = probe_av(path)
    except ValueError as exc:
        raise RuntimeError(f"lip sync output media is invalid: {exc}") from exc

    video = media.video
    audio = media.audio
    container_formats = {
        value.strip().lower()
        for value in media.format_name.split(",")
        if value.strip()
    }
    if not container_formats.intersection(_MP4_MOV_FORMATS):
        raise RuntimeError(
            f"lip sync output must use an MP4/MOV-family container, got {media.format_name}"
        )
    if video.codec != "h264":
        raise RuntimeError(f"lip sync output must be H.264, got {video.codec}")
    if video.pixel_format != "yuv420p":
        raise RuntimeError(f"lip sync output must use yuv420p, got {video.pixel_format}")
    if (video.width, video.height) != (_OUTPUT_WIDTH, _OUTPUT_HEIGHT):
        raise RuntimeError(
            f"lip sync output must be {_OUTPUT_WIDTH}x{_OUTPUT_HEIGHT}, "
            f"got {video.width}x{video.height}"
        )
    if abs(video.fps - _OUTPUT_FPS) > 0.01:
        raise RuntimeError(f"lip sync output must be 25 FPS, got {video.fps:g}")
    if audio.codec != "aac":
        raise RuntimeError(f"lip sync output audio must be AAC, got {audio.codec}")
    if audio.sample_rate <= 0 or audio.channels <= 0:
        raise RuntimeError("lip sync output audio has invalid stream metadata")
    if not all(
        math.isfinite(duration) and duration > 0
        for duration in (media.duration, video.duration, audio.duration)
    ):
        raise RuntimeError("lip sync output contains a non-positive duration")
    if (
        min(video.duration, audio.duration)
        + _OUTPUT_DURATION_TOLERANCE
        + _TOLERANCE_EPSILON
        < target_duration
    ):
        raise RuntimeError(
            "lip sync output is shorter than the Shot target: "
            f"video={video.duration:.4f}s audio={audio.duration:.4f}s "
            f"target={target_duration:.4f}s"
        )
    if (
        abs(video.duration - audio.duration)
        > _OUTPUT_AV_TOLERANCE + _TOLERANCE_EPSILON
    ):
        raise RuntimeError(
            "lip sync output A/V synchronization exceeds 0.08s: "
            f"video={video.duration:.4f}s audio={audio.duration:.4f}s"
        )
    return media


def _snapshot_inputs(database_url: str, project_id: str, shot_id: str) -> _InputSnapshot | None:
    with session_scope(database_url) as session:
        shot = _owned_shot(session, project_id, shot_id)
        if shot is None:
            raise ValueError("shot does not belong to project")
        if not shot.requires_lip_sync or not shot.speaker_visible:
            return None
        if not shot.video_asset_id:
            raise ValueError("lip sync requires a source video asset")

        source = session.get(Asset, shot.video_asset_id)
        if source is None or source.project_id != project_id or source.kind != "VIDEO":
            raise ValueError("source video asset must be a project-owned VIDEO asset")

        dialogues = (
            session.query(Dialogue)
            .filter_by(shot_id=shot_id)
            .order_by(Dialogue.order)
            .all()
        )
        if len(dialogues) != 1:
            raise ValueError("PHASE 8 lip sync requires exactly one dialogue")
        dialogue = dialogues[0]
        if not dialogue.audio_asset_id:
            raise ValueError("lip sync dialogue requires an audio asset")
        dialogue_duration = _positive_duration(dialogue.duration, "dialogue")

        audio = session.get(Asset, dialogue.audio_asset_id)
        if audio is None or audio.project_id != project_id or audio.kind != "AUDIO":
            raise ValueError("dialogue audio asset must be a project-owned AUDIO asset")

        return _InputSnapshot(
            project_id=project_id,
            shot_id=shot.id,
            scene_id=shot.scene_id,
            requires_lip_sync=bool(shot.requires_lip_sync),
            speaker_visible=bool(shot.speaker_visible),
            source_asset_id=source.id,
            source_path=source.path,
            dialogue_id=dialogue.id,
            audio_asset_id=audio.id,
            audio_path=audio.path,
            dialogue_duration=dialogue_duration,
            shot_duration=_positive_duration(shot.duration, "Shot"),
            shot_status=shot.status,
            lipsync_asset_id=shot.lipsync_asset_id,
        )


def _assert_snapshot_unchanged(
    session,
    snapshot: _InputSnapshot,
    *,
    lock_rows: bool = False,
) -> Shot:
    shot = _owned_shot(
        session,
        snapshot.project_id,
        snapshot.shot_id,
        for_update=lock_rows,
    )
    if shot is None:
        raise RuntimeError("shot changed during lip sync generation")
    if (
        shot.scene_id != snapshot.scene_id
        or bool(shot.requires_lip_sync) != snapshot.requires_lip_sync
        or bool(shot.speaker_visible) != snapshot.speaker_visible
        or not shot.requires_lip_sync
        or not shot.speaker_visible
        or shot.video_asset_id != snapshot.source_asset_id
        or shot.duration != snapshot.shot_duration
        or shot.status != snapshot.shot_status
        or shot.lipsync_asset_id != snapshot.lipsync_asset_id
    ):
        raise RuntimeError("shot changed during lip sync generation")

    source = _asset_by_id(
        session,
        snapshot.source_asset_id,
        for_update=lock_rows,
    )
    if (
        source is None
        or source.project_id != snapshot.project_id
        or source.kind != "VIDEO"
        or source.path != snapshot.source_path
    ):
        raise RuntimeError("source video asset changed during lip sync generation")

    dialogue_query = (
        session.query(Dialogue)
        .filter_by(shot_id=snapshot.shot_id)
        .order_by(Dialogue.order)
    )
    if lock_rows:
        dialogue_query = dialogue_query.with_for_update()
    dialogues = dialogue_query.all()
    if len(dialogues) != 1:
        raise RuntimeError("dialogue changed during lip sync generation")
    dialogue = dialogues[0]
    if (
        dialogue.id != snapshot.dialogue_id
        or dialogue.audio_asset_id != snapshot.audio_asset_id
        or dialogue.duration != snapshot.dialogue_duration
    ):
        raise RuntimeError("dialogue changed during lip sync generation")

    audio = _asset_by_id(
        session,
        snapshot.audio_asset_id,
        for_update=lock_rows,
    )
    if (
        audio is None
        or audio.project_id != snapshot.project_id
        or audio.kind != "AUDIO"
        or audio.path != snapshot.audio_path
    ):
        raise RuntimeError("dialogue audio asset changed during lip sync generation")
    return shot


async def generate_shot_lipsync(
    database_url: str,
    project_id: str,
    shot_id: str,
    provider,
    output_dir: Path,
) -> Asset | None:
    """Generate, validate, and atomically register a MuseTalk lip-sync asset."""
    snapshot = _snapshot_inputs(database_url, project_id, shot_id)
    if snapshot is None:
        return None

    inputs = _validate_source(snapshot)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "project_id": project_id,
        "shot_id": shot_id,
        "input_assets": [snapshot.source_asset_id, snapshot.audio_asset_id],
        "target_duration": snapshot.shot_duration,
    }

    result = await provider.generate(
        inputs.video_path,
        inputs.audio_path,
        output_dir,
        metadata,
    )
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        raise RuntimeError("lip sync provider did not return output and manifest paths")
    output_path = _provider_file(result[0], output_dir, "output")
    manifest_path = _provider_file(result[1], output_dir, "manifest")
    output_sha256 = _sha256(output_path)
    _, provenance = _read_provider_manifest(
        manifest_path,
        snapshot,
        inputs,
        output_path,
        output_sha256,
    )
    media = _validate_output(output_path, snapshot.shot_duration)

    asset_metadata = {
        "manifest_path": str(manifest_path),
        "provider": "musetalk",
        "provider_version": provenance.provider_version,
        "model_name": _MODEL_NAME,
        "workflow_name": _WORKFLOW_NAME,
        "generation_time": provenance.generation_time,
        "source_video_asset_id": snapshot.source_asset_id,
        "source_video_sha256": inputs.video_sha256,
        "dialogue_id": snapshot.dialogue_id,
        "audio_asset_id": snapshot.audio_asset_id,
        "source_audio_sha256": inputs.audio_sha256,
        "input_assets": [snapshot.source_asset_id, snapshot.audio_asset_id],
        "target_duration": snapshot.shot_duration,
        "output_sha256": output_sha256,
        "format_name": media.format_name,
        "duration": media.duration,
        "video_duration": media.video.duration,
        "audio_duration": media.audio.duration,
        "video_codec": media.video.codec,
        "pixel_format": media.video.pixel_format,
        "width": media.video.width,
        "height": media.video.height,
        "fps": media.video.fps,
        "audio_codec": media.audio.codec,
        "sample_rate": media.audio.sample_rate,
        "channels": media.audio.channels,
    }

    with session_scope(database_url) as session:
        is_sqlite = session.get_bind().dialect.name == "sqlite"
        if is_sqlite:
            session.execute(text("BEGIN IMMEDIATE"))
        shot = _assert_snapshot_unchanged(
            session,
            snapshot,
            lock_rows=not is_sqlite,
        )
        asset = Asset(
            project_id=project_id,
            kind="LIPSYNC",
            path=str(output_path),
            mime_type="video/mp4",
            metadata_json=asset_metadata,
        )
        session.add(asset)
        session.flush()
        session.add(GenerationManifest(
            asset_id=asset.id,
            provider="musetalk",
            provider_version=provenance.provider_version,
            model_name=_MODEL_NAME,
            prompt=None,
            negative_prompt=None,
            seed=provenance.seed,
            workflow_name=_WORKFLOW_NAME,
            workflow_hash=None,
            binding_version="1",
            generation_time=provenance.generation_time,
            input_assets=[snapshot.source_asset_id, snapshot.audio_asset_id],
            output_asset=asset.id,
        ))
        shot.lipsync_asset_id = asset.id
        shot.status = "LIPSYNC_GENERATED"
        session.flush()
        return asset
