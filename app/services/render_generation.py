"""Persist and publish one immutable Phase 9 project render."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.db.session import get_engine, session_scope
from app.models import Asset, GenerationManifest, Project
from app.providers.ffmpeg_render_provider import FFmpegRenderProvider, FFmpegRenderResult
from app.services.render_timeline import (
    RenderTimeline,
    assert_render_timeline_unchanged,
    build_render_timeline,
)
from app.services.subtitle_generation import (
    cues_from_timeline,
    serialize_srt,
    write_subtitle_atomic,
)


@dataclass(frozen=True)
class RenderProjectResult:
    subtitle_asset: Asset
    final_asset: Asset
    subtitle_path: Path
    published_path: Path
    provider_result: FFmpegRenderResult
    alias_status: str
    alias_error: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _try_lock(file) -> bool:
    file.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                exc, "winerror", None
            ) in {33, 36}:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock(file) -> None:
    file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _project_lock(path: Path, timeout_seconds: float = 30.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if os.fstat(file.fileno()).st_size == 0:
            file.write(b"\0")
            file.flush()
        deadline = time.monotonic() + timeout_seconds
        while not _try_lock(file):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for Phase 9 project lock: {path}")
            time.sleep(0.025)
        try:
            yield
        finally:
            _unlock(file)


def _atomic_alias(source: Path, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if _sha256(temporary) != expected_sha256:
            raise RuntimeError("canonical alias staging hash mismatch")
        os.replace(temporary, destination)
        temporary = None
        if _sha256(destination) != expected_sha256:
            raise RuntimeError("canonical alias hash mismatch")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _provider_manifest(result: FFmpegRenderResult, timeline: RenderTimeline, srt: bytes) -> dict:
    try:
        payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("FFmpeg provider manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("FFmpeg provider manifest is invalid")
    expected = {
        "provider": "ffmpeg",
        "workflow": "final_render_v1",
        "project_id": timeline.project_id,
        "workflow_hash": timeline.workflow_hash,
        "output_path": str(result.output_path.resolve()),
        "output_sha256": result.output_sha256,
        "srt_sha256": hashlib.sha256(srt).hexdigest(),
        "cue_count": sum(len(shot.dialogues) for shot in timeline.shots),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("FFmpeg provider manifest does not match render result")
    return payload


def _reconcile_current_alias(database_url: str, project_id: str, alias: Path) -> None:
    with session_scope(database_url) as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        if project.final_video_asset_id is None:
            return
        asset = session.get(Asset, project.final_video_asset_id)
        if (
            asset is None
            or asset.project_id != project_id
            or asset.kind != "FINAL_VIDEO"
        ):
            raise RuntimeError("current authoritative final video Asset is invalid")
        immutable = Path(asset.path).resolve()
        metadata = asset.metadata_json
    try:
        if not immutable.is_file() or immutable.stat().st_size <= 0:
            raise OSError
        digest = _sha256(immutable)
    except OSError as exc:
        raise RuntimeError("current authoritative final video bytes are unavailable") from exc
    if not isinstance(metadata, dict) or metadata.get("sha256") != digest:
        raise RuntimeError("current authoritative Asset metadata hash mismatch")
    if not alias.is_file() or _sha256(alias) != digest:
        _atomic_alias(immutable, alias, digest)


def render_project(
    database_url: str,
    project_id: str,
    provider: FFmpegRenderProvider,
    storage_root: str | os.PathLike[str],
) -> RenderProjectResult:
    timeline = build_render_timeline(database_url, project_id)
    cues = cues_from_timeline(timeline)
    srt = serialize_srt(cues)
    generation_id = str(uuid.uuid4())
    project_root = Path(storage_root).resolve() / "projects" / project_id
    subtitle_path = project_root / "subtitles" / f"{generation_id}.srt"
    output_path = project_root / "render" / f"{generation_id}.mp4"
    manifest_path = project_root / "manifests" / f"{generation_id}.json"
    alias_path = project_root / "output" / "final.mp4"
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    write_subtitle_atomic(subtitle_path, srt)
    provider_result = provider.render(timeline, srt, output_path, manifest_path)
    try:
        persisted_srt = subtitle_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("persisted subtitle bytes mismatch") from exc
    if persisted_srt != srt:
        raise RuntimeError("persisted subtitle bytes mismatch")
    if provider_result.output_path.resolve() != output_path.resolve():
        raise RuntimeError("FFmpeg provider returned an unexpected output path")
    if provider_result.manifest_path.resolve() != manifest_path.resolve():
        raise RuntimeError("FFmpeg provider returned an unexpected manifest path")
    if not output_path.is_file() or _sha256(output_path) != provider_result.output_sha256:
        raise RuntimeError("FFmpeg provider output hash mismatch")
    provider_payload = _provider_manifest(provider_result, timeline, srt)

    subtitle_id = str(uuid.uuid4())
    final_id = str(uuid.uuid4())
    subtitle_asset = Asset(
        id=subtitle_id,
        project_id=project_id,
        kind="SUBTITLE",
        path=str(subtitle_path),
        mime_type="application/x-subrip",
        metadata_json={
            "provider": "local",
            "workflow": "subtitle_srt_v1",
            "sha256": hashlib.sha256(srt).hexdigest(),
            "cue_count": len(cues),
            "workflow_hash": timeline.workflow_hash,
        },
    )
    final_asset = Asset(
        id=final_id,
        project_id=project_id,
        kind="FINAL_VIDEO",
        path=str(output_path),
        mime_type="video/mp4",
        metadata_json={
            "provider": "ffmpeg",
            "workflow": "final_render_v1",
            "sha256": provider_result.output_sha256,
            "workflow_hash": timeline.workflow_hash,
            "immutable_path": str(output_path),
            "published_path": str(alias_path),
            "profile": {
                "width": timeline.profile.width,
                "height": timeline.profile.height,
                "fps": timeline.profile.fps,
                "sample_rate": timeline.profile.sample_rate,
                "channels": timeline.profile.channels,
            },
            "cue_count": len(cues),
            "total_frames": timeline.total_frames,
            "duration_seconds": timeline.total_frames / timeline.profile.fps,
            "ffmpeg": {
                "executable": str(provider_result.identity.executable),
                "version": provider_result.identity.version,
                "configuration": provider_result.identity.configuration,
            },
            "font": {
                "path": str(provider_result.identity.font_path),
                "size": provider_result.identity.font_size,
                "sha256": provider_result.identity.font_sha256,
            },
            "timeline": [
                {
                    "role": shot.video_asset_kind,
                    "shot_id": shot.shot_id,
                    "asset_id": shot.video_asset_id,
                    "path": str(shot.video_path),
                    "sha256": shot.video_sha256,
                    "start_frame": shot.start_frame,
                    "frame_count": shot.frame_count,
                    "audio": [
                        {
                            "role": "AUDIO",
                            "asset_id": dialogue.audio_asset_id,
                            "path": str(dialogue.audio_path),
                            "sha256": dialogue.audio_sha256,
                            "start_ms": dialogue.start_ms,
                            "end_ms": dialogue.end_ms,
                        }
                        for dialogue in shot.dialogues
                    ],
                }
                for shot in timeline.shots
            ],
            "provider_manifest": provider_payload,
        },
    )
    subtitle_inputs = [
        dialogue.audio_asset_id
        for shot in timeline.shots
        for dialogue in shot.dialogues
    ]
    final_inputs = [
        asset_id
        for shot in timeline.shots
        for asset_id in (
            shot.video_asset_id,
            *(dialogue.audio_asset_id for dialogue in shot.dialogues),
        )
    ]
    if cues:
        final_inputs.append(subtitle_id)

    lock_path = project_root / ".phase9-publish.lock"
    with _project_lock(lock_path):
        _reconcile_current_alias(database_url, project_id, alias_path)
        session = sessionmaker(
            bind=get_engine(database_url), expire_on_commit=False, future=True
        )()
        try:
            assert_render_timeline_unchanged(session, timeline)
            project = session.get(Project, project_id)
            if project is None:
                raise RuntimeError("project timeline changed during Phase 9 render")
            session.add_all([subtitle_asset, final_asset])
            session.flush()
            session.add_all(
                [
                    GenerationManifest(
                        asset_id=subtitle_id,
                        provider="local",
                        provider_version="1",
                        workflow_name="subtitle_srt_v1",
                        workflow_hash=timeline.workflow_hash,
                        generation_time=0.0,
                        input_assets=subtitle_inputs,
                        output_asset=subtitle_id,
                    ),
                    GenerationManifest(
                        asset_id=final_id,
                        provider="ffmpeg",
                        provider_version=provider_result.identity.version,
                        workflow_name="final_render_v1",
                        workflow_hash=timeline.workflow_hash,
                        generation_time=provider_result.generation_time,
                        input_assets=final_inputs,
                        output_asset=final_id,
                    ),
                ]
            )
            project.subtitle_asset_id = subtitle_id
            project.final_video_asset_id = final_id
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        try:
            _atomic_alias(output_path, alias_path, provider_result.output_sha256)
        except Exception as exc:
            return RenderProjectResult(
                subtitle_asset=subtitle_asset,
                final_asset=final_asset,
                subtitle_path=subtitle_path,
                published_path=alias_path,
                provider_result=provider_result,
                alias_status="DEGRADED",
                alias_error=str(exc),
            )
    return RenderProjectResult(
        subtitle_asset=subtitle_asset,
        final_asset=final_asset,
        subtitle_path=subtitle_path,
        published_path=alias_path,
        provider_result=provider_result,
        alias_status="READY",
    )
