import hashlib, json, time
from pathlib import Path
from app.comfyui.client import ComfyUIClient
from app.comfyui.workflow_binding import validate_workflow

class ComfyUIImageProvider:
    def __init__(self, client: ComfyUIClient): self.client = client
    async def generate(self, workflow: dict, output_dir: Path, metadata: dict):
        validate_workflow(workflow); started = time.perf_counter(); prompt_id = await self.client.submit_prompt(workflow)
        history = await self.client.wait_for_completion(prompt_id)
        outputs = history.get("outputs", {})
        first = next((item for node in outputs.values() for item in node.get("images", [])), None)
        if not first: raise RuntimeError("ComfyUI completed without an image output")
        path = await self.client.download_output(first["filename"], first.get("subfolder", ""), output_dir)
        manifest = {**metadata, "provider": "comfyui", "generation_time": time.perf_counter() - started, "output_asset": str(path)}
        manifest_path = output_dir / f"{path.stem}.manifest.json"; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path, manifest_path

    async def generate_and_register(self, database_url: str, project_id: str, workflow: dict, output_dir: Path, metadata: dict):
        from app.db.session import session_scope
        from app.models import Asset, GenerationManifest
        path, manifest_path = await self.generate(workflow, output_dir, metadata)
        generation_record = json.loads(manifest_path.read_text(encoding="utf-8"))
        workflow_hash = hashlib.sha256(json.dumps(workflow, sort_keys=True).encode()).hexdigest()
        with session_scope(database_url) as session:
            asset = Asset(project_id=project_id, kind="IMAGE", path=str(path), mime_type="image/png", metadata_json={"manifest_path": str(manifest_path)})
            session.add(asset); session.flush()
            record = GenerationManifest(asset_id=asset.id, provider="comfyui", provider_version=metadata.get("provider_version"),
                model_name=metadata.get("model_name"), prompt=metadata.get("prompt"), negative_prompt=metadata.get("negative_prompt"),
                seed=metadata.get("seed"), workflow_name=metadata.get("workflow_name"), workflow_hash=workflow_hash,
                binding_version=metadata.get("binding_version", "1"), generation_time=generation_record.get("generation_time"),
                input_assets=metadata.get("input_assets", []), output_asset=asset.id)
            session.add(record); session.flush()
            return asset
