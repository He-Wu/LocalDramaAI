import json, time
from pathlib import Path
from app.comfyui.client import ComfyUIClient
from app.providers.ffmpeg_provider import FFmpegProvider

VIDEO_EXTENSIONS = {".webm", ".mp4", ".mkv", ".webp", ".mov"}

def find_video_output(history: dict):
    for node in history.get("outputs", {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node.get(key, []) if isinstance(node, dict) else []:
                if str(item.get("filename", "")).lower().endswith(tuple(VIDEO_EXTENSIONS)):
                    return item
    raise RuntimeError("ComfyUI completed without a video output")

class ComfyUIVideoProvider:
    def __init__(self, client: ComfyUIClient, ffmpeg: FFmpegProvider | None = None): self.client = client; self.ffmpeg = ffmpeg or FFmpegProvider()

    async def generate(self, workflow: dict, output_dir: Path, metadata: dict):
        started = time.perf_counter(); prompt_id = await self.client.submit_prompt(workflow); history = await self.client.wait_for_completion(prompt_id)
        result = find_video_output(history)
        source = await self.client.download_output(result["filename"], result.get("subfolder", ""), output_dir)
        mp4 = output_dir / f"{source.stem}.mp4"
        self.ffmpeg.to_mp4(source, mp4, fps=int(metadata.get("fps", 16)))
        manifest = {**metadata, "provider":"comfyui", "generation_time":time.perf_counter()-started, "output_asset":str(mp4), "source_output":str(source)}
        manifest_path = output_dir / f"{mp4.stem}.manifest.json"; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return mp4, manifest_path

    async def generate_and_register(self, database_url: str, project_id: str, workflow: dict, output_dir: Path, metadata: dict):
        """Generate a video, then persist the video Asset and immutable manifest."""
        import hashlib
        from app.db.session import session_scope
        from app.models import Asset, GenerationManifest

        path, manifest_path = await self.generate(workflow, output_dir, metadata)
        generation_record = json.loads(manifest_path.read_text(encoding="utf-8"))
        workflow_hash = hashlib.sha256(json.dumps(workflow, sort_keys=True).encode()).hexdigest()
        with session_scope(database_url) as session:
            asset = Asset(project_id=project_id, kind="VIDEO", path=str(path), mime_type="video/mp4",
                          metadata_json={"manifest_path": str(manifest_path), "source_output": generation_record.get("source_output")})
            session.add(asset); session.flush()
            record = GenerationManifest(
                asset_id=asset.id, provider="comfyui", provider_version=metadata.get("provider_version"),
                model_name=metadata.get("model_name"), prompt=metadata.get("prompt"),
                negative_prompt=metadata.get("negative_prompt"), seed=metadata.get("seed"),
                workflow_name=metadata.get("workflow_name"), workflow_hash=workflow_hash,
                binding_version=metadata.get("binding_version", "1"),
                generation_time=generation_record.get("generation_time"),
                input_assets=metadata.get("input_assets", []), output_asset=asset.id)
            session.add(record); session.flush()
            return asset
