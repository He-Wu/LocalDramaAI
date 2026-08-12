from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.db.session import session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot
from app.providers.ffmpeg_provider import FFmpegProvider
from app.services.audio_probe import probe_wav
from app.services.video_probe import VideoInfo, probe_video


_AUDIO_DURATION_TOLERANCE = 0.02
_VIDEO_DURATION_TOLERANCE = 0.12


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _milliseconds(value: float | Decimal) -> int:
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=f".tmp{path.suffix}",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FinalRenderService:
    def __init__(
        self,
        database_url: str,
        ffmpeg: FFmpegProvider,
        output_dir: Path,
    ) -> None:
        self.database_url = database_url
        self.ffmpeg = ffmpeg
        self.output_dir = Path(output_dir)

    def mux_shots(self, project_id: str) -> dict[str, Any]:
        shots = self._shot_snapshot(project_id)
        project_dir = self._project_dir(project_id)
        shot_dir = project_dir / "shots"
        muxed_paths: list[str] = []
        evidences: list[dict[str, Any]] = []

        for index, shot in enumerate(shots, start=1):
            output_path = shot_dir / f"{index:04d}-{shot['id']}.mp4"
            audio_path = shot_dir / f"{index:04d}-{shot['id']}.wav"
            state_path = shot_dir / f"{index:04d}-{shot['id']}.mux.json"
            evidence = self._shot_evidence(shot)
            state_hash = _canonical_hash(evidence)
            if not self._valid_mux_cache(
                output_path,
                state_path,
                state_hash,
                shot["video_info"].duration,
            ):
                audio_candidate = self.ffmpeg._temporary_path(audio_path)
                mux_candidate = self.ffmpeg._temporary_path(output_path)
                try:
                    dialogue_paths = [
                        item["audio_path"] for item in shot["dialogues"]
                    ]
                    if dialogue_paths:
                        self.ffmpeg.concat_audio(dialogue_paths, audio_candidate)
                        concatenated = probe_wav(audio_candidate)
                        expected = sum(
                            item["duration"] for item in shot["dialogues"]
                        )
                        if (
                            abs(concatenated.duration - expected)
                            > _AUDIO_DURATION_TOLERANCE
                        ):
                            raise RuntimeError(
                                "concatenated dialogue duration does not match "
                                "persisted durations"
                            )
                    else:
                        self.ffmpeg.create_silence(
                            audio_candidate,
                            duration=shot["video_info"].duration,
                        )
                        silence = probe_wav(audio_candidate)
                        if (
                            abs(silence.duration - shot["video_info"].duration)
                            > _AUDIO_DURATION_TOLERANCE
                        ):
                            raise RuntimeError(
                                "generated silence does not match shot video duration"
                            )
                    self.ffmpeg.mux_audio(
                        shot["video_path"], audio_candidate, mux_candidate
                    )
                    self._validate_muxed(
                        mux_candidate, shot["video_info"].duration
                    )
                    audio_candidate.replace(audio_path)
                    mux_candidate.replace(output_path)
                finally:
                    audio_candidate.unlink(missing_ok=True)
                    mux_candidate.unlink(missing_ok=True)
                state = {
                    "input_hash": state_hash,
                    "output_sha256": _sha256(output_path),
                }
                _atomic_write(
                    state_path,
                    (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
                )
            muxed_paths.append(str(output_path))
            evidences.append(
                {
                    "shot_id": shot["id"],
                    "input_hash": state_hash,
                    "sha256": _sha256(output_path),
                }
            )

        return {
            "project_id": project_id,
            "shot_ids": [shot["id"] for shot in shots],
            "muxed_paths": muxed_paths,
            "evidence": evidences,
        }

    def concat_project(
        self,
        project_id: str,
        muxed_paths: list[str | Path] | dict[str, Any],
    ) -> dict[str, Any]:
        shots = self._shot_snapshot(project_id)
        project_dir = self._project_dir(project_id)
        if isinstance(muxed_paths, dict):
            muxed_paths = muxed_paths.get("muxed_paths", [])
        normalized = [Path(path) for path in muxed_paths]
        expected = [
            project_dir / "shots" / f"{index:04d}-{shot['id']}.mp4"
            for index, shot in enumerate(shots, start=1)
        ]
        if len(normalized) != len(expected) or any(
            actual.resolve() != wanted.resolve()
            for actual, wanted in zip(normalized, expected)
        ):
            raise ValueError("muxed paths must be the ordered project output paths")
        input_hashes = []
        for path, shot in zip(normalized, shots):
            self._validate_muxed(path, shot["video_info"].duration)
            mux_hash = _sha256(path)
            state_path = path.with_suffix(".mux.json")
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"muxed shot evidence is missing or invalid: {path}") from exc
            if (
                not isinstance(state, dict)
                or state.get("input_hash")
                != _canonical_hash(self._shot_evidence(shot))
                or state.get("output_sha256") != mux_hash
            ):
                raise ValueError(f"muxed shot evidence does not match project inputs: {path}")
            input_hashes.append(mux_hash)

        output_path = project_dir / "final.mp4"
        existing = self._assets_for_kind(project_id, "FINAL_VIDEO")
        reusable = next(
            (
                asset
                for asset in existing
                if Path(asset["path"]).resolve() == output_path.resolve()
                and (asset["metadata"] or {}).get("input_sha256") == input_hashes
                and self._valid_file_hash(output_path, (asset["metadata"] or {}).get("sha256"))
                and self._valid_final_video(output_path)
            ),
            None,
        )
        if reusable is None:
            candidate = self.ffmpeg._temporary_path(output_path)
            try:
                self.ffmpeg.concat(normalized, candidate)
                metadata = self._final_video_metadata(candidate, input_hashes, shots)
                asset_id = self._publish_registered_asset(
                    project_id,
                    kind="FINAL_VIDEO",
                    candidate=candidate,
                    destination=output_path,
                    mime_type="video/mp4",
                    metadata=metadata,
                )
            finally:
                candidate.unlink(missing_ok=True)
        else:
            metadata = self._final_video_metadata(output_path, input_hashes, shots)
            asset_id = self._register_single_asset(
                project_id,
                kind="FINAL_VIDEO",
                path=output_path,
                mime_type="video/mp4",
                metadata=metadata,
            )
        return {
            "asset_id": asset_id,
            "path": str(output_path),
            "metadata": metadata,
        }

    def export_subtitles(self, project_id: str) -> dict[str, Any]:
        shots = self._shot_snapshot(project_id)
        sequence = 1
        shot_cursor = Decimal("0")
        blocks: list[str] = []
        for shot in shots:
            dialogue_cursor = shot_cursor
            for dialogue in shot["dialogues"]:
                end = dialogue_cursor + Decimal(str(dialogue["duration"]))
                blocks.append(
                    f"{sequence}\n{_srt_time(_milliseconds(dialogue_cursor))} --> "
                    f"{_srt_time(_milliseconds(end))}"
                    f"\n{dialogue['text']}"
                )
                sequence += 1
                dialogue_cursor = end
            shot_cursor += Decimal(str(shot["duration"]))

        text = "\n\n".join(blocks)
        if text:
            text += "\n"
        payload = codecs.BOM_UTF8 + text.encode("utf-8")
        output_path = self._project_dir(project_id) / "subtitles.srt"
        candidate = self.ffmpeg._temporary_path(output_path)
        try:
            candidate.write_bytes(payload)
            if candidate.read_bytes() != payload:
                raise RuntimeError("subtitle export validation failed")
            metadata = {
                "sha256": _sha256(candidate),
                "size": candidate.stat().st_size,
                "entries": sequence - 1,
                "timeline_duration_ms": _milliseconds(shot_cursor),
                "encoding": "utf-8-sig",
            }
            asset_id = self._publish_registered_asset(
                project_id,
                kind="SUBTITLE",
                candidate=candidate,
                destination=output_path,
                mime_type="application/x-subrip",
                metadata=metadata,
            )
        finally:
            candidate.unlink(missing_ok=True)
        return {
            "asset_id": asset_id,
            "path": str(output_path),
            "metadata": metadata,
        }

    def export_manifest(
        self,
        project_id: str,
        final_video_asset_id: str,
    ) -> dict[str, Any]:
        shots = self._shot_snapshot(project_id)
        with session_scope(self.database_url) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"Project not found: {project_id}")
            for shot in shots:
                storyboard_asset_id = shot["storyboard_asset_id"]
                if storyboard_asset_id is None:
                    continue
                storyboard = session.get(Asset, storyboard_asset_id)
                if storyboard is None or storyboard.project_id != project_id:
                    raise ValueError(
                        "shot storyboard asset does not belong to project"
                    )
                if storyboard.kind != "IMAGE":
                    raise ValueError("shot storyboard asset must have kind IMAGE")
                storyboard_path = Path(storyboard.path)
                if (
                    not storyboard_path.is_file()
                    or storyboard_path.stat().st_size <= 0
                ):
                    raise ValueError(
                        f"shot storyboard file is missing or empty: {storyboard_path}"
                    )
                storyboard_metadata = storyboard.metadata_json
                if (
                    isinstance(storyboard_metadata, dict)
                    and storyboard_metadata.get("sha256") is not None
                    and storyboard_metadata["sha256"] != _sha256(storyboard_path)
                ):
                    raise ValueError(
                        "shot storyboard file does not match registered evidence"
                    )
            final_asset = session.get(Asset, final_video_asset_id)
            if final_asset is None or final_asset.project_id != project_id:
                raise ValueError("final video asset does not belong to project")
            if final_asset.kind != "FINAL_VIDEO":
                raise ValueError("final video asset must have kind FINAL_VIDEO")
            final_path = Path(final_asset.path)
            expected_final_path = self._project_dir(project_id) / "final.mp4"
            if final_path.resolve() != expected_final_path.resolve():
                raise ValueError(
                    "final video asset must reference the deterministic project output"
                )
            registered_metadata = final_asset.metadata_json
            if (
                not isinstance(registered_metadata, dict)
                or registered_metadata.get("sha256") != _sha256(final_path)
                or registered_metadata.get("size") != final_path.stat().st_size
                or registered_metadata.get("shot_ids")
                != [shot["id"] for shot in shots]
            ):
                raise ValueError("final video does not match registered asset evidence")
            registered_inputs = registered_metadata.get("input_sha256")
            if not isinstance(registered_inputs, list) or len(registered_inputs) != len(shots):
                raise ValueError("final video does not match registered asset evidence")
            for index, (shot, registered_hash) in enumerate(
                zip(shots, registered_inputs), start=1
            ):
                muxed_path = (
                    self._project_dir(project_id)
                    / "shots"
                    / f"{index:04d}-{shot['id']}.mp4"
                )
                if (
                    not self._valid_mux_cache(
                        muxed_path,
                        muxed_path.with_suffix(".mux.json"),
                        _canonical_hash(self._shot_evidence(shot)),
                        shot["video_info"].duration,
                    )
                    or _sha256(muxed_path) != registered_hash
                ):
                    raise ValueError("final video does not match registered asset evidence")
            self._final_video_metadata(final_path, [], shots)

            scenes = (
                session.query(Scene)
                .filter_by(project_id=project_id)
                .order_by(Scene.order, Scene.id)
                .all()
            )
            assets = (
                session.query(Asset)
                .filter(Asset.project_id == project_id, Asset.kind != "MANIFEST")
                .order_by(Asset.kind, Asset.id)
                .all()
            )
            asset_ids = [asset.id for asset in assets]
            generation_records = (
                session.query(GenerationManifest)
                .filter(GenerationManifest.asset_id.in_(asset_ids))
                .order_by(GenerationManifest.asset_id, GenerationManifest.id)
                .all()
                if asset_ids
                else []
            )
            owned_asset_ids = set(asset_ids)
            for record in generation_records:
                input_assets = record.input_assets or []
                if not isinstance(input_assets, list):
                    raise ValueError(
                        "generation manifest input asset does not belong to project"
                    )
                for asset_id in input_assets:
                    referenced_asset = session.get(Asset, asset_id)
                    if (
                        referenced_asset is not None
                        and referenced_asset.id not in owned_asset_ids
                    ):
                        raise ValueError(
                            "generation manifest input asset does not belong to project"
                        )
                if (
                    record.output_asset is not None
                    and record.output_asset not in owned_asset_ids
                ):
                    raise ValueError(
                        "generation manifest output asset does not belong to project"
                    )
            project_payload = {
                "id": project.id,
                "name": project.name,
                "story": project.story,
                "description": project.description,
                "language": project.language,
                "style": project.style,
                "status": project.status,
            }
            scene_payload = [
                {
                    "id": scene.id,
                    "project_id": scene.project_id,
                    "order": scene.order,
                    "title": scene.title,
                    "description": scene.description,
                    "location": scene.location,
                    "time_of_day": scene.time_of_day,
                    "mood": scene.mood,
                    "estimated_duration": scene.estimated_duration,
                }
                for scene in scenes
            ]
            asset_payload = []
            for asset in assets:
                path = Path(asset.path)
                if not path.is_file() or path.stat().st_size <= 0:
                    raise ValueError(f"project asset file is missing or empty: {path}")
                asset_payload.append(
                    {
                        "id": asset.id,
                        "project_id": asset.project_id,
                        "kind": asset.kind,
                        "path": str(path),
                        "mime_type": asset.mime_type,
                        "metadata": asset.metadata_json,
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
            generation_payload = [
                {
                    "asset_id": record.asset_id,
                    "provider": record.provider,
                    "provider_version": record.provider_version,
                    "model_name": record.model_name,
                    "seed": record.seed,
                    "workflow_name": record.workflow_name,
                    "workflow_hash": record.workflow_hash,
                    "binding_version": record.binding_version,
                    "generation_time": record.generation_time,
                    "input_assets": record.input_assets,
                    "output_asset": record.output_asset,
                }
                for record in generation_records
            ]

        shot_payload = [self._manifest_shot(shot) for shot in shots]
        dialogue_payload = [
            self._manifest_dialogue(shot, dialogue)
            for shot in shots
            for dialogue in shot["dialogues"]
        ]
        manifest = {
            "schema_version": 1,
            "project": project_payload,
            "final_video_asset_id": final_video_asset_id,
            "scenes": scene_payload,
            "shots": shot_payload,
            "dialogues": dialogue_payload,
            "assets": asset_payload,
            "generation_manifests": generation_payload,
        }
        payload = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        output_path = self._project_dir(project_id) / "manifest.json"
        candidate = self.ffmpeg._temporary_path(output_path)
        try:
            candidate.write_bytes(payload)
            try:
                decoded = json.loads(candidate.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("manifest export validation failed") from exc
            if decoded != manifest:
                raise RuntimeError("manifest export validation failed")
            sha256 = _sha256(candidate)
            metadata = {
                "sha256": sha256,
                "size": candidate.stat().st_size,
                "final_video_asset_id": final_video_asset_id,
                "schema_version": 1,
            }
            asset_id = self._publish_registered_asset(
                project_id,
                kind="MANIFEST",
                candidate=candidate,
                destination=output_path,
                mime_type="application/json",
                metadata=metadata,
            )
        finally:
            candidate.unlink(missing_ok=True)
        return {
            "asset_id": asset_id,
            "path": str(output_path),
            "sha256": sha256,
            "metadata": metadata,
        }

    def render(self, project_id: str) -> dict[str, Any]:
        muxed = self.mux_shots(project_id)
        final = self.concat_project(project_id, muxed["muxed_paths"])
        subtitle = self.export_subtitles(project_id)
        manifest = self.export_manifest(project_id, final["asset_id"])
        return {
            "video_asset_id": final["asset_id"],
            "video_path": final["path"],
            "subtitle_asset_id": subtitle["asset_id"],
            "subtitle_path": subtitle["path"],
            "manifest_asset_id": manifest["asset_id"],
            "manifest_path": manifest["path"],
            "muxed_paths": muxed["muxed_paths"],
        }

    def _shot_snapshot(self, project_id: str) -> list[dict[str, Any]]:
        self._project_dir(project_id)
        with session_scope(self.database_url) as session:
            if session.get(Project, project_id) is None:
                raise ValueError(f"Project not found: {project_id}")
            rows = (
                session.query(Shot, Scene)
                .join(Scene, Shot.scene_id == Scene.id)
                .filter(Scene.project_id == project_id)
                .order_by(Scene.order, Shot.order, Scene.id, Shot.id)
                .all()
            )
            if not rows:
                raise ValueError("project requires at least one shot")
            result = []
            for shot, scene in rows:
                if not math.isfinite(shot.duration) or shot.duration <= 0:
                    raise ValueError("shot duration must be a positive finite number")
                if not shot.video_asset_id:
                    raise ValueError("shot requires a VIDEO asset")
                video_asset = session.get(Asset, shot.video_asset_id)
                if video_asset is None or video_asset.project_id != project_id:
                    raise ValueError("shot VIDEO asset does not belong to project")
                if video_asset.kind != "VIDEO":
                    raise ValueError("shot asset must have kind VIDEO")
                video_path = Path(video_asset.path)
                video_info = probe_video(video_path, executable=self.ffmpeg.probe_executable)
                if video_info.duration + (1 / video_info.fps) < shot.duration:
                    raise ValueError("shot video is shorter than persisted shot duration")
                dialogues = (
                    session.query(Dialogue)
                    .filter_by(shot_id=shot.id)
                    .order_by(Dialogue.order, Dialogue.id)
                    .all()
                )
                dialogue_payload = []
                for dialogue in dialogues:
                    if (
                        dialogue.duration is None
                        or not math.isfinite(dialogue.duration)
                        or dialogue.duration <= 0
                    ):
                        raise ValueError("dialogue duration must be a positive finite number")
                    if not dialogue.audio_asset_id:
                        raise ValueError("dialogue requires an AUDIO asset")
                    audio_asset = session.get(Asset, dialogue.audio_asset_id)
                    if audio_asset is None or audio_asset.project_id != project_id:
                        raise ValueError("dialogue AUDIO asset does not belong to project")
                    if audio_asset.kind != "AUDIO":
                        raise ValueError("dialogue asset must have kind AUDIO")
                    audio_path = Path(audio_asset.path)
                    audio_info = probe_wav(audio_path)
                    if abs(audio_info.duration - dialogue.duration) > _AUDIO_DURATION_TOLERANCE:
                        raise ValueError(
                            "persisted dialogue duration does not match measured WAV duration"
                        )
                    dialogue_payload.append(
                        {
                            "id": dialogue.id,
                            "order": dialogue.order,
                            "text": dialogue.text,
                            "emotion": dialogue.emotion,
                            "character_id": dialogue.character_id,
                            "start_time": dialogue.start_time,
                            "end_time": dialogue.end_time,
                            "duration": dialogue.duration,
                            "audio_asset_id": audio_asset.id,
                            "audio_path": audio_path,
                            "audio_sha256": _sha256(audio_path),
                        }
                    )
                dialogue_duration = sum(item["duration"] for item in dialogue_payload)
                if dialogue_duration - shot.duration > _AUDIO_DURATION_TOLERANCE:
                    raise ValueError("dialogue durations exceed persisted shot duration")
                if dialogue_duration - video_info.duration > _VIDEO_DURATION_TOLERANCE:
                    raise ValueError("dialogue durations exceed shot video duration")
                result.append(
                    {
                        "id": shot.id,
                        "scene_id": scene.id,
                        "scene_order": scene.order,
                        "order": shot.order,
                        "title": shot.title,
                        "description": shot.description,
                        "shot_type": shot.shot_type,
                        "duration": shot.duration,
                        "character_id": shot.character_id,
                        "image_prompt": shot.image_prompt,
                        "video_prompt": shot.video_prompt,
                        "negative_prompt": shot.negative_prompt,
                        "storyboard_asset_id": shot.storyboard_asset_id,
                        "video_asset_id": video_asset.id,
                        "status": shot.status,
                        "video_path": video_path,
                        "video_sha256": _sha256(video_path),
                        "video_info": video_info,
                        "dialogues": dialogue_payload,
                    }
                )
            return result

    def _project_dir(self, project_id: str) -> Path:
        if not project_id or Path(project_id).name != project_id:
            raise ValueError("project id must be a safe path component")
        root = self.output_dir.resolve()
        project_dir = (root / project_id).resolve()
        if project_dir.parent != root:
            raise ValueError("project output must remain under output_dir")
        return project_dir

    @staticmethod
    def _shot_evidence(shot: dict[str, Any]) -> dict[str, Any]:
        return {
            "shot_id": shot["id"],
            "shot_duration": shot["duration"],
            "video_asset_id": shot["video_asset_id"],
            "video_sha256": shot["video_sha256"],
            "video_duration": shot["video_info"].duration,
            "dialogues": [
                {
                    "id": dialogue["id"],
                    "order": dialogue["order"],
                    "duration": dialogue["duration"],
                    "audio_asset_id": dialogue["audio_asset_id"],
                    "audio_sha256": dialogue["audio_sha256"],
                }
                for dialogue in shot["dialogues"]
            ],
        }

    def _valid_mux_cache(
        self,
        output_path: Path,
        state_path: Path,
        input_hash: str,
        expected_duration: float,
    ) -> bool:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return False
            if state.get("input_hash") != input_hash:
                return False
            if not self._valid_file_hash(output_path, state.get("output_sha256")):
                return False
            self._validate_muxed(output_path, expected_duration)
            return True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def _validate_muxed(self, path: Path, expected_duration: float) -> None:
        info = probe_video(path, executable=self.ffmpeg.probe_executable)
        signature = self.ffmpeg._media_signature(path)
        if signature["video.codec"] != "h264" or signature["audio.codec"] != "aac":
            raise ValueError("muxed shot requires H.264 video and AAC audio")
        if abs(info.duration - expected_duration) > _VIDEO_DURATION_TOLERANCE:
            raise ValueError("muxed shot duration does not preserve the source video")

    def _valid_final_video(self, path: Path) -> bool:
        try:
            signature = self.ffmpeg._media_signature(path)
            info = probe_video(path, executable=self.ffmpeg.probe_executable)
            return (
                signature["video.codec"] == "h264"
                and signature["audio.codec"] == "aac"
                and info.duration > 0
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def _final_video_metadata(
        self,
        output_path: Path,
        input_hashes: list[str],
        shots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        info = probe_video(output_path, executable=self.ffmpeg.probe_executable)
        signature = self.ffmpeg._media_signature(output_path)
        if signature["video.codec"] != "h264" or signature["audio.codec"] != "aac":
            raise ValueError("final video requires H.264 video and AAC audio")
        expected_duration = sum(shot["video_info"].duration for shot in shots)
        publication_tolerance = min(
            0.05,
            min(shot["video_info"].duration for shot in shots) / 2,
        )
        if abs(info.duration - expected_duration) > publication_tolerance:
            raise ValueError(
                "final video duration does not match the ordered shot timeline"
            )
        return {
            "sha256": _sha256(output_path),
            "size": output_path.stat().st_size,
            "duration": info.duration,
            "video": {
                "codec": info.codec,
                "width": info.width,
                "height": info.height,
                "fps": info.fps,
                "frames": info.frames,
            },
            "audio": {
                "codec": signature["audio.codec"],
                "sample_rate": signature["audio.sample_rate"],
                "channels": signature["audio.channels"],
                "channel_layout": signature["audio.channel_layout"],
            },
            "input_sha256": input_hashes,
            "shot_ids": [shot["id"] for shot in shots],
        }

    def _assets_for_kind(self, project_id: str, kind: str) -> list[dict[str, Any]]:
        with session_scope(self.database_url) as session:
            assets = (
                session.query(Asset)
                .filter_by(project_id=project_id, kind=kind)
                .order_by(Asset.created_at, Asset.id)
                .all()
            )
            return [
                {
                    "id": asset.id,
                    "path": asset.path,
                    "metadata": asset.metadata_json,
                }
                for asset in assets
            ]

    def _register_single_asset(
        self,
        project_id: str,
        *,
        kind: str,
        path: Path,
        mime_type: str,
        metadata: dict[str, Any],
    ) -> str:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"cannot register missing or empty {kind} file: {path}")
        with session_scope(self.database_url) as session:
            if session.get(Project, project_id) is None:
                raise ValueError(f"Project not found: {project_id}")
            existing = (
                session.query(Asset)
                .filter_by(project_id=project_id, kind=kind)
                .order_by(Asset.created_at, Asset.id)
                .all()
            )
            if existing:
                asset = existing[0]
                for duplicate in existing[1:]:
                    session.delete(duplicate)
            else:
                asset = Asset(project_id=project_id, kind=kind, path=str(path))
                session.add(asset)
            asset.path = str(path)
            asset.mime_type = mime_type
            asset.metadata_json = metadata
            session.flush()
            return asset.id

    def _publish_registered_asset(
        self,
        project_id: str,
        *,
        kind: str,
        candidate: Path,
        destination: Path,
        mime_type: str,
        metadata: dict[str, Any],
    ) -> str:
        candidate = Path(candidate)
        destination = Path(destination)
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise ValueError(f"cannot publish missing or empty {kind} candidate")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        old_moved = False
        new_published = False
        if destination.exists():
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.",
                suffix=f".bak{destination.suffix}",
                dir=destination.parent,
            )
            os.close(descriptor)
            backup = Path(backup_name)
        try:
            if backup is not None:
                destination.replace(backup)
                old_moved = True
            candidate.replace(destination)
            new_published = True
            asset_id = self._register_single_asset(
                project_id,
                kind=kind,
                path=destination,
                mime_type=mime_type,
                metadata=metadata,
            )
        except Exception:
            if old_moved and backup is not None:
                backup.replace(destination)
                old_moved = False
                new_published = False
            elif new_published:
                destination.unlink(missing_ok=True)
                new_published = False
            raise
        else:
            if old_moved and backup is not None:
                old_moved = False
                try:
                    backup.unlink()
                except OSError:
                    pass
            return asset_id
        finally:
            candidate.unlink(missing_ok=True)
            if backup is not None and backup.exists() and not old_moved:
                try:
                    backup.unlink()
                except OSError:
                    pass

    @staticmethod
    def _valid_file_hash(path: Path, expected_hash: str | None) -> bool:
        return bool(
            expected_hash
            and path.is_file()
            and path.stat().st_size > 0
            and _sha256(path) == expected_hash
        )

    @staticmethod
    def _manifest_shot(shot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": shot["id"],
            "scene_id": shot["scene_id"],
            "order": shot["order"],
            "title": shot["title"],
            "description": shot["description"],
            "shot_type": shot["shot_type"],
            "duration": shot["duration"],
            "character_id": shot["character_id"],
            "image_prompt": shot["image_prompt"],
            "video_prompt": shot["video_prompt"],
            "negative_prompt": shot["negative_prompt"],
            "storyboard_asset_id": shot["storyboard_asset_id"],
            "video_asset_id": shot["video_asset_id"],
            "status": shot["status"],
        }

    @staticmethod
    def _manifest_dialogue(
        shot: dict[str, Any],
        dialogue: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": dialogue["id"],
            "shot_id": shot["id"],
            "order": dialogue["order"],
            "character_id": dialogue["character_id"],
            "text": dialogue["text"],
            "emotion": dialogue["emotion"],
            "audio_asset_id": dialogue["audio_asset_id"],
            "start_time": dialogue["start_time"],
            "end_time": dialogue["end_time"],
            "duration": dialogue["duration"],
        }
