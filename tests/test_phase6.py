import asyncio
import wave
from pathlib import Path

import httpx
import pytest

from app.db.session import create_schema, session_scope
from app.models import Asset, Character, Dialogue, GenerationManifest, Project, Scene, Shot, VoiceProfile
from app.providers.qwen3_tts_provider import Qwen3TTSProvider
from app.services.audio_probe import probe_wav
from app.services.tts_generation import generate_dialogue_audio


def _pcm_wav(path: Path, seconds: float = 1.25, rate: int = 24000) -> Path:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        output.writeframes(b"\x00\x00" * frames)
    return path


def test_probe_wav_reports_real_pcm_duration(tmp_path):
    path = _pcm_wav(tmp_path / "speech.wav")
    info = probe_wav(path)
    assert info.sample_rate == 24000
    assert info.channels == 1
    assert info.duration == pytest.approx(1.25, abs=0.001)


def test_dialogue_and_voice_profile_persist(tmp_path):
    database = str(tmp_path / "phase6.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="TTS Test"); session.add(project); session.flush()
        character = Character(project_id=project.id, name="林遥", visual_bible_json={}); session.add(character); session.flush()
        voice = VoiceProfile(character_id=character.id, name="林遥克隆", model_name="qwen3-tts-0.6b-base", reference_transcript="你好")
        session.add(voice); session.flush()
        scene = Scene(project_id=project.id, order=1, title="一", description="一"); session.add(scene); session.flush()
        shot = Shot(scene_id=scene.id, character_id=character.id, order=1, title="近景", description="说话", duration=3.0); session.add(shot); session.flush()
        dialogue = Dialogue(shot_id=shot.id, character_id=character.id, order=1, text="你好", emotion="平静")
        session.add(dialogue); session.flush(); dialogue_id, voice_id = dialogue.id, voice.id
    with session_scope(database) as session:
        assert session.get(Dialogue, dialogue_id).audio_asset_id is None
        assert session.get(VoiceProfile, voice_id).reference_transcript == "你好"


def test_provider_rejects_missing_reference_audio(tmp_path):
    provider = Qwen3TTSProvider("http://127.0.0.1:9")
    with pytest.raises(FileNotFoundError):
        asyncio.run(provider.generate("你好", tmp_path / "out.wav", tmp_path / "missing.wav", "你好"))


def test_tts_generation_registers_audio_and_updates_shot(tmp_path):
    database = str(tmp_path / "phase6.db"); reference = _pcm_wav(tmp_path / "reference.wav", 0.5)
    output = _pcm_wav(tmp_path / "generated.wav", 2.0)
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="TTS Test"); session.add(project); session.flush()
        character = Character(project_id=project.id, name="林遥", visual_bible_json={}); session.add(character); session.flush()
        scene = Scene(project_id=project.id, order=1, title="一", description="一"); session.add(scene); session.flush()
        shot = Shot(scene_id=scene.id, character_id=character.id, order=1, title="近景", description="说话", duration=3.0); session.add(shot); session.flush()
        ref_asset = Asset(project_id=project.id, kind="AUDIO", path=str(reference), mime_type="audio/wav"); session.add(ref_asset); session.flush()
        voice = VoiceProfile(character_id=character.id, name="林遥克隆", model_name="qwen3-tts-0.6b-base", reference_asset_id=ref_asset.id, reference_transcript="你好")
        session.add(voice); session.flush()
        dialogue = Dialogue(shot_id=shot.id, character_id=character.id, order=1, text="你好", emotion="平静"); session.add(dialogue); session.flush()
        project_id, dialogue_id, shot_id = project.id, dialogue.id, shot.id

    class DeterministicProvider:
        async def generate(self, text, output_path, reference_path, reference_transcript, language="Chinese", metadata=None):
            output_path.parent.mkdir(parents=True, exist_ok=True); output.replace(output_path)
            return output_path
        async def unload(self): return {"unloaded": True}

    asset = asyncio.run(generate_dialogue_audio(database, project_id, dialogue_id, DeterministicProvider(), tmp_path / "audio"))
    with session_scope(database) as session:
        dialogue = session.get(Dialogue, dialogue_id); shot = session.get(Shot, shot_id)
        assert asset.kind == "AUDIO" and asset.mime_type == "audio/wav"
        assert dialogue.audio_asset_id == asset.id
        assert dialogue.duration == pytest.approx(2.0, abs=0.001)
        assert shot.duration == pytest.approx(2.3, abs=0.001)
        manifest = session.query(GenerationManifest).one()
        assert manifest.generation_time is not None and manifest.generation_time > 0
