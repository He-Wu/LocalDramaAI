import sqlite3

from sqlalchemy import create_engine, inspect, text

from app.db.session import create_schema, session_scope
from app.models import Project, Scene, Shot
from app.schemas.drama import ShotSpec


def test_phase8_shot_defaults_are_false(tmp_path):
    database = str(tmp_path / "phase8.db")
    create_schema(database)

    with session_scope(database) as session:
        project = Project(name="Phase 8")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, order=1, title="Scene", description="Description")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, order=1, title="Shot", description="Description")
        session.add(shot)
        session.flush()
        shot_id = shot.id

    with session_scope(database) as session:
        shot = session.get(Shot, shot_id)
        assert shot.requires_lip_sync is False
        assert shot.speaker_visible is False
        assert shot.lipsync_asset_id is None


def test_phase8_shot_spec_defaults_are_false():
    shot = ShotSpec(order=1, title="Shot", description="Description")

    assert shot.requires_lip_sync is False
    assert shot.speaker_visible is False


def test_phase7_database_upgrades_without_losing_video_link(tmp_path):
    database = tmp_path / "phase7.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;

            CREATE TABLE projects (
                id VARCHAR(36) NOT NULL PRIMARY KEY
            );

            CREATE TABLE scenes (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE assets (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE shots (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                scene_id VARCHAR(36) NOT NULL,
                video_asset_id VARCHAR(36),
                FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
                FOREIGN KEY(video_asset_id) REFERENCES assets(id) ON DELETE SET NULL
            );
            """
        )
        connection.execute("INSERT INTO projects VALUES (?)", ("project-1",))
        connection.execute("INSERT INTO scenes VALUES (?, ?)", ("scene-1", "project-1"))
        connection.execute("INSERT INTO assets VALUES (?, ?)", ("video-asset-1", "project-1"))
        connection.execute(
            "INSERT INTO shots VALUES (?, ?, ?)",
            ("shot-1", "scene-1", "video-asset-1"),
        )

    from app.db.migrations import upgrade_schema

    upgrade_schema(str(database))
    upgrade_schema(f"sqlite:///{database.as_posix()}")

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("shots")}
        assert {"requires_lip_sync", "speaker_visible", "lipsync_asset_id"} <= columns

        foreign_keys = inspector.get_foreign_keys("shots")
        assert any(
            foreign_key["constrained_columns"] == ["video_asset_id"]
            and foreign_key["referred_table"] == "assets"
            and foreign_key["referred_columns"] == ["id"]
            for foreign_key in foreign_keys
        )
        assert any(
            foreign_key["name"] == "fk_shots_lipsync_asset_id_assets"
            and foreign_key["constrained_columns"] == ["lipsync_asset_id"]
            and foreign_key["referred_table"] == "assets"
            and foreign_key["referred_columns"] == ["id"]
            and foreign_key["options"].get("ondelete") == "SET NULL"
            for foreign_key in foreign_keys
        )

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT requires_lip_sync, speaker_visible, lipsync_asset_id, video_asset_id
                    FROM shots WHERE id = 'shot-1'
                    """
                )
            ).mappings().one()
        assert row == {
            "requires_lip_sync": 0,
            "speaker_visible": 0,
            "lipsync_asset_id": None,
            "video_asset_id": "video-asset-1",
        }
    finally:
        engine.dispose()
