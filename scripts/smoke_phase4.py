import asyncio, json, subprocess, time, shutil
from pathlib import Path
import httpx
from app.comfyui.client import ComfyUIClient
from app.db.session import create_schema, session_scope
from app.models import Project, Character, Scene, Shot, Asset, CharacterReference
from app.providers.comfyui_image_provider import ComfyUIImageProvider
from app.schemas.character import VisualBible
from app.services.phase4_pipeline import generate_character_master, generate_storyboard

ROOT = Path(__file__).resolve().parents[1]
COMFY_ROOT = Path("E:/LocalDramaAI/ComfyUI"); COMFY_PYTHON = Path("E:/LocalDramaAI/env-comfyui/Scripts/python.exe")
URL = "http://127.0.0.1:8188"

async def main():
    database = f"sqlite:///E:/LocalDramaAI/phase4-smoke-{int(time.time())}.db"
    storage_root = Path(f"E:/LocalDramaAI/Storage/phase4/{int(time.time())}")
    log_file = (ROOT / "logs" / "comfyui-phase4.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(COMFY_PYTHON), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch"], cwd=COMFY_ROOT, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        client = ComfyUIClient(URL, timeout=120)
        for _ in range(90):
            try:
                if await client.health(): break
            except httpx.HTTPError: pass
            await asyncio.sleep(1)
        else: raise RuntimeError("ComfyUI did not become healthy")
        create_schema(database)
        with session_scope(database) as session:
            project = Project(name="Phase 4 Smoke", story="雨夜快递员与会说话的包裹"); session.add(project); session.flush()
            bible = VisualBible(name="林遥", age="30岁", gender="女", face="椭圆脸，清晰下颌线", eyes="杏眼", nose="小巧直鼻", mouth="自然薄唇", hair="黑色短发", body="纤细", clothes="蓝色夹克和白色衬衫", accessories="银色耳钉", visual_style="写实电影感")
            character = Character(project_id=project.id, name=bible.name, visual_bible_json=bible.model_dump(), negative_prompt="年龄变化，发色变化，服装变化"); session.add(character); session.flush()
            scene = Scene(project_id=project.id, order=1, title="雨夜街口", description="橙色路灯下的湿街道"); session.add(scene); session.flush()
            shots = [Shot(scene_id=scene.id, character_id=character.id, order=1, title="发现包裹", description="林遥站在雨夜路灯下，低头发现一个发光包裹"), Shot(scene_id=scene.id, character_id=character.id, order=2, title="抬头反应", description="林遥抬头望向远处，手里仍然保持蓝色夹克和包裹")]
            session.add_all(shots); session.flush(); project_id, character_id, shot_ids = project.id, character.id, [s.id for s in shots]
        master_workflow = json.loads((ROOT / "comfyui/workflows/phase4_character_storyboard.json").read_text(encoding="utf-8"))
        provider = ComfyUIImageProvider(client)
        reference = await generate_character_master(database, project_id, character_id, provider, master_workflow, storage_root / "characters")
        with session_scope(database) as session: master_path = session.get(Asset, reference.asset_id).path
        input_dir = COMFY_ROOT / "input"; input_dir.mkdir(parents=True, exist_ok=True); shutil.copy2(master_path, input_dir / "phase4_master.png")
        storyboard_workflow = json.loads((ROOT / "comfyui/workflows/phase4_storyboard_img2img.json").read_text(encoding="utf-8"))
        assets = []
        for shot_id in shot_ids:
            assets.append(await generate_storyboard(database, project_id, shot_id, provider, storyboard_workflow, storage_root / "storyboards", "phase4_master.png"))
        with session_scope(database) as session:
            paths = [session.get(Asset, reference.asset_id).path, *[session.get(Asset, a.id).path for a in assets]]
            print("CHARACTER_REFERENCE_ID", reference.id); print("IMAGE_PATHS", *paths, sep="\n")
            print("REFERENCE_COUNT", session.query(CharacterReference).count(), "STORYBOARD_COUNT", len(assets))
        async with httpx.AsyncClient(base_url=URL, timeout=30) as http: print("FREE_STATUS", (await http.post("/free", json={"unload_models":True,"free_memory":True})).status_code)
    finally:
        process.terminate()
        try: process.wait(timeout=20)
        except subprocess.TimeoutExpired: process.kill()
        log_file.close()

if __name__ == "__main__": asyncio.run(main())
