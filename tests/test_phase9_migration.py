import importlib
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text

from app.db.session import create_schema, initialize_database, session_scope
from app.models import Project


PHASE8_REVISION = "0001_phase8_shot_lipsync"
PHASE9_REVISION = "0002_phase9_project_outputs"
SUBTITLE_FOREIGN_KEY = "fk_projects_subtitle_asset_id_assets"
FINAL_VIDEO_FOREIGN_KEY = "fk_projects_final_video_asset_id_assets"


def _alembic_config(database: Path) -> Config:
    repository_root = Path(__file__).resolve().parents[1]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        (repository_root / "migrations").as_posix(),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _create_phase8_database(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys=ON;

            CREATE TABLE projects (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                story TEXT,
                description TEXT,
                language VARCHAR(20) NOT NULL,
                style VARCHAR(100),
                status VARCHAR(30) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );

            CREATE TABLE assets (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                kind VARCHAR(40) NOT NULL,
                path TEXT NOT NULL,
                mime_type VARCHAR(100),
                metadata_json JSON,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE scenes (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                title VARCHAR(200) NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );

            INSERT INTO projects VALUES (
                'project-1', 'Phase 8 project', 'story', 'description',
                'zh-CN', 'cinematic', 'DRAFT',
                '2026-08-11 00:00:00', '2026-08-11 00:00:00'
            );
            INSERT INTO assets VALUES (
                'subtitle-asset-1', 'project-1', 'subtitle', '/subtitle.srt',
                'application/x-subrip', NULL,
                '2026-08-11 00:00:00', '2026-08-11 00:00:00'
            );
            INSERT INTO assets VALUES (
                'video-asset-1', 'project-1', 'video', '/final.mp4',
                'video/mp4', NULL,
                '2026-08-11 00:00:00', '2026-08-11 00:00:00'
            );
            INSERT INTO scenes VALUES ('scene-1', 'project-1', 'Opening');
            INSERT INTO alembic_version VALUES ('{PHASE8_REVISION}');
            """
        )


def _project_output_foreign_keys(inspector):
    return {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("projects")
        if foreign_key["constrained_columns"]
        in (["subtitle_asset_id"], ["final_video_asset_id"])
    }


def _run_phase9_revision_with_foreign_keys(database: Path, direction: str):
    engine = create_engine(f"sqlite:///{database.as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    migration = importlib.import_module(
        "migrations.versions.0002_phase9_project_outputs"
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            context = MigrationContext.configure(
                connection,
                opts={"render_as_batch": True},
            )
            with Operations.context(context):
                getattr(migration, direction)()
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            return {
                "projects": connection.execute(
                    text("SELECT id, name FROM projects")
                ).all(),
                "assets": connection.execute(
                    text("SELECT id, project_id FROM assets ORDER BY id")
                ).all(),
                "scenes": connection.execute(
                    text("SELECT id, project_id FROM scenes")
                ).all(),
            }
    finally:
        engine.dispose()


def _assert_phase9_project_schema(database: Path) -> None:
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("projects")}
        assert columns["subtitle_asset_id"]["nullable"] is True
        assert columns["final_video_asset_id"]["nullable"] is True

        foreign_keys = _project_output_foreign_keys(inspector)
        assert set(foreign_keys) == {SUBTITLE_FOREIGN_KEY, FINAL_VIDEO_FOREIGN_KEY}
        assert foreign_keys[SUBTITLE_FOREIGN_KEY]["referred_table"] == "assets"
        assert foreign_keys[SUBTITLE_FOREIGN_KEY]["referred_columns"] == ["id"]
        assert foreign_keys[SUBTITLE_FOREIGN_KEY]["options"].get("ondelete") == "SET NULL"
        assert foreign_keys[FINAL_VIDEO_FOREIGN_KEY]["referred_table"] == "assets"
        assert foreign_keys[FINAL_VIDEO_FOREIGN_KEY]["referred_columns"] == ["id"]
        assert foreign_keys[FINAL_VIDEO_FOREIGN_KEY]["options"].get("ondelete") == "SET NULL"

        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == PHASE9_REVISION
    finally:
        engine.dispose()


def test_fresh_schema_project_output_fields_default_to_none(tmp_path):
    database = tmp_path / "fresh.db"
    create_schema(str(database))

    with session_scope(str(database)) as session:
        project = Project(name="Phase 9")
        session.add(project)
        session.flush()
        project_id = project.id

    with session_scope(str(database)) as session:
        project = session.get(Project, project_id)
        assert project.subtitle_asset_id is None
        assert project.final_video_asset_id is None

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        foreign_keys = _project_output_foreign_keys(inspect(engine))
        assert set(foreign_keys) == {SUBTITLE_FOREIGN_KEY, FINAL_VIDEO_FOREIGN_KEY}
    finally:
        engine.dispose()


def test_literal_phase8_database_upgrades_without_losing_project_or_assets(tmp_path):
    database = tmp_path / "phase8.db"
    _create_phase8_database(database)

    from app.db.migrations import upgrade_schema

    upgrade_schema(str(database))

    _assert_phase9_project_schema(database)
    with sqlite3.connect(database) as connection:
        project = connection.execute(
            "SELECT name, story, description, language, style, status, "
            "subtitle_asset_id, final_video_asset_id FROM projects WHERE id = ?",
            ("project-1",),
        ).fetchone()
        assets = connection.execute(
            "SELECT id, project_id, kind, path FROM assets ORDER BY id"
        ).fetchall()
    assert project == (
        "Phase 8 project",
        "story",
        "description",
        "zh-CN",
        "cinematic",
        "DRAFT",
        None,
        None,
    )
    assert assets == [
        ("subtitle-asset-1", "project-1", "subtitle", "/subtitle.srt"),
        ("video-asset-1", "project-1", "video", "/final.mp4"),
    ]


def test_batch_recreate_preserves_dependent_rows_with_foreign_keys_enabled(tmp_path):
    database = tmp_path / "foreign-keys-enabled.db"
    _create_phase8_database(database)

    expected_rows = {
        "projects": [("project-1", "Phase 8 project")],
        "assets": [
            ("subtitle-asset-1", "project-1"),
            ("video-asset-1", "project-1"),
        ],
        "scenes": [("scene-1", "project-1")],
    }
    assert _run_phase9_revision_with_foreign_keys(database, "upgrade") == expected_rows
    assert _run_phase9_revision_with_foreign_keys(database, "downgrade") == expected_rows


def test_initialize_database_is_idempotent_after_create_all(tmp_path):
    database = tmp_path / "initialized.db"
    create_schema(str(database))

    initialize_database(str(database))
    initialize_database(f"sqlite:///{database.as_posix()}")

    _assert_phase9_project_schema(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        assert len(_project_output_foreign_keys(inspect(engine))) == 2
    finally:
        engine.dispose()


def test_phase9_downgrade_removes_output_fields_and_preserves_phase8_data(tmp_path):
    database = tmp_path / "downgrade.db"
    _create_phase8_database(database)
    command.upgrade(_alembic_config(database), "head")

    _assert_phase9_project_schema(database)

    command.downgrade(_alembic_config(database), PHASE8_REVISION)

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("projects")}
        assert "subtitle_asset_id" not in columns
        assert "final_video_asset_id" not in columns
        assert _project_output_foreign_keys(inspector) == {}
        with engine.connect() as connection:
            project_name = connection.execute(
                text("SELECT name FROM projects WHERE id = 'project-1'")
            ).scalar_one()
            asset_count = connection.execute(text("SELECT COUNT(*) FROM assets")).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert project_name == "Phase 8 project"
        assert asset_count == 2
        assert revision == PHASE8_REVISION
    finally:
        engine.dispose()


def test_deleting_output_assets_sets_project_links_to_null(tmp_path):
    database = tmp_path / "set-null.db"
    _create_phase8_database(database)
    command.upgrade(_alembic_config(database), "head")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE projects SET subtitle_asset_id = ?, final_video_asset_id = ? WHERE id = ?",
            ("subtitle-asset-1", "video-asset-1", "project-1"),
        )
        connection.execute("DELETE FROM assets")
        links = connection.execute(
            "SELECT subtitle_asset_id, final_video_asset_id FROM projects WHERE id = ?",
            ("project-1",),
        ).fetchone()

    assert links == (None, None)
