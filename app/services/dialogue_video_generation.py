import hashlib
import json
import shutil
from pathlib import Path

from app.comfyui.video_workflow import bind_video_workflow, frame_count_for_duration
from app.db.session import session_scope
from app.models import Asset, Dialogue, GenerationManifest, Shot
from app.services.audio_probe import probe_wav
from app.services.video_probe import probe_video


def _copy_storyboard_atomic(source: Path, input_dir: Path, shot_id: str) -> str:
    input_dir.mkdir(parents=True, exist_ok=True)
    filename = f"phase7_{shot_id}{source.suffix.lower()}"
    final = input_dir / filename
    temporary = final.with_name(f"{final.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(final)
    return filename


async def generate_dialogue_video(
    database_url: str,
    project_id: str,
    shot_id: str,
    provider,
    workflow: dict,
    comfy_input_dir: Path,
    output_dir: Path,
    *,
    seed: int = 20260810,
    width: int = 640,
    height: int = 368,
    fps: int = 16,
):
    """Generate one dialogue Shot from its persisted audio and approved first frame."""
    if fps != 16:
        raise ValueError("PHASE 7 uses the locked 16 FPS Wan profile")
    with session_scope(database_url) as session:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.scene.project_id != project_id:
            raise ValueError("shot does not belong to project")
        if not shot.storyboard_asset_id:
            raise ValueError("dialogue video requires a storyboard asset")
        storyboard = session.get(Asset, shot.storyboard_asset_id)
        if storyboard is None or storyboard.kind != "IMAGE":
            raise ValueError("dialogue video storyboard asset is invalid")
        storyboard_path = Path(storyboard.path)
        if not storyboard_path.is_file() or storyboard_path.stat().st_size == 0:
            raise FileNotFoundError(f"storyboard file is missing or empty: {storyboard_path}")

        dialogues = session.query(Dialogue).filter_by(shot_id=shot_id).order_by(Dialogue.order).all()
        if not dialogues:
            raise ValueError("dialogue video requires at least one dialogue")
        audio_asset_ids = []
        audio_duration = 0.0
        for dialogue in dialogues:
            if not dialogue.audio_asset_id or not dialogue.duration or dialogue.duration <= 0:
                raise ValueError("dialogue video requires generated audio with measured duration")
            audio = session.get(Asset, dialogue.audio_asset_id)
            if audio is None or audio.kind != "AUDIO":
                raise ValueError("dialogue audio asset is invalid")
            audio_path = Path(audio.path)
            if not audio_path.is_file() or audio_path.stat().st_size == 0:
                raise FileNotFoundError(f"dialogue audio file is missing or empty: {audio_path}")
            wav_info = probe_wav(audio_path)
            if abs(wav_info.duration - dialogue.duration) > 0.02:
                raise ValueError("persisted dialogue duration does not match measured WAV duration")
            audio_asset_ids.append(audio.id)
            audio_duration += wav_info.duration

        required_duration = max(float(shot.duration or 0), audio_duration + 0.3)
        frames = frame_count_for_duration(required_duration, fps=fps)
        prompt = shot.video_prompt or shot.description
        negative_prompt = shot.negative_prompt
        storyboard_asset_id = storyboard.id

    image_name = _copy_storyboard_atomic(storyboard_path, Path(comfy_input_dir), shot_id)
    bound = bind_video_workflow(
        workflow,
        image_name=image_name,
        prompt=prompt,
        seed=seed,
        width=width,
        height=height,
        frames=frames,
        filename_prefix=f"LocalDramaAI/phase7/{shot_id}",
        fps=fps,
    )
    metadata = {
        "model_name": "wan2.2_ti2v_5B_fp16",
        "workflow_name": "wan22_i2v_5b_dialogue",
        "provider_version": "ComfyUI 0.31.0",
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "fps": fps,
        "width": width,
        "height": height,
        "frames": frames,
        "required_duration": required_duration,
        "input_assets": [storyboard_asset_id, *audio_asset_ids],
    }
    path, manifest_path = await provider.generate(
        bound,
        Path(output_dir),
        metadata,
    )
    video_path = Path(path)
    video_info = probe_video(video_path)
    if video_info.codec != "h264":
        raise RuntimeError(f"dialogue video must be H.264, got {video_info.codec}")
    if (video_info.width, video_info.height) != (width, height):
        raise RuntimeError(f"dialogue video dimensions are {video_info.width}x{video_info.height}, expected {width}x{height}")
    if abs(video_info.fps - fps) > 0.01:
        raise RuntimeError(f"dialogue video FPS is {video_info.fps}, expected {fps}")
    if video_info.duration + (1 / fps) < required_duration:
        raise RuntimeError(f"dialogue video is shorter than required: {video_info.duration:.4f}s < {required_duration:.4f}s")
    manifest_path = Path(manifest_path)
    try:
        generation_record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("video provider did not produce a valid generation manifest") from exc
    workflow_hash = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()

    with session_scope(database_url) as session:
        asset = Asset(
            project_id=project_id,
            kind="VIDEO",
            path=str(video_path),
            mime_type="video/mp4",
            metadata_json={
                "manifest_path": str(manifest_path),
                "source_output": generation_record.get("source_output"),
                "duration": video_info.duration,
                "width": video_info.width,
                "height": video_info.height,
                "fps": video_info.fps,
                "frames": video_info.frames,
            },
        )
        session.add(asset)
        session.flush()
        session.add(GenerationManifest(
            asset_id=asset.id,
            provider="comfyui",
            provider_version=metadata["provider_version"],
            model_name=metadata["model_name"],
            prompt=metadata["prompt"],
            negative_prompt=metadata["negative_prompt"],
            seed=metadata["seed"],
            workflow_name=metadata["workflow_name"],
            workflow_hash=workflow_hash,
            binding_version="1",
            generation_time=generation_record.get("generation_time"),
            input_assets=metadata["input_assets"],
            output_asset=asset.id,
        ))
        shot = session.get(Shot, shot_id)
        shot.duration = required_duration
        shot.video_asset_id = asset.id
        shot.status = "VIDEO_GENERATED"
        session.flush()
    return asset
