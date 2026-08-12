"""Build an immutable, deterministic snapshot of a Project render timeline."""

from __future__ import annotations

import hashlib
import json
import os
import wave
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_engine, session_scope
from app.models import Asset, Dialogue, Project, Scene, Shot
from app.services.audio_probe import probe_wav
from app.services.video_probe import probe_video


_WAV_VALIDATION_BLOCK_BYTES = 64 * 1024


@dataclass(frozen=True)
class RenderProfile:
    width: int = 640
    height: int = 368
    fps: int = 25
    sample_rate: int = 48_000
    channels: int = 2


@dataclass(frozen=True)
class TimelineDialogue:
    dialogue_id: str
    order: int
    text: str
    persisted_duration: float
    persisted_start_time: float | None
    persisted_end_time: float | None
    audio_asset_id: str
    audio_asset_project_id: str
    audio_asset_kind: str
    audio_raw_path: str
    audio_path: Path
    audio_size: int
    audio_sha256: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class TimelineShot:
    shot_id: str
    scene_id: str
    character_id: str | None
    order: int
    persisted_duration: float
    status: str
    requires_lip_sync: bool
    speaker_visible: bool
    storyboard_asset_id: str | None
    source_video_asset_id: str | None
    source_lipsync_asset_id: str | None
    video_asset_id: str
    video_asset_project_id: str
    video_asset_kind: str
    video_raw_path: str
    video_path: Path
    video_size: int
    video_sha256: str
    start_frame: int
    frame_count: int
    dialogues: tuple[TimelineDialogue, ...]


@dataclass(frozen=True)
class TimelineSceneSnapshot:
    scene_id: str
    order: int
    shots: tuple[TimelineShot, ...]


@dataclass(frozen=True)
class RenderTimeline:
    project_id: str
    subtitle_asset_id: str | None
    final_video_asset_id: str | None
    profile: RenderProfile
    scenes: tuple[TimelineSceneSnapshot, ...]
    shots: tuple[TimelineShot, ...]
    total_frames: int
    canonical_json: str
    workflow_hash: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frames(duration: float, fps: int) -> int:
    return int((Decimal(str(duration)) * fps).to_integral_value(rounding=ROUND_CEILING))


def _milliseconds(seconds: Decimal) -> int:
    return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _reject_duplicate_orders(records, label: str, parent_key) -> None:
    seen: set[tuple[object, int]] = set()
    for record in records:
        if isinstance(record.order, bool) or not isinstance(record.order, int) or record.order < 0:
            raise ValueError(f"invalid {label} order")
        key = (parent_key(record), record.order)
        if key in seen:
            raise ValueError(f"duplicate {label} order")
        seen.add(key)


def _canonical_value(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    return value


def _positive_duration(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and positive")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be finite and positive") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _optional_time(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be finite and nonnegative") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return number


def _required_asset(
    asset_rows: dict[str, tuple[str, str, str, str]],
    asset_id: str | None,
    project_id: str,
    expected_kind: str,
) -> tuple[str, str, str, str]:
    if asset_id is None or asset_id not in asset_rows:
        raise ValueError(f"Shot requires a project-owned {expected_kind} Asset")
    row = asset_rows[asset_id]
    if row[1] != project_id:
        raise ValueError(f"Asset must be project-owned by {project_id}")
    if row[2] != expected_kind:
        raise ValueError(f"Asset {asset_id} must have kind {expected_kind}")
    return row


def _readable_nonempty_file(raw_path: str, label: str) -> tuple[Path, int]:
    try:
        path = Path(raw_path).resolve(strict=True)
        size = path.stat().st_size
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} file is missing or unreadable: {raw_path}") from exc
    if not path.is_file() or size <= 0:
        raise ValueError(f"{label} media file is missing or empty: {path}")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{label} file is unreadable: {path}")
    return path, size


def _validate_wav_payload(
    path: Path,
    *,
    frames: int,
    channels: int,
    sample_width: int,
) -> None:
    frame_size = channels * sample_width
    if frame_size > _WAV_VALIDATION_BLOCK_BYTES:
        raise ValueError(
            f"WAV frame size exceeds validation byte budget: {path}"
        )
    frames_per_chunk = _WAV_VALIDATION_BLOCK_BYTES // frame_size
    remaining_frames = frames
    try:
        with wave.open(str(path), "rb") as source:
            while remaining_frames:
                requested_frames = min(remaining_frames, frames_per_chunk)
                payload = source.readframes(requested_frames)
                if len(payload) != requested_frames * frame_size:
                    raise ValueError(f"WAV has a truncated PCM payload: {path}")
                remaining_frames -= requested_frames
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError(f"WAV has a truncated PCM payload: {path}") from exc


def build_render_timeline(database_url: str, project_id: str) -> RenderTimeline:
    """Snapshot one Project without holding a transaction during media probes."""

    with session_scope(database_url) as session:
        project = session.execute(
            select(Project).where(Project.id == project_id)
        ).scalar_one_or_none()
        if project is None:
            raise ValueError(f"project not found: {project_id}")

        scenes = list(
            session.execute(
                select(Scene)
                .where(Scene.project_id == project_id)
                .order_by(Scene.order)
            ).scalars()
        )
        shots = list(
            session.execute(
                select(Shot)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.project_id == project_id)
                .order_by(Scene.order, Shot.order)
            ).scalars()
        )
        dialogues = list(
            session.execute(
                select(Dialogue)
                .join(Shot, Dialogue.shot_id == Shot.id)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.project_id == project_id)
                .order_by(Scene.order, Shot.order, Dialogue.order)
            ).scalars()
        )

        _reject_duplicate_orders(scenes, "scene", lambda _scene: project_id)
        _reject_duplicate_orders(shots, "shot", lambda shot: shot.scene_id)
        _reject_duplicate_orders(dialogues, "dialogue", lambda dialogue: dialogue.shot_id)
        if not shots:
            raise ValueError("Project timeline must contain at least one Shot")

        selected_asset_ids = {
            shot.lipsync_asset_id
            if shot.requires_lip_sync and shot.speaker_visible
            else shot.video_asset_id
            for shot in shots
        }
        selected_asset_ids.update(dialogue.audio_asset_id for dialogue in dialogues)
        selected_asset_ids.discard(None)
        assets = {
            asset.id: asset
            for asset in session.execute(
                select(Asset)
                .where(Asset.id.in_(sorted(selected_asset_ids)))
                .order_by(Asset.id)
            ).scalars()
        }

        project_pointers = (
            getattr(project, "subtitle_asset_id", None),
            getattr(project, "final_video_asset_id", None),
        )
        scene_rows = [(scene.id, scene.order) for scene in scenes]
        shot_rows = [
            (
                shot.id,
                shot.scene_id,
                shot.character_id,
                shot.order,
                shot.duration,
                shot.status,
                bool(shot.requires_lip_sync),
                bool(shot.speaker_visible),
                shot.storyboard_asset_id,
                shot.video_asset_id,
                shot.lipsync_asset_id,
            )
            for shot in shots
        ]
        dialogue_rows = [
            (
                dialogue.id,
                dialogue.shot_id,
                dialogue.order,
                dialogue.text,
                dialogue.duration,
                dialogue.start_time,
                dialogue.end_time,
                dialogue.audio_asset_id,
            )
            for dialogue in dialogues
        ]
        asset_rows = {
            asset_id: (asset.id, asset.project_id, asset.kind, asset.path)
            for asset_id, asset in assets.items()
        }

    profile = RenderProfile()
    dialogues_by_shot: dict[str, list[tuple]] = {}
    for dialogue_row in dialogue_rows:
        dialogues_by_shot.setdefault(dialogue_row[1], []).append(dialogue_row)

    timeline_shots: list[TimelineShot] = []
    current_frame = 0
    for shot_row in shot_rows:
        (
            shot_id,
            scene_id,
            character_id,
            order,
            duration,
            status,
            requires_lip_sync,
            speaker_visible,
            storyboard_asset_id,
            source_video_asset_id,
            source_lipsync_asset_id,
        ) = shot_row
        shot_duration = _positive_duration(duration, "Shot duration")
        shot_dialogue_rows = dialogues_by_shot.get(shot_id, [])
        eligible = requires_lip_sync and speaker_visible
        if eligible and len(shot_dialogue_rows) != 1:
            raise ValueError("eligible lip-sync Shot requires exactly one Dialogue")
        selected_id = (
            source_lipsync_asset_id
            if eligible
            else source_video_asset_id
        )
        expected_video_kind = "LIPSYNC" if eligible else "VIDEO"
        asset_id, asset_project_id, asset_kind, asset_raw_path = _required_asset(
            asset_rows, selected_id, project_id, expected_video_kind
        )
        video_path, video_size = _readable_nonempty_file(asset_raw_path, "video")
        video_info = probe_video(video_path)
        source_duration = Decimal(str(video_info.duration))
        source_fps = Decimal(str(video_info.fps))
        if (
            not source_duration.is_finite()
            or source_duration <= 0
            or not source_fps.is_finite()
            or source_fps <= 0
        ):
            raise ValueError(f"invalid video probe metadata: {video_path}")
        source_frame = Decimal(1) / source_fps
        if video_info.frames is not None:
            if video_info.frames <= 0:
                raise ValueError(f"invalid video probe metadata: {video_path}")
            source_duration = min(
                source_duration,
                Decimal(video_info.frames) / source_fps,
            )
        if source_duration + source_frame < shot_duration:
            raise ValueError(
                f"video source does not cover Shot editorial duration: {shot_id}"
            )
        frame_count = _frames(duration, profile.fps)

        timeline_dialogues: list[TimelineDialogue] = []
        packed_start = Decimal(0)
        previous_end = Decimal(0)
        shot_start = Decimal(current_frame) / profile.fps
        for dialogue_row in shot_dialogue_rows:
            (
                dialogue_id,
                _dialogue_shot_id,
                dialogue_order,
                text,
                persisted_duration,
                persisted_start,
                persisted_end,
                audio_asset_id,
            ) = dialogue_row
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Dialogue text must be nonempty: {dialogue_id}")
            dialogue_duration = _positive_duration(
                persisted_duration, "Dialogue duration"
            )
            (
                _asset_id,
                audio_project_id,
                audio_kind,
                audio_raw_path,
            ) = _required_asset(asset_rows, audio_asset_id, project_id, "AUDIO")
            audio_path, audio_size = _readable_nonempty_file(audio_raw_path, "WAV")
            wav = probe_wav(audio_path)
            _validate_wav_payload(
                audio_path,
                frames=wav.frames,
                channels=wav.channels,
                sample_width=wav.sample_width,
            )
            measured_duration = Decimal(str(wav.duration))
            if abs(measured_duration - dialogue_duration) > Decimal("0.020"):
                raise ValueError(
                    f"persisted Dialogue duration disagrees with WAV duration: {dialogue_id}"
                )
            if (persisted_start is None) != (persisted_end is None):
                raise ValueError("Dialogue requires both start_time and end_time or neither")
            if persisted_start is None and persisted_end is None:
                local_start = packed_start
                local_end = local_start + measured_duration
            else:
                local_start = _optional_time(persisted_start, "Dialogue start_time")
                local_end = _optional_time(persisted_end, "Dialogue end_time")
                if local_end <= local_start:
                    raise ValueError("Dialogue end_time must be after start_time")
                if abs((local_end - local_start) - measured_duration) > Decimal("0.020"):
                    raise ValueError(
                        f"Dialogue time span disagrees with WAV duration: {dialogue_id}"
                    )
            if local_start < previous_end:
                raise ValueError(f"Dialogue times overlap within Shot: {shot_id}")
            if local_end > shot_duration:
                raise ValueError(f"Dialogue exceeds Shot duration: {dialogue_id}")
            if eligible and local_start != 0:
                raise ValueError("eligible lip-sync Shot requires its Dialogue starting at zero")
            packed_start = local_end
            previous_end = local_end
            timeline_dialogues.append(
                TimelineDialogue(
                    dialogue_id=dialogue_id,
                    order=dialogue_order,
                    text=text,
                    persisted_duration=persisted_duration,
                    persisted_start_time=persisted_start,
                    persisted_end_time=persisted_end,
                    audio_asset_id=audio_asset_id,
                    audio_asset_project_id=audio_project_id,
                    audio_asset_kind=audio_kind,
                    audio_raw_path=audio_raw_path,
                    audio_path=audio_path,
                    audio_size=audio_size,
                    audio_sha256=_sha256(audio_path),
                    start_ms=_milliseconds(shot_start + local_start),
                    end_ms=_milliseconds(shot_start + local_end),
                )
            )
        timeline_shots.append(
            TimelineShot(
                shot_id=shot_id,
                scene_id=scene_id,
                character_id=character_id,
                order=order,
                persisted_duration=duration,
                status=status,
                requires_lip_sync=requires_lip_sync,
                speaker_visible=speaker_visible,
                storyboard_asset_id=storyboard_asset_id,
                source_video_asset_id=source_video_asset_id,
                source_lipsync_asset_id=source_lipsync_asset_id,
                video_asset_id=asset_id,
                video_asset_project_id=asset_project_id,
                video_asset_kind=asset_kind,
                video_raw_path=asset_raw_path,
                video_path=video_path,
                video_size=video_size,
                video_sha256=_sha256(video_path),
                start_frame=current_frame,
                frame_count=frame_count,
                dialogues=tuple(timeline_dialogues),
            )
        )
        current_frame += frame_count

    shots_by_scene: dict[str, list[TimelineShot]] = {}
    for shot in timeline_shots:
        shots_by_scene.setdefault(shot.scene_id, []).append(shot)
    timeline_scenes = tuple(
        TimelineSceneSnapshot(
            scene_id=scene_id,
            order=order,
            shots=tuple(shots_by_scene.get(scene_id, [])),
        )
        for scene_id, order in scene_rows
    )

    canonical_payload = {
        "project_id": project_id,
        "subtitle_asset_id": project_pointers[0],
        "final_video_asset_id": project_pointers[1],
        "profile": asdict(profile),
        "scenes": _canonical_value([asdict(scene) for scene in timeline_scenes]),
        "total_frames": current_frame,
    }
    canonical_json = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RenderTimeline(
        project_id=project_id,
        subtitle_asset_id=project_pointers[0],
        final_video_asset_id=project_pointers[1],
        profile=profile,
        scenes=timeline_scenes,
        shots=tuple(timeline_shots),
        total_frames=current_frame,
        canonical_json=canonical_json,
        workflow_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


_STALE_MESSAGE = "project timeline changed during Phase 9 render"


def _timeline_database_state(session, project_id: str, *, lock_rows: bool) -> tuple:
    project_query = select(Project).where(Project.id == project_id)
    scene_query = (
        select(Scene)
        .where(Scene.project_id == project_id)
        .order_by(Scene.order)
    )
    shot_query = (
        select(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .where(Scene.project_id == project_id)
        .order_by(Scene.order, Shot.order)
    )
    dialogue_query = (
        select(Dialogue)
        .join(Shot, Dialogue.shot_id == Shot.id)
        .join(Scene, Shot.scene_id == Scene.id)
        .where(Scene.project_id == project_id)
        .order_by(Scene.order, Shot.order, Dialogue.order)
    )
    if lock_rows:
        project_query = project_query.with_for_update()
        scene_query = scene_query.with_for_update()
        shot_query = shot_query.with_for_update()
        dialogue_query = dialogue_query.with_for_update()

    project = session.execute(project_query).scalar_one_or_none()
    if project is None:
        raise RuntimeError(_STALE_MESSAGE)
    scenes = list(session.execute(scene_query).scalars())
    shots = list(session.execute(shot_query).scalars())
    dialogues = list(session.execute(dialogue_query).scalars())

    selected_ids = {
        shot.lipsync_asset_id
        if shot.requires_lip_sync and shot.speaker_visible
        else shot.video_asset_id
        for shot in shots
    }
    selected_ids.update(dialogue.audio_asset_id for dialogue in dialogues)
    selected_ids.discard(None)
    asset_query = (
        select(Asset)
        .where(Asset.id.in_(sorted(selected_ids)))
        .order_by(Asset.id)
    )
    if lock_rows:
        asset_query = asset_query.with_for_update()
    assets = list(session.execute(asset_query).scalars())

    return (
        (project.subtitle_asset_id, project.final_video_asset_id),
        tuple((scene.id, scene.order) for scene in scenes),
        tuple(
            (
                shot.id,
                shot.scene_id,
                shot.character_id,
                shot.order,
                shot.duration,
                shot.status,
                bool(shot.requires_lip_sync),
                bool(shot.speaker_visible),
                shot.storyboard_asset_id,
                shot.video_asset_id,
                shot.lipsync_asset_id,
            )
            for shot in shots
        ),
        tuple(
            (
                dialogue.id,
                dialogue.shot_id,
                dialogue.order,
                dialogue.text,
                dialogue.duration,
                dialogue.start_time,
                dialogue.end_time,
                dialogue.audio_asset_id,
            )
            for dialogue in dialogues
        ),
        tuple((asset.id, asset.project_id, asset.kind, asset.path) for asset in assets),
    )


def _snapshot_database_state(timeline: RenderTimeline) -> tuple:
    dialogues = tuple(
        (
            dialogue.dialogue_id,
            shot.shot_id,
            dialogue.order,
            dialogue.text,
            dialogue.persisted_duration,
            dialogue.persisted_start_time,
            dialogue.persisted_end_time,
            dialogue.audio_asset_id,
        )
        for shot in timeline.shots
        for dialogue in shot.dialogues
    )
    assets: dict[str, tuple[str, str, str, str]] = {}
    for shot in timeline.shots:
        assets[shot.video_asset_id] = (
            shot.video_asset_id,
            shot.video_asset_project_id,
            shot.video_asset_kind,
            shot.video_raw_path,
        )
        for dialogue in shot.dialogues:
            assets[dialogue.audio_asset_id] = (
                dialogue.audio_asset_id,
                dialogue.audio_asset_project_id,
                dialogue.audio_asset_kind,
                dialogue.audio_raw_path,
            )
    return (
        (timeline.subtitle_asset_id, timeline.final_video_asset_id),
        tuple((scene.scene_id, scene.order) for scene in timeline.scenes),
        tuple(
            (
                shot.shot_id,
                shot.scene_id,
                shot.character_id,
                shot.order,
                shot.persisted_duration,
                shot.status,
                shot.requires_lip_sync,
                shot.speaker_visible,
                shot.storyboard_asset_id,
                shot.source_video_asset_id,
                shot.source_lipsync_asset_id,
            )
            for shot in timeline.shots
        ),
        dialogues,
        tuple(assets[asset_id] for asset_id in sorted(assets)),
    )


def _snapshot_file_state(timeline: RenderTimeline) -> tuple:
    files: dict[str, tuple[str, int, str]] = {}
    for shot in timeline.shots:
        files[shot.video_asset_id] = (
            str(shot.video_path),
            shot.video_size,
            shot.video_sha256,
        )
        for dialogue in shot.dialogues:
            files[dialogue.audio_asset_id] = (
                str(dialogue.audio_path),
                dialogue.audio_size,
                dialogue.audio_sha256,
            )
    return tuple((asset_id, *files[asset_id]) for asset_id in sorted(files))


def _current_file_state(database_state: tuple) -> tuple:
    files = []
    for asset_id, _owner, _kind, raw_path in database_state[4]:
        try:
            path, size = _readable_nonempty_file(raw_path, "input")
            files.append((asset_id, str(path), size, _sha256(path)))
        except (OSError, ValueError):
            raise RuntimeError(_STALE_MESSAGE) from None
    return tuple(files)


def assert_render_timeline_unchanged(
    database_or_session: str | Session, timeline: RenderTimeline
) -> None:
    """Reject changes, optionally inside a caller-owned publication transaction."""

    owns_session = not isinstance(database_or_session, Session)
    if owns_session:
        engine = get_engine(database_or_session)
        session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    else:
        session = database_or_session
        engine = session.get_bind()
    try:
        if engine.dialect.name == "sqlite":
            if not owns_session and session.in_transaction():
                raise RuntimeError(
                    "SQLite stale check requires a clean Session so it can start "
                    "BEGIN IMMEDIATE"
                )
            session.execute(text("BEGIN IMMEDIATE"))
            lock_rows = False
        else:
            lock_rows = True
        current = _timeline_database_state(
            session, timeline.project_id, lock_rows=lock_rows
        )
        if current != _snapshot_database_state(timeline):
            raise RuntimeError(_STALE_MESSAGE)
        if _current_file_state(current) != _snapshot_file_state(timeline):
            raise RuntimeError(_STALE_MESSAGE)
        if owns_session:
            session.commit()
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
