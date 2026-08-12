from pathlib import Path

from alembic import command
from alembic.config import Config

from app.db.session import normalize_database_url


def upgrade_schema(database_url: str) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        (repository_root / "migrations").as_posix(),
    )
    config.set_main_option(
        "sqlalchemy.url",
        normalize_database_url(database_url).replace("%", "%%"),
    )
    command.upgrade(config, "head")
