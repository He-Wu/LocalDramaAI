import hashlib
import json
import time
from pathlib import Path
from app.db.session import session_scope
from app.models import Asset, Character, Dialogue, GenerationManifest, Shot, VoiceProfile
from app.services.audio_probe import probe_wav


async def generate_dialogue_audio(database_url: str, project_id: str, dialogue_id: str, provider, output_dir: Path):
    with session_scope(database_url) as session:
        dialogue = session.get(Dialogue, dialogue_id)
        if dialogue is None: raise ValueError(f"Dialogue not found: {dialogue_id}")
        shot = session.get(Shot, dialogue.shot_id); character = session.get(Character, dialogue.character_id) if dialogue.character_id else None
        voice = session.query(VoiceProfile).filter_by(character_id=dialogue.character_id).order_by(VoiceProfile.created_at).first() if character else None
        reference = session.get(Asset, voice.reference_asset_id) if voice and voice.reference_asset_id else None
        if voice is None or reference is None or not voice.reference_transcript:
            raise ValueError("Dialogue requires a VoiceProfile with reference audio and transcript")
        text = dialogue.text; emotion = dialogue.emotion; reference_path = Path(reference.path)
        language = voice.language; model_name = voice.model_name; reference_asset_id = reference.id; shot_id = shot.id
    output_path = Path(output_dir) / f"{dialogue_id}.wav"
    started = time.perf_counter()
    generated = await provider.generate(text, output_path, reference_path, voice.reference_transcript, language=language,
                                         metadata={"emotion": emotion, "project_id": project_id, "dialogue_id": dialogue_id})
    generation_time = time.perf_counter() - started
    info = probe_wav(generated)
    metadata = {"dialogue_id": dialogue_id, "project_id": project_id, "model_name": model_name,
                "language": language, "reference_asset": reference_asset_id, "sample_rate": info.sample_rate}
    with session_scope(database_url) as session:
        asset = Asset(project_id=project_id, kind="AUDIO", path=str(generated), mime_type="audio/wav",
                      metadata_json={"duration": info.duration, "sample_rate": info.sample_rate})
        session.add(asset); session.flush()
        workflow_hash = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        session.add(GenerationManifest(asset_id=asset.id, provider="qwen3-tts", provider_version="local-service",
            model_name=model_name, prompt=text, seed=None, workflow_name="qwen3_tts_voice_clone",
            workflow_hash=workflow_hash, binding_version="1", generation_time=generation_time,
            input_assets=[reference_asset_id], output_asset=asset.id))
        dialogue = session.get(Dialogue, dialogue_id); dialogue.audio_asset_id = asset.id; dialogue.duration = info.duration
        shot = session.get(Shot, shot_id); shot.duration = info.duration + 0.3
        session.flush(); return asset
