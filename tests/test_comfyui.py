import pytest
from app.comfyui.workflow_binding import validate_workflow, bind_workflow
from app.providers.comfyui_image_provider import ComfyUIImageProvider
from app.db.session import create_schema, session_scope
from app.models import Project, Asset, GenerationManifest


def test_workflow_binding_replaces_inputs_and_validates():
    workflow = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "MODEL"}},
                "2": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}}
    bound = bind_workflow(workflow, {"MODEL": "sdxl.safetensors", "width": 640, "height": 360})
    validate_workflow(bound)
    assert bound["1"]["inputs"]["ckpt_name"] == "sdxl.safetensors"
    assert bound["2"]["inputs"]["width"] == 640


def test_invalid_workflow_is_rejected():
    with pytest.raises(ValueError):
        validate_workflow({"1": {"inputs": {}}})

@pytest.mark.anyio
async def test_real_output_is_registered_as_asset_and_manifest(tmp_path):
    class FakeClient:
        async def submit_prompt(self, workflow): return "prompt-1"
        async def wait_for_completion(self, prompt_id):
            return {"outputs": {"9": {"images": [{"filename": "real.png", "subfolder": ""}]}}}
        async def download_output(self, filename, subfolder, output_dir):
            path = output_dir / filename; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"PNG"); return path
    db = tmp_path / "db.sqlite"; create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Image"); session.add(project); session.flush(); project_id = project.id
    provider = ComfyUIImageProvider(FakeClient())
    workflow = {"9": {"class_type":"SaveImage", "inputs":{}}}
    asset = await provider.generate_and_register(str(db), project_id, workflow, tmp_path / "outputs", {"model_name":"sd15", "workflow_name":"smoke"})
    with session_scope(str(db)) as session:
        assert session.get(Asset, asset.id).path.endswith("real.png")
        assert session.query(GenerationManifest).filter_by(asset_id=asset.id).one().output_asset == asset.id
