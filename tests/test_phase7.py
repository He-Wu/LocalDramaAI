import asyncio
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.db.session import create_schema, session_scope
from app.models import Asset, Dialogue, GenerationManifest, Project, Scene, Shot
from app.providers.ffmpeg_provider import FFmpegProvider
from app.services.dialogue_video_generation import generate_dialogue_video
from app.services.video_probe import probe_video
from scripts.smoke_phase7 import validate_smoke_evidence


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * int(seconds * 24000))
    return path


def _workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "PROMPT"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "negative"}},
        "57": {"class_type": "LoadImage", "inputs": {"image": "IMAGE"}},
        "55": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"width": 640, "height": 368, "length": 49}},
    }


class DeterministicVideoProvider:
    def __init__(self, *, short: bool = False):
        self.workflow = None
        self.metadata = None
        self.short = short

    async def generate(self, workflow, output_dir, metadata):
        self.workflow = workflow
        self.metadata = metadata
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "dialogue.mp4"
        frame = output_dir / "frame.png"
        Image.new("RGB", (640, 368), (40, 70, 100)).save(frame)
        duration = 0.5 if self.short else metadata["required_duration"] + 0.2
        FFmpegProvider().image_to_mp4(frame, path, duration=duration, fps=16)
        manifest = output_dir / "dialogue.manifest.json"
        manifest.write_text('{"generation_time": 0.25, "source_output": "deterministic"}', encoding="utf-8")
        return path, manifest


def _seed_shot(tmp_path: Path, *, with_storyboard: bool = True, with_audio: bool = True):
    database = str(tmp_path / "phase7.db")
    create_schema(database)
    storyboard_path = tmp_path / "storyboard.png"
    Image.new("RGB", (640, 368), (40, 70, 100)).save(storyboard_path)
    audio_path = _wav(tmp_path / "dialogue.wav", 2.48)
    with session_scope(database) as session:
        project = Project(name="Phase 7")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, order=1, title="夜路", description="回家")
        session.add(scene)
        session.flush()
        storyboard = Asset(project_id=project.id, kind="IMAGE", path=str(storyboard_path), mime_type="image/png")
        audio = Asset(project_id=project.id, kind="AUDIO", path=str(audio_path), mime_type="audio/wav")
        session.add_all([storyboard, audio])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            order=1,
            title="对白近景",
            description="人物轻声说话",
            shot_type="DIALOGUE_CLOSEUP",
            duration=2.78,
            image_prompt="approved identity frame",
            storyboard_asset_id=storyboard.id if with_storyboard else None,
        )
        session.add(shot)
        session.flush()
        dialogue = Dialogue(
            shot_id=shot.id,
            order=1,
            text="别怕，我已经找到回家的路了。",
            duration=2.48,
            audio_asset_id=audio.id if with_audio else None,
        )
        session.add(dialogue)
        session.flush()
        return database, project.id, shot.id, storyboard.id, audio.id


def test_dialogue_video_uses_storyboard_audio_duration_and_links_asset(tmp_path):
    database, project_id, shot_id, storyboard_id, audio_id = _seed_shot(tmp_path)
    provider = DeterministicVideoProvider()

    asset = asyncio.run(generate_dialogue_video(
        database,
        project_id,
        shot_id,
        provider,
        _workflow(),
        tmp_path / "comfy-input",
        tmp_path / "video",
        seed=42,
    ))

    assert provider.workflow["57"]["inputs"]["image"].startswith("phase7_")
    assert provider.workflow["55"]["inputs"]["length"] == 49
    assert provider.metadata["input_assets"] == [storyboard_id, audio_id]
    assert Path(provider.workflow["57"]["inputs"]["image"]).name == provider.workflow["57"]["inputs"]["image"]
    with session_scope(database) as session:
        shot = session.get(Shot, shot_id)
        assert shot.video_asset_id == asset.id
        assert shot.duration == pytest.approx(2.78)
        manifest = session.query(GenerationManifest).filter_by(asset_id=asset.id).one()
        assert manifest.input_assets == [storyboard_id, audio_id]


def test_probe_video_reports_real_duration_profile(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (640, 368), (10, 20, 30)).save(frame)
    path = FFmpegProvider().image_to_mp4(frame, tmp_path / "probe.mp4", duration=1.0, fps=16)
    info = probe_video(path)
    assert info.codec == "h264"
    assert (info.width, info.height) == (640, 368)
    assert info.fps == pytest.approx(16.0)
    assert info.duration >= 1.0


@pytest.mark.parametrize("with_storyboard,with_audio,message", [
    (False, True, "storyboard"),
    (True, False, "audio"),
])
def test_dialogue_video_rejects_missing_required_assets(tmp_path, with_storyboard, with_audio, message):
    database, project_id, shot_id, _, _ = _seed_shot(
        tmp_path,
        with_storyboard=with_storyboard,
        with_audio=with_audio,
    )
    provider = DeterministicVideoProvider()
    with pytest.raises(ValueError, match=message):
        asyncio.run(generate_dialogue_video(
            database,
            project_id,
            shot_id,
            provider,
            _workflow(),
            tmp_path / "comfy-input",
            tmp_path / "video",
        ))
    assert provider.workflow is None
    with session_scope(database) as session:
        assert session.get(Shot, shot_id).video_asset_id is None


def test_dialogue_video_rejects_malformed_wav_before_provider(tmp_path):
    database, project_id, shot_id, _, audio_id = _seed_shot(tmp_path)
    with session_scope(database) as session:
        Path(session.get(Asset, audio_id).path).write_bytes(b"not a wav")
    provider = DeterministicVideoProvider()
    with pytest.raises(ValueError, match="WAV"):
        asyncio.run(generate_dialogue_video(
            database, project_id, shot_id, provider, _workflow(),
            tmp_path / "comfy-input", tmp_path / "video",
        ))
    assert provider.workflow is None


def test_dialogue_video_rejects_short_provider_output_without_registration(tmp_path):
    database, project_id, shot_id, _, _ = _seed_shot(tmp_path)
    provider = DeterministicVideoProvider(short=True)
    with pytest.raises(RuntimeError, match="shorter"):
        asyncio.run(generate_dialogue_video(
            database, project_id, shot_id, provider, _workflow(),
            tmp_path / "comfy-input", tmp_path / "video",
        ))
    with session_scope(database) as session:
        assert session.get(Shot, shot_id).video_asset_id is None
        assert session.query(Asset).filter_by(kind="VIDEO").count() == 0
        assert session.query(GenerationManifest).count() == 0


def test_dialogue_video_rejects_non_locked_fps_before_provider(tmp_path):
    database, project_id, shot_id, _, _ = _seed_shot(tmp_path)
    provider = DeterministicVideoProvider()
    with pytest.raises(ValueError, match="16 FPS"):
        asyncio.run(generate_dialogue_video(
            database, project_id, shot_id, provider, _workflow(),
            tmp_path / "comfy-input", tmp_path / "video", fps=8,
        ))
    assert provider.workflow is None


def test_phase7_smoke_validation_rejects_short_video():
    probe = {
        "streams": [{"codec_name": "h264", "width": 640, "height": 368, "r_frame_rate": "16/1", "nb_frames": "49"}],
        "format": {"duration": "2.500000", "size": "10000"},
    }
    with pytest.raises(RuntimeError, match="shorter"):
        validate_smoke_evidence(
            probe,
            dialogue_duration=2.48,
            shot_duration=2.78,
            shot_video_asset_id="video",
            video_asset_id="video",
            manifest_input_assets=["storyboard", "audio"],
            expected_input_assets=["storyboard", "audio"],
            manifest_output_asset="video",
            free_status=200,
        )
