import pytest
from app.schemas.character import VisualBible, CharacterCreate
from app.services.character_generation import build_master_prompt, build_identity_prompt
from app.services.storyboard_generation import build_storyboard_prompt, derive_shot_seed
from app.db.session import create_schema, session_scope
from app.models import Project, Character, Scene, Shot
from app.models import Asset, CharacterReference
from app.services.phase4_pipeline import generate_character_master, generate_storyboard


def test_visual_bible_normalizes_identity_and_locks_forbidden_changes():
    bible = VisualBible(name="  林  ", age="30岁", gender="女", face=" oval face ", eyes="杏眼", hair="黑色短发", body="纤细", clothes="蓝色夹克", visual_style="写实")
    assert bible.name == "林"
    assert "face structure" in bible.forbidden_changes
    assert "age" in bible.forbidden_changes
    prompt = build_master_prompt(bible)
    assert "林" in prompt and "蓝色夹克" in prompt and "禁止改变" in prompt


def test_storyboard_prompt_reuses_exact_identity_and_seed_is_stable():
    bible = VisualBible(name="林", age="30岁", gender="女", face="椭圆脸", eyes="杏眼", hair="黑色短发", body="纤细", clothes="蓝色夹克", visual_style="写实")
    prompt = build_storyboard_prompt(bible, "雨夜站在路灯下，手持包裹")
    assert build_identity_prompt(bible) in prompt
    assert derive_shot_seed("character-1", 1) == derive_shot_seed("character-1", 1)
    assert derive_shot_seed("character-1", 1) != derive_shot_seed("character-1", 2)


def test_phase4_domain_persists_character_scene_and_shot(tmp_path):
    db = tmp_path / "phase4.db"; create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Phase 4"); session.add(project); session.flush()
        bible = VisualBible(name="林", age="30岁", gender="女", face="椭圆脸", eyes="杏眼", hair="黑色短发", body="纤细", clothes="蓝色夹克", visual_style="写实")
        character = Character(project_id=project.id, name=bible.name, visual_bible_json=bible.model_dump())
        session.add(character); session.flush()
        scene = Scene(project_id=project.id, order=1, title="雨夜", description="路灯下")
        session.add(scene); session.flush()
        shot = Shot(scene_id=scene.id, character_id=character.id, order=1, title="路灯", description="人物手持包裹")
        session.add(shot); session.flush(); shot_id = shot.id
    with session_scope(str(db)) as session:
        assert session.get(Shot, shot_id).scene.project_id == project.id

@pytest.mark.anyio
async def test_phase4_generation_registers_master_and_storyboard(tmp_path):
    db = tmp_path / "phase4.db"; create_schema(str(db))
    with session_scope(str(db)) as session:
        project = Project(name="Phase 4"); session.add(project); session.flush()
        bible = VisualBible(name="林", age="30岁", gender="女", face="椭圆脸", eyes="杏眼", hair="黑色短发", body="纤细", clothes="蓝色夹克", visual_style="写实")
        character = Character(project_id=project.id, name=bible.name, visual_bible_json=bible.model_dump()); session.add(character); session.flush()
        scene = Scene(project_id=project.id, order=1, title="雨夜", description="路灯下"); session.add(scene); session.flush()
        shot = Shot(scene_id=scene.id, character_id=character.id, order=1, title="路灯", description="人物手持包裹"); session.add(shot); session.flush()
        project_id, character_id, shot_id = project.id, character.id, shot.id
    class FakeProvider:
        def __init__(self): self.prompts = []
        async def generate_and_register(self, database_url, project_id, workflow, output_dir, metadata):
            self.prompts.append(metadata["prompt"])
            with session_scope(database_url) as session:
                asset = Asset(project_id=project_id, kind="IMAGE", path=str(output_dir / f"{len(self.prompts)}.png"), mime_type="image/png", metadata_json={})
                session.add(asset); session.flush(); return asset
    provider = FakeProvider()
    ref = await generate_character_master(str(db), project_id, character_id, provider, {}, tmp_path / "chars")
    storyboard = await generate_storyboard(str(db), project_id, shot_id, provider, {}, tmp_path / "storyboards")
    assert ref.type == "MASTER" and storyboard.kind == "IMAGE"
    with session_scope(str(db)) as session:
        assert session.query(CharacterReference).filter_by(character_id=character_id).count() == 1
        assert session.get(Shot, shot_id).storyboard_asset_id == storyboard.id
