"""Add PHASE 9 project output asset fields.

Revision ID: 0002_phase9_project_outputs
Revises: 0001_phase8_shot_lipsync
Create Date: 2026-08-12

"""
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa


revision: str = "0002_phase9_project_outputs"
down_revision: str | None = "0001_phase8_shot_lipsync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SUBTITLE_FOREIGN_KEY = "fk_projects_subtitle_asset_id_assets"
FINAL_VIDEO_FOREIGN_KEY = "fk_projects_final_video_asset_id_assets"


def _is_asset_foreign_key(foreign_key: dict, column_name: str) -> bool:
    return (
        foreign_key.get("constrained_columns") == [column_name]
        and foreign_key.get("referred_table") == "assets"
        and foreign_key.get("referred_columns") == ["id"]
    )


@contextmanager
def _disable_sqlite_foreign_keys_for_batch_recreate() -> Iterator[None]:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return

    foreign_keys_enabled = bool(
        bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if not foreign_keys_enabled:
        yield
        return

    bind.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        bind.commit()
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        bind.commit()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "projects" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("projects")
    }
    columns_to_add = [
        column
        for column in (
            sa.Column("subtitle_asset_id", sa.String(length=36), nullable=True),
            sa.Column("final_video_asset_id", sa.String(length=36), nullable=True),
        )
        if column.name not in existing_columns
    ]
    existing_foreign_keys = inspector.get_foreign_keys("projects")
    foreign_keys_to_add = [
        (SUBTITLE_FOREIGN_KEY, "subtitle_asset_id"),
        (FINAL_VIDEO_FOREIGN_KEY, "final_video_asset_id"),
    ]
    foreign_keys_to_add = [
        (constraint_name, column_name)
        for constraint_name, column_name in foreign_keys_to_add
        if not any(
            _is_asset_foreign_key(foreign_key, column_name)
            for foreign_key in existing_foreign_keys
        )
    ]

    if not columns_to_add and not foreign_keys_to_add:
        return

    with _disable_sqlite_foreign_keys_for_batch_recreate():
        with op.batch_alter_table("projects", recreate="always") as batch_op:
            for column in columns_to_add:
                batch_op.add_column(column)
            for constraint_name, column_name in foreign_keys_to_add:
                batch_op.create_foreign_key(
                    constraint_name,
                    "assets",
                    [column_name],
                    ["id"],
                    ondelete="SET NULL",
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "projects" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("projects")
    }
    columns_to_drop = [
        column_name
        for column_name in ("final_video_asset_id", "subtitle_asset_id")
        if column_name in existing_columns
    ]
    foreign_keys_to_drop = [
        foreign_key["name"]
        for foreign_key in inspector.get_foreign_keys("projects")
        if any(
            _is_asset_foreign_key(foreign_key, column_name)
            for column_name in ("subtitle_asset_id", "final_video_asset_id")
        )
        and foreign_key.get("name")
    ]

    if not columns_to_drop and not foreign_keys_to_drop:
        return

    with _disable_sqlite_foreign_keys_for_batch_recreate():
        with op.batch_alter_table("projects", recreate="always") as batch_op:
            for foreign_key_name in foreign_keys_to_drop:
                batch_op.drop_constraint(foreign_key_name, type_="foreignkey")
            for column_name in columns_to_drop:
                batch_op.drop_column(column_name)
