"""Real Wan2.2 image-to-video smoke test (3 seconds, 640x368, MP4)."""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import psutil

from app.comfyui.client import ComfyUIClient
from app.comfyui.video_workflow import bind_video_workflow
from app.db.session import create_schema, session_scope
from app.models import Asset, GenerationManifest, Project
from app.providers.comfyui_video_provider import ComfyUIVideoProvider

ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = Path("E:/LocalDramaAI/ComfyUI")
COMFY_PYTHON = Path("E:/LocalDramaAI/env-comfyui/Scripts/python.exe")
URL = "http://127.0.0.1:8188"


async def monitor_resources(stop: asyncio.Event, peaks: dict) -> None:
    while not stop.is_set():
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True,
        )
        if gpu.returncode == 0 and gpu.stdout.strip():
            memory, utilization, temperature = (int(part.strip()) for part in gpu.stdout.splitlines()[0].split(","))
            peaks["vram_mib"] = max(peaks.get("vram_mib", 0), memory)
            peaks["gpu_percent"] = max(peaks.get("gpu_percent", 0), utilization)
            peaks["temperature_c"] = max(peaks.get("temperature_c", 0), temperature)
        peaks["ram_mib"] = max(peaks.get("ram_mib", 0), int(psutil.virtual_memory().used / 1024 / 1024))
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


def latest_storyboard() -> Path:
    candidates = list(Path("E:/LocalDramaAI/Storage/phase4").glob("**/*.png"))
    if not candidates:
        raise FileNotFoundError("No Phase 4 storyboard PNG found under E:/LocalDramaAI/Storage/phase4")
    return max(candidates, key=lambda p: p.stat().st_mtime)


async def main() -> None:
    stamp = int(time.time())
    runtime_root = ROOT / ".runtime" / "phase5"
    input_root = runtime_root / "input"; output_root = runtime_root / "output"
    temp_root = runtime_root / "temp"; user_root = runtime_root / "user"
    for directory in (input_root, output_root, temp_root, user_root): directory.mkdir(parents=True, exist_ok=True)
    database = f"sqlite:///{(runtime_root / f'phase5-smoke-{stamp}.db').as_posix()}"
    storage_root = ROOT / "artifacts" / "phase5" / str(stamp)
    log_path = ROOT / "logs" / "comfyui-phase5.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(COMFY_PYTHON), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch",
         "--extra-model-paths-config", str(ROOT / "runtime/wan22-extra-model-paths.yaml"),
         "--input-directory", str(input_root), "--output-directory", str(output_root),
         "--temp-directory", str(temp_root), "--user-directory", str(user_root),
         "--database-url", "sqlite:///:memory:"],
        cwd=COMFY_ROOT, stdout=log_file, stderr=subprocess.STDOUT,
    )
    client = ComfyUIClient(URL, timeout=180)
    try:
        for _ in range(150):
            try:
                if await client.health():
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("ComfyUI did not become healthy")

        source = latest_storyboard()
        shutil.copy2(source, input_root / "PHASE5_FIRST_FRAME.png")

        create_schema(database)
        with session_scope(database) as session:
            project = Project(name="Phase 5 Smoke", story="Wan2.2 storyboard-to-video MVP")
            session.add(project); session.flush(); project_id = project.id

        workflow = json.loads((ROOT / "comfyui/workflows/wan22_i2v_5b_api.json").read_text(encoding="utf-8"))
        prompt = "cinematic close-up portrait of the same woman, preserve her face, hairstyle and blue clothing, subtle breathing, one gentle blink, a few strands of hair moving slightly, locked camera, coherent natural motion"
        workflow = bind_video_workflow(workflow, "PHASE5_FIRST_FRAME.png", prompt, seed=stamp, width=640, height=368, frames=49)
        metadata = {
            "model_name": "wan2.2_ti2v_5B_fp16",
            "workflow_name": "wan22_i2v_5b_api",
            "provider_version": "ComfyUI 0.31.0",
            "prompt": prompt,
            "negative_prompt": workflow["7"]["inputs"]["text"],
            "seed": stamp,
            "fps": 16,
            "input_assets": [str(source)],
        }
        stop_monitor = asyncio.Event(); peaks = {}; monitor_task = asyncio.create_task(monitor_resources(stop_monitor, peaks))
        try:
            asset = await ComfyUIVideoProvider(client).generate_and_register(database, project_id, workflow, storage_root, metadata)
        finally:
            stop_monitor.set(); await monitor_task
        with session_scope(database) as session:
            manifest = session.query(GenerationManifest).filter_by(asset_id=asset.id).one()
            registered_path = session.get(Asset, asset.id).path
            print("VIDEO_ASSET_ID", asset.id)
            print("VIDEO_PATH", registered_path)
            print("VIDEO_MANIFEST_ID", manifest.id)
            print("GENERATION_SECONDS", manifest.generation_time)
            print("ASSET_COUNT", session.query(Asset).count())
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", registered_path], capture_output=True, text=True, check=True)
        print("VIDEO_DURATION_SECONDS", probe.stdout.strip())
        print("PEAK_RESOURCES", json.dumps(peaks, sort_keys=True))
        async with httpx.AsyncClient(base_url=URL, timeout=30) as http:
            print("FREE_STATUS", (await http.post("/free", json={"unload_models": True, "free_memory": True})).status_code)
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()


if __name__ == "__main__":
    asyncio.run(main())
