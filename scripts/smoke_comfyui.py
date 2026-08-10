import asyncio
import json
import subprocess
import time
from pathlib import Path
import httpx
from app.comfyui.client import ComfyUIClient
from app.db.session import create_schema, session_scope
from app.models import Project
from app.providers.comfyui_image_provider import ComfyUIImageProvider

ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = Path("E:/LocalDramaAI/ComfyUI")
COMFY_PYTHON = Path("E:/LocalDramaAI/env-comfyui/Scripts/python.exe")
DATABASE = "sqlite:///E:/LocalDramaAI/phase3-smoke.db"
URL = "http://127.0.0.1:8188"

async def main():
    log_path = ROOT / "logs" / "comfyui-smoke.log"
    log_path.parent.mkdir(exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen([str(COMFY_PYTHON), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch"], cwd=COMFY_ROOT, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        client = ComfyUIClient(URL, timeout=120)
        for _ in range(90):
            try:
                if await client.health(): break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("ComfyUI did not become healthy")
        create_schema(DATABASE)
        with session_scope(DATABASE) as session:
            project = Project(name="Phase 3 Smoke", story="雨夜快递员发现会说话的包裹")
            session.add(project); session.flush(); project_id = project.id
        workflow = json.loads((ROOT / "comfyui/workflows/sd15_smoke.json").read_text(encoding="utf-8"))
        metadata = {"provider_version": "0.31.0", "model_name": "v1-5-pruned-emaonly-fp16.safetensors", "prompt": workflow["6"]["inputs"]["text"], "negative_prompt": workflow["7"]["inputs"]["text"], "seed": 20260809, "workflow_name": "sd15_smoke", "binding_version": "1"}
        asset = await ComfyUIImageProvider(client).generate_and_register(DATABASE, project_id, workflow, Path("E:/LocalDramaAI/Storage/phase3"), metadata)
        print("ASSET_ID", asset.id)
        print("IMAGE_PATH", asset.path)
        async with httpx.AsyncClient(base_url=URL, timeout=30) as http:
            response = await http.post("/free", json={"unload_models": True, "free_memory": True})
            print("FREE_STATUS", response.status_code)
    finally:
        process.terminate()
        try: process.wait(timeout=20)
        except subprocess.TimeoutExpired: process.kill()
        log_file.close()

if __name__ == "__main__":
    asyncio.run(main())
