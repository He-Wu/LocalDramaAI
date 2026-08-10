"""Real Qwen3-TTS 0.6B voice-clone smoke test."""
import asyncio
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import psutil

from app.db.session import create_schema, session_scope
from app.models import Asset, Character, Dialogue, GenerationManifest, Project, Scene, Shot, VoiceProfile
from app.providers.qwen3_tts_provider import Qwen3TTSProvider
from app.services.audio_probe import probe_wav
from app.services.tts_generation import generate_dialogue_audio

ROOT = Path(__file__).resolve().parents[1]
TTS_PYTHON = Path("E:/LocalDramaAI/env-tts/Scripts/python.exe")
MODEL_PATH = Path("E:/LocalDramaAI/Models/Qwen3-TTS-12Hz-0.6B-Base")
REFERENCE_PATH = Path("E:/LocalDramaAI/Storage/shared/voices/qwen3_clone_reference.wav")
REFERENCE_TRANSCRIPT = "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you."
URL = "http://127.0.0.1:8020"


class MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def committed_memory_mib() -> int:
    status = MemoryStatus(); status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)): return 0
    return int((status.ullTotalPageFile - status.ullAvailPageFile) / 1024 / 1024)


async def monitor_resources(stop: asyncio.Event, peaks: dict) -> None:
    while not stop.is_set():
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True)
        if gpu.returncode == 0 and gpu.stdout.strip():
            memory, utilization, temperature = (int(part.strip()) for part in gpu.stdout.splitlines()[0].split(","))
            peaks["vram_mib"] = max(peaks.get("vram_mib", 0), memory)
            peaks["gpu_percent"] = max(peaks.get("gpu_percent", 0), utilization)
            peaks["temperature_c"] = max(peaks.get("temperature_c", 0), temperature)
        peaks["ram_mib"] = max(peaks.get("ram_mib", 0), int(psutil.virtual_memory().used / 1024 / 1024))
        peaks["commit_mib"] = max(peaks.get("commit_mib", 0), committed_memory_mib())
        try: await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError: pass


async def main() -> None:
    stamp = int(time.time())
    runtime = ROOT / ".runtime" / "phase6"; runtime.mkdir(parents=True, exist_ok=True)
    database = str(runtime / f"phase6-smoke-{stamp}.db")
    output_dir = ROOT / "artifacts" / "phase6" / str(stamp)
    log_path = ROOT / "logs" / "qwen3-tts-phase6.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy(); environment["PYTHONPATH"] = str(ROOT); environment["LOCALDRAMA_QWEN3_TTS_MODEL"] = str(MODEL_PATH)
    process = subprocess.Popen([str(TTS_PYTHON), "-m", "uvicorn", "ai_services.qwen3_tts.service:app", "--host", "127.0.0.1", "--port", "8020"], cwd=ROOT, env=environment, stdout=log_file, stderr=subprocess.STDOUT)
    provider = Qwen3TTSProvider(URL, timeout=900)
    try:
        for _ in range(90):
            try:
                health = await provider.health()
                if health.get("status") == "ONLINE": break
            except (httpx.HTTPError, OSError): pass
            await asyncio.sleep(1)
        else: raise RuntimeError("Qwen3-TTS service did not become healthy")
        create_schema(database)
        with session_scope(database) as session:
            project = Project(name="Phase 6 Smoke", story="雨夜归途"); session.add(project); session.flush()
            character = Character(project_id=project.id, name="林遥", visual_bible_json={}); session.add(character); session.flush()
            reference = Asset(project_id=project.id, kind="AUDIO_REFERENCE", path=str(REFERENCE_PATH), mime_type="audio/wav"); session.add(reference); session.flush()
            voice = VoiceProfile(character_id=character.id, name="林遥声音克隆", model_name="Qwen3-TTS-12Hz-0.6B-Base", language="Chinese", reference_asset_id=reference.id, reference_transcript=REFERENCE_TRANSCRIPT)
            session.add(voice); session.flush()
            scene = Scene(project_id=project.id, order=1, title="雨夜归途", description="林遥在雨中找到方向"); session.add(scene); session.flush()
            shot = Shot(scene_id=scene.id, character_id=character.id, order=1, title="坚定回应", description="林遥看向前方说话", shot_type="DIALOGUE_CLOSEUP", duration=3.0); session.add(shot); session.flush()
            dialogue = Dialogue(shot_id=shot.id, character_id=character.id, order=1, text="别怕，我已经找到回家的路了。", emotion="坚定而温柔"); session.add(dialogue); session.flush()
            project_id, dialogue_id, shot_id = project.id, dialogue.id, shot.id
        stop = asyncio.Event(); peaks = {}; monitor = asyncio.create_task(monitor_resources(stop, peaks))
        try:
            asset = await generate_dialogue_audio(database, project_id, dialogue_id, provider, output_dir)
        finally:
            stop.set(); await monitor
        unload = await provider.unload(); await asyncio.sleep(2)
        info = probe_wav(Path(asset.path))
        with session_scope(database) as session:
            dialogue = session.get(Dialogue, dialogue_id); shot = session.get(Shot, shot_id)
            manifest = session.query(GenerationManifest).filter_by(asset_id=asset.id).one()
            print("AUDIO_ASSET_ID", asset.id); print("AUDIO_PATH", asset.path)
            print("DIALOGUE_DURATION_SECONDS", dialogue.duration); print("SHOT_DURATION_SECONDS", shot.duration)
            print("SAMPLE_RATE", info.sample_rate, "CHANNELS", info.channels, "FRAMES", info.frames)
            print("GENERATION_SECONDS", manifest.generation_time); print("MANIFEST_ID", manifest.id)
            print("PEAK_RESOURCES", json.dumps(peaks, sort_keys=True)); print("UNLOAD", json.dumps(unload, sort_keys=True))
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True)
        print("POST_UNLOAD_GPU", gpu.stdout.strip())
        print("DATABASE", database)
    finally:
        process.terminate()
        try: process.wait(timeout=30)
        except subprocess.TimeoutExpired: process.kill()
        log_file.close()


if __name__ == "__main__": asyncio.run(main())
