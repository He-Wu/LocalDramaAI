from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import event

from app.db.session import create_schema, get_engine, session_scope
from app.models import (
    Asset,
    Character,
    CharacterReference,
    Dialogue,
    Project,
    Scene,
    Shot,
    VoiceProfile,
)
from app.schemas.drama import StructuredDrama


def _seed_existing_graph(database):
    with session_scope(database) as session:
        project = Project(name="旧标题", status="STRUCTURED")
        session.add(project)
        session.flush()
        character = Character(
            project_id=project.id,
            name="旧角色",
            visual_bible_json={},
        )
        asset = Asset(project_id=project.id, kind="IMAGE", path="old.png")
        session.add_all([character, asset])
        session.flush()
        reference = CharacterReference(
            character_id=character.id,
            asset_id=asset.id,
            prompt="旧参考图",
            seed=7,
        )
        voice = VoiceProfile(character_id=character.id, name="旧音色")
        scene = Scene(
            project_id=project.id,
            order=1,
            title="旧场景",
            description="旧描述",
        )
        session.add_all([reference, voice, scene])
        session.flush()
        shot = Shot(scene_id=scene.id, character_id=character.id, order=1,
                    title="旧镜头", description="旧镜头描述")
        session.add(shot)
        session.flush()
        dialogue = Dialogue(
            shot_id=shot.id,
            character_id=character.id,
            order=1,
            text="旧对白",
        )
        session.add(dialogue)
        session.flush()
        return {
            "project": project.id,
            "character": character.id,
            "reference": reference.id,
            "voice": voice.id,
            "scene": scene.id,
            "shot": shot.id,
            "dialogue": dialogue.id,
        }


def _replacement_data(title="新标题"):
    return {
        "title": title,
        "characters": [{"name": f"{title}角色"}],
        "scenes": [{"order": 1, "title": f"{title}场景", "description": "新描述"}],
        "shots": [{
            "order": 1,
            "scene_order": 1,
            "character_name": f"{title}角色",
            "title": f"{title}镜头",
            "description": "新镜头描述",
        }],
        "dialogues": [{
            "shot_order": 1,
            "character_name": f"{title}角色",
            "text": f"{title}对白",
        }],
    }


def _assert_existing_graph_unchanged(database, old_ids):
    with session_scope(database) as session:
        project = session.get(Project, old_ids["project"])
        character = session.get(Character, old_ids["character"])
        shot = session.get(Shot, old_ids["shot"])
        dialogue = session.get(Dialogue, old_ids["dialogue"])
        assert project.name == "旧标题"
        assert character.name == "旧角色"
        assert session.get(CharacterReference, old_ids["reference"]) is not None
        assert session.get(VoiceProfile, old_ids["voice"]) is not None
        assert session.get(Scene, old_ids["scene"]) is not None
        assert shot.scene_id == old_ids["scene"]
        assert shot.character_id == old_ids["character"]
        assert dialogue.shot_id == old_ids["shot"]
        assert dialogue.character_id == old_ids["character"]
        assert session.query(Project).count() == 1
        assert session.query(Character).count() == 1
        assert session.query(CharacterReference).count() == 1
        assert session.query(VoiceProfile).count() == 1
        assert session.query(Scene).count() == 1
        assert session.query(Shot).count() == 1
        assert session.query(Dialogue).count() == 1


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
    ("reference_kind", "expected_error"),
    [
        ("shot", "shot 1 references missing character 幽灵"),
        ("dialogue", "dialogue for shot 1 references missing character 幽灵"),
    ],
)
def test_replace_project_drama_rejects_unknown_characters_before_replacing(
    tmp_path,
    reference_kind,
    expected_error,
):
    database = str(tmp_path / f"unknown-{reference_kind}.db")
    create_schema(database)
    old_ids = _seed_existing_graph(database)
    data = _replacement_data()
    if reference_kind == "shot":
        data["shots"][0]["character_name"] = "幽灵"
    else:
        data["dialogues"][0]["character_name"] = "幽灵"

    from app.services.drama_persistence import replace_project_drama

    destructive_statements = []

    def record_destructive_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("delete from"):
            destructive_statements.append(normalized)

    engine = get_engine(database)
    event.listen(engine, "before_cursor_execute", record_destructive_statement)
    try:
        with pytest.raises(ValueError, match=expected_error):
            replace_project_drama(
                database,
                old_ids["project"],
                StructuredDrama.model_validate(data),
            )
    finally:
        event.remove(engine, "before_cursor_execute", record_destructive_statement)

    assert destructive_statements == []
    _assert_existing_graph_unchanged(database, old_ids)


@pytest.mark.parametrize(
    ("reference_kind", "expected_error"),
    [
        ("scene", "shot 1 references missing scene 88"),
        ("shot", "dialogue references missing shot 88"),
    ],
)
def test_replace_project_drama_rolls_back_after_invalid_order_reference(
    tmp_path,
    reference_kind,
    expected_error,
):
    database = str(tmp_path / f"rollback-{reference_kind}.db")
    create_schema(database)
    old_ids = _seed_existing_graph(database)
    data = _replacement_data()
    if reference_kind == "scene":
        data["shots"][0]["scene_order"] = 88
    else:
        data["dialogues"][0]["shot_order"] = 88

    from app.services.drama_persistence import replace_project_drama

    with pytest.raises(ValueError, match=expected_error):
        replace_project_drama(
            database,
            old_ids["project"],
            StructuredDrama.model_validate(data),
        )

    _assert_existing_graph_unchanged(database, old_ids)


@pytest.mark.parametrize(
    ("explicit_face", "expected_face"),
    [
        (None, "高颧骨、轮廓分明"),
        ("方脸", "方脸"),
    ],
)
def test_replace_project_drama_maps_legacy_appearance_without_overriding_face(
    tmp_path,
    explicit_face,
    expected_face,
):
    database = str(tmp_path / f"legacy-appearance-{explicit_face}.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="旧格式")
        session.add(project)
        session.flush()
        project_id = project.id

    data = _replacement_data("旧格式")
    data["characters"][0]["appearance"] = "高颧骨、轮廓分明"
    if explicit_face is not None:
        data["characters"][0]["face"] = explicit_face

    from app.services.drama_persistence import replace_project_drama

    replace_project_drama(
        database,
        project_id,
        StructuredDrama.model_validate(data),
    )

    with session_scope(database) as session:
        character = session.query(Character).one()
        assert character.visual_bible_json["face"] == expected_face


def test_replace_project_drama_removes_old_dependents_and_writes_coherent_graph(tmp_path):
    database = str(tmp_path / "replace-old-graph.db")
    create_schema(database)
    old_ids = _seed_existing_graph(database)

    from app.services.drama_persistence import replace_project_drama

    replace_project_drama(
        database,
        old_ids["project"],
        StructuredDrama.model_validate(_replacement_data()),
    )

    with session_scope(database) as session:
        assert session.get(Character, old_ids["character"]) is None
        assert session.get(CharacterReference, old_ids["reference"]) is None
        assert session.get(VoiceProfile, old_ids["voice"]) is None
        assert session.get(Scene, old_ids["scene"]) is None
        assert session.get(Shot, old_ids["shot"]) is None
        assert session.get(Dialogue, old_ids["dialogue"]) is None

        character = session.query(Character).one()
        scene = session.query(Scene).one()
        shot = session.query(Shot).one()
        dialogue = session.query(Dialogue).one()
        assert character.name == "新标题角色"
        assert scene.title == "新标题场景"
        assert shot.scene_id == scene.id
        assert shot.character_id == character.id
        assert dialogue.shot_id == shot.id
        assert dialogue.character_id == character.id
        assert session.query(CharacterReference).count() == 0
        assert session.query(VoiceProfile).count() == 0


def test_concurrent_replacements_leave_one_complete_project_graph(tmp_path):
    database = str(tmp_path / "concurrent-replace.db")
    create_schema(database)
    old_ids = _seed_existing_graph(database)
    engine = get_engine(database)
    first_acquired = threading.Event()
    allow_first_to_finish = threading.Event()
    second_attempted = threading.Event()
    second_acquired = threading.Event()
    begin_attempts = 0
    begin_attempts_lock = threading.Lock()
    statements_by_attempt = {1: [], 2: []}

    def record_begin_attempt(
        connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        nonlocal begin_attempts
        normalized = " ".join(statement.lower().split())
        if normalized == "begin immediate":
            with begin_attempts_lock:
                begin_attempts += 1
                attempt = begin_attempts
            connection.info["replacement_attempt"] = attempt
            if attempt == 2:
                second_attempted.set()
        attempt = connection.info.get("replacement_attempt")
        if attempt in statements_by_attempt:
            statements_by_attempt[attempt].append(normalized)

    def hold_first_writer_after_lock(
        connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if " ".join(statement.lower().split()) != "begin immediate":
            return
        attempt = connection.info["replacement_attempt"]
        if attempt == 1:
            first_acquired.set()
            if not allow_first_to_finish.wait(timeout=5):
                raise TimeoutError("test did not release the first replacement")
        elif attempt == 2:
            second_acquired.set()

    from app.services.drama_persistence import replace_project_drama

    dramas = [
        StructuredDrama.model_validate(_replacement_data("并发甲")),
        StructuredDrama.model_validate(_replacement_data("并发乙")),
    ]
    event.listen(engine, "before_cursor_execute", record_begin_attempt)
    event.listen(engine, "after_cursor_execute", hold_first_writer_after_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                replace_project_drama, database, old_ids["project"], dramas[0]
            )
            try:
                assert first_acquired.wait(timeout=5)
                second = executor.submit(
                    replace_project_drama, database, old_ids["project"], dramas[1]
                )
                assert second_attempted.wait(timeout=5)
                assert not second_acquired.wait(timeout=0.2)
            finally:
                allow_first_to_finish.set()
            futures = [first, second]
            assert [future.result(timeout=10) for future in futures] == [
                {"characters": 1, "scenes": 1, "shots": 1, "dialogues": 1},
                {"characters": 1, "scenes": 1, "shots": 1, "dialogues": 1},
            ]
    finally:
        allow_first_to_finish.set()
        event.remove(engine, "before_cursor_execute", record_begin_attempt)
        event.remove(engine, "after_cursor_execute", hold_first_writer_after_lock)

    assert second_acquired.is_set()
    assert statements_by_attempt[1][0] == "begin immediate"
    assert statements_by_attempt[2][0] == "begin immediate"

    with session_scope(database) as session:
        project = session.get(Project, old_ids["project"])
        characters = session.query(Character).all()
        scenes = session.query(Scene).all()
        shots = session.query(Shot).all()
        dialogues = session.query(Dialogue).all()
        assert project.name == "并发乙"
        assert len(characters) == len(scenes) == len(shots) == len(dialogues) == 1
        assert characters[0].name == f"{project.name}角色"
        assert scenes[0].title == f"{project.name}场景"
        assert shots[0].title == f"{project.name}镜头"
        assert dialogues[0].text == f"{project.name}对白"
        assert shots[0].scene_id == scenes[0].id
        assert shots[0].character_id == characters[0].id
        assert dialogues[0].shot_id == shots[0].id
        assert dialogues[0].character_id == characters[0].id


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
