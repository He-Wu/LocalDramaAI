from sqlalchemy import delete, select

from app.db.session import session_scope
from app.models import Character, Dialogue, Project, Scene, Shot
from app.schemas.character import VisualBible
from app.schemas.drama import StructuredDrama


def replace_project_drama(
    database_url: str,
    project_id: str,
    drama: StructuredDrama,
) -> dict:
    with session_scope(database_url) as session:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            project = session.get(Project, project_id)
        else:
            project = session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
        if project is None:
            raise ValueError("project not found")

        reference_keys = (
            ("character name", [spec.name for spec in drama.characters]),
            ("scene order", [spec.order for spec in drama.scenes]),
            ("shot order", [spec.order for spec in drama.shots]),
        )
        for label, keys in reference_keys:
            seen = set()
            for key in keys:
                if key in seen:
                    raise ValueError(f"duplicate {label}: {key}")
                seen.add(key)

        character_names = {spec.name for spec in drama.characters}
        for spec in drama.shots:
            if spec.character_name is not None and spec.character_name not in character_names:
                raise ValueError(
                    f"shot {spec.order} references missing character {spec.character_name}"
                )
        for spec in drama.dialogues:
            if spec.character_name is not None and spec.character_name not in character_names:
                raise ValueError(
                    "dialogue for shot "
                    f"{spec.shot_order} references missing character {spec.character_name}"
                )

        session.execute(delete(Scene).where(Scene.project_id == project_id))
        session.execute(delete(Character).where(Character.project_id == project_id))

        characters = {}
        for spec in drama.characters:
            face = spec.face
            if spec.appearance and "face" not in spec.model_fields_set:
                face = spec.appearance
            bible = VisualBible(
                name=spec.name,
                age=spec.age,
                gender=spec.gender,
                face=face,
                eyes=spec.eyes,
                nose=spec.nose,
                mouth=spec.mouth,
                hair=spec.hair,
                body=spec.body,
                clothes=spec.clothes,
                accessories=spec.accessories,
                visual_style=spec.visual_style,
            )
            character = Character(
                project_id=project_id,
                name=spec.name,
                visual_bible_json=bible.model_dump(),
            )
            session.add(character)
            session.flush()
            characters[spec.name] = character

        scenes = {}
        for spec in sorted(drama.scenes, key=lambda item: item.order):
            scene = Scene(
                project_id=project_id,
                order=spec.order,
                title=spec.title,
                description=spec.description,
                location=spec.location,
                time_of_day=spec.time_of_day,
                mood=spec.mood,
                estimated_duration=spec.estimated_duration,
            )
            session.add(scene)
            session.flush()
            scenes[spec.order] = scene

        shots = {}
        for spec in sorted(drama.shots, key=lambda item: item.order):
            scene = scenes.get(spec.scene_order)
            if scene is None:
                raise ValueError(
                    f"shot {spec.order} references missing scene {spec.scene_order}"
                )
            character = characters.get(spec.character_name) if spec.character_name else None
            shot = Shot(
                scene_id=scene.id,
                character_id=character.id if character else None,
                order=spec.order,
                title=spec.title,
                description=spec.description,
                shot_type=spec.shot_type,
                duration=spec.duration,
                image_prompt=spec.image_prompt,
                video_prompt=spec.video_prompt,
                negative_prompt=spec.negative_prompt,
            )
            session.add(shot)
            session.flush()
            shots[spec.order] = shot

        dialogue_orders = {}
        for spec in sorted(drama.dialogues, key=lambda item: item.shot_order):
            shot = shots.get(spec.shot_order)
            if shot is None:
                raise ValueError(f"dialogue references missing shot {spec.shot_order}")
            character = characters.get(spec.character_name) if spec.character_name else None
            order = dialogue_orders.get(spec.shot_order, 0) + 1
            dialogue_orders[spec.shot_order] = order
            session.add(
                Dialogue(
                    shot_id=shot.id,
                    character_id=character.id if character else None,
                    order=order,
                    text=spec.text,
                    emotion=spec.emotion,
                )
            )

        project.name = drama.title
        project.status = "STRUCTURED"
        session.flush()
        return {
            "characters": len(characters),
            "scenes": len(scenes),
            "shots": len(shots),
            "dialogues": len(drama.dialogues),
        }
