"""Real PHASE 7 smoke: Chinese dialogue -> TTS duration -> approved storyboard -> Wan2.2 I2V."""
import asyncio
import ctypes
import json
import os
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import httpx
import psutil

from app.comfyui.client import ComfyUIClient
from app.db.session import create_schema, session_scope
from app.models import Asset, Character, Dialogue, GenerationManifest, Project, Scene, Shot, VoiceProfile
from app.providers.comfyui_video_provider import ComfyUIVideoProvider
from app.providers.qwen3_tts_provider import Qwen3TTSProvider
from app.services.dialogue_video_generation import generate_dialogue_video
from app.services.tts_generation import generate_dialogue_audio

ROOT = Path(__file__).resolve().parents[1]
TTS_PYTHON = Path("E:/LocalDramaAI/env-tts/Scripts/python.exe")
TTS_MODEL = Path("E:/LocalDramaAI/Models/Qwen3-TTS-12Hz-0.6B-Base")
REFERENCE_PATH = Path("E:/LocalDramaAI/Storage/shared/voices/qwen3_clone_reference.wav")
REFERENCE_TRANSCRIPT = "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you."
COMFY_ROOT = Path("E:/LocalDramaAI/ComfyUI")
COMFY_PYTHON = Path("E:/LocalDramaAI/env-comfyui/Scripts/python.exe")
TTS_URL = "http://127.0.0.1:8020"
COMFY_URL = "http://127.0.0.1:8188"


def validate_smoke_evidence(
    probe_data: dict,
    *,
    dialogue_duration: float,
    shot_duration: float,
    shot_video_asset_id: str | None,
    video_asset_id: str,
    manifest_input_assets: list,
    expected_input_assets: list,
    manifest_output_asset: str | None,
    free_status: int,
) -> None:
    try:
        stream = probe_data["streams"][0]
        duration = float(probe_data["format"]["duration"])
        size = int(probe_data["format"]["size"])
        fps = float(Fraction(stream["r_frame_rate"]))
        frames = int(stream["nb_frames"])
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError("PHASE 7 ffprobe evidence is incomplete") from exc
    if stream["codec_name"] != "h264" or (int(stream["width"]), int(stream["height"])) != (640, 368):
        raise RuntimeError("PHASE 7 video codec or dimensions do not match the locked profile")
    if abs(fps - 16) > 0.01 or frames < 1 or size < 1:
        raise RuntimeError("PHASE 7 video FPS, frame count, or size is invalid")
    if duration + (1 / 16) < shot_duration:
        raise RuntimeError(f"PHASE 7 video is shorter than the Shot: {duration:.4f}s < {shot_duration:.4f}s")
    if shot_duration + 0.001 < dialogue_duration + 0.3:
        raise RuntimeError("PHASE 7 Shot duration does not include the dialogue tail buffer")
    if shot_video_asset_id != video_asset_id or manifest_output_asset != video_asset_id:
        raise RuntimeError("PHASE 7 Shot, manifest, and VIDEO Asset links disagree")
    if manifest_input_assets != expected_input_assets:
        raise RuntimeError("PHASE 7 manifest does not contain the expected storyboard and audio inputs")
    if free_status != 200:
        raise RuntimeError(f"ComfyUI model release request failed with HTTP {free_status}")


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def committed_memory_mib() -> int:
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int((status.ullTotalPageFile - status.ullAvailPageFile) / 1024 / 1024)


async def monitor_resources(stop: asyncio.Event, peaks: dict) -> None:
    while not stop.is_set():
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
        )
        if gpu.returncode == 0 and gpu.stdout.strip():
            memory, utilization, temperature = (int(value.strip()) for value in gpu.stdout.splitlines()[0].split(","))
            peaks["vram_mib"] = max(peaks.get("vram_mib", 0), memory)
            peaks["gpu_percent"] = max(peaks.get("gpu_percent", 0), utilization)
            peaks["temperature_c"] = max(peaks.get("temperature_c", 0), temperature)
        peaks["ram_mib"] = max(peaks.get("ram_mib", 0), int(psutil.virtual_memory().used / 1024 / 1024))
        peaks["commit_mib"] = max(peaks.get("commit_mib", 0), committed_memory_mib())
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


def latest_storyboard() -> Path:
    candidates = list(Path("E:/LocalDramaAI/Storage/phase4").glob("**/storyboards/*.png"))
    if not candidates:
        raise FileNotFoundError("No Phase 4 storyboard PNG found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def wait_for_tts(provider: Qwen3TTSProvider) -> None:
    for _ in range(90):
        try:
            if (await provider.health()).get("status") == "ONLINE":
                return
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(1)
    raise RuntimeError("Qwen3-TTS service did not become healthy")


async def wait_for_comfy(client: ComfyUIClient) -> None:
    for _ in range(150):
        try:
            if await client.health():
                return
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(1)
    raise RuntimeError("ComfyUI did not become healthy")


async def main() -> None:
    stamp = int(time.time())
    runtime = ROOT / ".runtime" / "phase7"
    input_dir = runtime / "input"
    output_dir = runtime / "output"
    temp_dir = runtime / "temp"
    user_dir = runtime / "user"
    for directory in (input_dir, output_dir, temp_dir, user_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = str(runtime / f"phase7-smoke-{stamp}.db")
    artifact_dir = ROOT / "artifacts" / "phase7" / str(stamp)
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    storyboard_path = latest_storyboard()

    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Phase 7 Smoke", story="雨夜归途")
        session.add(project)
        session.flush()
        character = Character(project_id=project.id, name="林遥", visual_bible_json={})
        session.add(character)
        session.flush()
        reference = Asset(project_id=project.id, kind="AUDIO_REFERENCE", path=str(REFERENCE_PATH), mime_type="audio/wav")
        storyboard = Asset(project_id=project.id, kind="IMAGE", path=str(storyboard_path), mime_type="image/png")
        session.add_all([reference, storyboard])
        session.flush()
        session.add(VoiceProfile(
            character_id=character.id,
            name="林遥声音克隆",
            model_name="Qwen3-TTS-12Hz-0.6B-Base",
            language="Chinese",
            reference_asset_id=reference.id,
            reference_transcript=REFERENCE_TRANSCRIPT,
        ))
        scene = Scene(project_id=project.id, order=1, title="雨夜归途", description="林遥在雨中找到方向")
        session.add(scene)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            character_id=character.id,
            order=1,
            title="坚定回应",
            description="cinematic close-up portrait of the same woman, preserve her face, hairstyle and blue clothing, subtle speaking motion, gentle blink, locked camera",
            shot_type="DIALOGUE_CLOSEUP",
            duration=3.0,
            storyboard_asset_id=storyboard.id,
            status="STORYBOARD_GENERATED",
        )
        session.add(shot)
        session.flush()
        dialogue = Dialogue(
            shot_id=shot.id,
            character_id=character.id,
            order=1,
            text="别怕，我已经找到回家的路了。",
            emotion="坚定而温柔",
        )
        session.add(dialogue)
        session.flush()
        project_id, shot_id, dialogue_id, storyboard_id = project.id, shot.id, dialogue.id, storyboard.id

    tts_process = None
    comfy_process = None
    tts_log = (log_dir / "qwen3-tts-phase7.log").open("w", encoding="utf-8")
    comfy_log = (log_dir / "comfyui-phase7.log").open("w", encoding="utf-8")
    stop = asyncio.Event()
    peaks = {}
    monitor = asyncio.create_task(monitor_resources(stop, peaks))
    try:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        environment["LOCALDRAMA_QWEN3_TTS_MODEL"] = str(TTS_MODEL)
        tts_process = subprocess.Popen(
            [str(TTS_PYTHON), "-m", "uvicorn", "ai_services.qwen3_tts.service:app", "--host", "127.0.0.1", "--port", "8020"],
            cwd=ROOT,
            env=environment,
            stdout=tts_log,
            stderr=subprocess.STDOUT,
        )
        tts_provider = Qwen3TTSProvider(TTS_URL, timeout=900)
        await wait_for_tts(tts_provider)
        audio_asset = await generate_dialogue_audio(database, project_id, dialogue_id, tts_provider, artifact_dir / "audio")
        unload_result = await tts_provider.unload()
        await asyncio.sleep(2)
        post_unload_health = await tts_provider.health()
        if post_unload_health.get("loaded"):
            raise RuntimeError("Qwen3-TTS model remained loaded before Wan generation")
        stop_process(tts_process)
        tts_process = None
        await asyncio.sleep(2)

        comfy_process = subprocess.Popen(
            [
                str(COMFY_PYTHON), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch",
                "--extra-model-paths-config", str(ROOT / "runtime/wan22-extra-model-paths.yaml"),
                "--input-directory", str(input_dir), "--output-directory", str(output_dir),
                "--temp-directory", str(temp_dir), "--user-directory", str(user_dir),
                "--database-url", "sqlite:///:memory:",
            ],
            cwd=COMFY_ROOT,
            stdout=comfy_log,
            stderr=subprocess.STDOUT,
        )
        client = ComfyUIClient(COMFY_URL, timeout=180)
        await wait_for_comfy(client)
        workflow = json.loads((ROOT / "comfyui/workflows/wan22_i2v_5b_api.json").read_text(encoding="utf-8"))
        video_asset = await generate_dialogue_video(
            database,
            project_id,
            shot_id,
            ComfyUIVideoProvider(client),
            workflow,
            input_dir,
            artifact_dir / "video",
            seed=stamp,
        )
        async with httpx.AsyncClient(base_url=COMFY_URL, timeout=30) as http:
            free_status = (await http.post("/free", json={"unload_models": True, "free_memory": True})).status_code

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration,size",
                "-of", "json", video_asset.path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        probe_data = json.loads(probe.stdout)
        with session_scope(database) as session:
            shot = session.get(Shot, shot_id)
            dialogue = session.get(Dialogue, dialogue_id)
            manifest = session.query(GenerationManifest).filter_by(asset_id=video_asset.id).one()
            validate_smoke_evidence(
                probe_data,
                dialogue_duration=dialogue.duration,
                shot_duration=shot.duration,
                shot_video_asset_id=shot.video_asset_id,
                video_asset_id=video_asset.id,
                manifest_input_assets=manifest.input_assets,
                expected_input_assets=[storyboard_id, audio_asset.id],
                manifest_output_asset=manifest.output_asset,
                free_status=free_status,
            )
            print("AUDIO_PATH", audio_asset.path)
            print("AUDIO_DURATION_SECONDS", dialogue.duration)
            print("SHOT_DURATION_SECONDS", shot.duration)
            print("STORYBOARD_PATH", storyboard_path)
            print("VIDEO_ASSET_ID", video_asset.id)
            print("VIDEO_PATH", video_asset.path)
            print("VIDEO_MANIFEST_ID", manifest.id)
            print("VIDEO_GENERATION_SECONDS", manifest.generation_time)
            print("VIDEO_INPUT_ASSETS", json.dumps(manifest.input_assets))
            print("SHOT_VIDEO_ASSET_ID", shot.video_asset_id)
        print("VIDEO_PROBE", json.dumps(probe_data, sort_keys=True))
        print("TTS_UNLOAD", json.dumps(unload_result, sort_keys=True))
        print("TTS_POST_UNLOAD", json.dumps(post_unload_health, sort_keys=True))
        print("COMFY_FREE_STATUS", free_status)
        print("PEAK_RESOURCES", json.dumps(peaks, sort_keys=True))
        print("DATABASE", database)
    finally:
        stop.set()
        await monitor
        stop_process(comfy_process)
        stop_process(tts_process)
        tts_log.close()
        comfy_log.close()


if __name__ == "__main__":
    asyncio.run(main())
