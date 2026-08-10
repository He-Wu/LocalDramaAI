import copy
from pathlib import Path
from app.db.session import session_scope
from app.models import Character, CharacterReference, Shot, Asset
from app.schemas.character import VisualBible
from app.services.character_generation import build_master_prompt, derive_character_seed
from app.services.storyboard_generation import build_storyboard_prompt, derive_shot_seed

def _bind_prompt(workflow: dict, prompt: str, seed: int, prefix: str, reference_image_name: str | None = None):
    bound = copy.deepcopy(workflow)
    for node in bound.values():
        inputs = node.get("inputs", {})
        if node.get("class_type") == "CLIPTextEncode" and inputs.get("text") != "low quality, blurry, text, watermark":
            inputs["text"] = prompt
        if node.get("class_type") == "KSampler": inputs["seed"] = seed
        if node.get("class_type") == "SaveImage": inputs["filename_prefix"] = prefix
        if node.get("class_type") == "LoadImage" and reference_image_name: inputs["image"] = reference_image_name
    return bound

async def generate_character_master(database_url: str, project_id: str, character_id: str, provider, workflow: dict, output_dir: Path):
    with session_scope(database_url) as session:
        character = session.get(Character, character_id)
        if not character or character.project_id != project_id: raise ValueError("character does not belong to project")
        bible = VisualBible.model_validate(character.visual_bible_json)
    prompt = build_master_prompt(bible); seed = derive_character_seed(character_id)
    bound = _bind_prompt(workflow, prompt, seed, f"LocalDramaAI/characters/{character_id}/master")
    asset = await provider.generate_and_register(database_url, project_id, bound, output_dir, {"model_name":"sd15-ema-fp16", "prompt":prompt, "seed":seed, "workflow_name":"phase4_character_master"})
    with session_scope(database_url) as session:
        reference = CharacterReference(character_id=character_id, asset_id=asset.id, type="MASTER", prompt=prompt, seed=seed, model="sd15-ema-fp16", workflow="phase4_character_master")
        session.add(reference); session.flush(); session.get(Character, character_id).primary_reference_id = reference.id
        return reference

async def generate_storyboard(database_url: str, project_id: str, shot_id: str, provider, workflow: dict, output_dir: Path, reference_image_name: str | None = None):
    with session_scope(database_url) as session:
        shot = session.get(Shot, shot_id)
        if not shot or not shot.scene or shot.scene.project_id != project_id: raise ValueError("shot does not belong to project")
        if not shot.character_id: raise ValueError("storyboard shot requires a character_id in Phase 4")
        character = session.get(Character, shot.character_id); bible = VisualBible.model_validate(character.visual_bible_json)
        character_id = character.id; description = shot.description; order = shot.order
    prompt = build_storyboard_prompt(bible, description); seed = derive_shot_seed(character_id, order)
    bound = _bind_prompt(workflow, prompt, seed, f"LocalDramaAI/storyboards/{shot_id}", reference_image_name)
    asset = await provider.generate_and_register(database_url, project_id, bound, output_dir, {"model_name":"sd15-ema-fp16", "prompt":prompt, "seed":seed, "workflow_name":"phase4_storyboard", "input_assets":[reference_image_name] if reference_image_name else []})
    with session_scope(database_url) as session:
        db_shot = session.get(Shot, shot_id); db_shot.storyboard_asset_id = asset.id; db_shot.image_prompt = prompt; db_shot.status = "STORYBOARD_GENERATED"
        return asset
