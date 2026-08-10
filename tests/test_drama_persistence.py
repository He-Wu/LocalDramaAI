import pytest

from app.db.session import create_schema, session_scope
from app.models import Character, Dialogue, Project, Scene, Shot
from app.schemas.drama import StructuredDrama


def test_replace_project_drama_maps_scene_character_shot_and_dialogue(tmp_path):
    database = str(tmp_path / "drama.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="旧标题", story="雨夜重逢")
        session.add(project)
        session.flush()
        project_id = project.id

    drama = StructuredDrama.model_validate({
        "title": "雨夜重逢",
        "characters": [{
            "name": "林遥", "age": "30岁", "gender": "女", "face": "椭圆脸",
            "eyes": "杏眼", "hair": "黑色短发", "body": "纤细", "clothes": "蓝色夹克",
            "visual_style": "写实", "personality": "冷静",
        }],
        "scenes": [{
            "order": 1,
            "title": "雨夜",
            "description": "路灯下",
            "estimated_duration": 4.5,
        }],
        "shots": [{
            "order": 1, "scene_order": 1, "character_name": "林遥", "title": "近景",
            "description": "林遥抬头", "video_prompt": "subtle blink",
        }],
        "dialogues": [{"shot_order": 1, "character_name": "林遥", "text": "我回来了。"}],
    })
    assert drama.characters[0].age == "30岁"
    assert drama.shots[0].scene_order == 1

    from app.services.drama_persistence import replace_project_drama

    counts = replace_project_drama(database, project_id, drama)

    assert counts == {"characters": 1, "scenes": 1, "shots": 1, "dialogues": 1}
    with session_scope(database) as session:
        project = session.get(Project, project_id)
        character = session.query(Character).one()
        scene = session.query(Scene).one()
        shot = session.query(Shot).one()
        dialogue = session.query(Dialogue).one()
        assert project.name == "雨夜重逢"
        assert project.status == "STRUCTURED"
        assert character.visual_bible_json["clothes"] == "蓝色夹克"
        assert scene.estimated_duration == 4.5
        assert shot.scene_id == scene.id and shot.character_id == character.id
        assert dialogue.shot_id == shot.id and dialogue.character_id == character.id


def test_replace_project_drama_groups_dialogues_by_shot_with_stable_local_order(tmp_path):
    database = str(tmp_path / "dialogue-order.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="对白排序")
        session.add(project)
        session.flush()
        project_id = project.id

    drama = StructuredDrama.model_validate({
        "title": "对白排序",
        "characters": [{"name": "林遥"}],
        "scenes": [{"order": 1, "title": "雨夜", "description": "路灯下"}],
        "shots": [
            {"order": 1, "scene_order": 1, "title": "近景", "description": "抬头"},
            {"order": 2, "scene_order": 1, "title": "远景", "description": "雨幕"},
        ],
        "dialogues": [
            {"shot_order": 2, "text": "第二镜第一句"},
            {"shot_order": 1, "text": "第一镜第一句"},
            {"shot_order": 2, "text": "第二镜第二句"},
        ],
    })

    from app.services.drama_persistence import replace_project_drama

    replace_project_drama(database, project_id, drama)

    with session_scope(database) as session:
        shots = {shot.order: shot for shot in session.query(Shot).all()}
        assert [(item.order, item.text) for item in shots[1].dialogues] == [
            (1, "第一镜第一句"),
        ]
        assert [(item.order, item.text) for item in shots[2].dialogues] == [
            (1, "第二镜第一句"),
            (2, "第二镜第二句"),
        ]


@pytest.mark.parametrize(
    ("duplicate_kind", "expected_error"),
    [
        ("character", "duplicate character name: 林遥"),
        ("scene", "duplicate scene order: 1"),
        ("shot", "duplicate shot order: 1"),
    ],
)
def test_replace_project_drama_rejects_duplicate_reference_keys(
    tmp_path,
    duplicate_kind,
    expected_error,
):
    database = str(tmp_path / f"duplicate-{duplicate_kind}.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="旧标题")
        session.add(project)
        session.flush()
        project_id = project.id

    data = {
        "title": "雨夜重逢",
        "characters": [{"name": "林遥"}],
        "scenes": [{"order": 1, "title": "雨夜", "description": "路灯下"}],
        "shots": [{
            "order": 1,
            "scene_order": 1,
            "title": "近景",
            "description": "林遥抬头",
        }],
        "dialogues": [{"shot_order": 1, "text": "我回来了。"}],
    }
    if duplicate_kind == "character":
        data["characters"].append({"name": "林遥"})
    elif duplicate_kind == "scene":
        data["scenes"].append({"order": 1, "title": "室内", "description": "门边"})
    else:
        data["shots"].append({
            "order": 1,
            "scene_order": 1,
            "title": "远景",
            "description": "雨幕",
        })

    from app.services.drama_persistence import replace_project_drama

    with pytest.raises(ValueError, match=expected_error):
        replace_project_drama(database, project_id, StructuredDrama.model_validate(data))
