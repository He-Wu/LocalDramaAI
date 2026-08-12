"""Add PHASE 8 lip-sync eligibility fields to shots.

Revision ID: 0001_phase8_shot_lipsync
Revises:
Create Date: 2026-08-11

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_phase8_shot_lipsync"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LIPSYNC_FOREIGN_KEY = "fk_shots_lipsync_asset_id_assets"


def _is_lipsync_foreign_key(foreign_key: dict) -> bool:
    return (
        foreign_key.get("constrained_columns") == ["lipsync_asset_id"]
        and foreign_key.get("referred_table") == "assets"
        and foreign_key.get("referred_columns") == ["id"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shots" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("shots")
    }
    columns_to_add = [
        column
        for column in (
            sa.Column(
                "requires_lip_sync",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "speaker_visible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("lipsync_asset_id", sa.String(length=36), nullable=True),
        )
        if column.name not in existing_columns
    ]
    has_lipsync_foreign_key = any(
        _is_lipsync_foreign_key(foreign_key)
        for foreign_key in inspector.get_foreign_keys("shots")
    )
    create_lipsync_foreign_key = not has_lipsync_foreign_key

    if not columns_to_add and not create_lipsync_foreign_key:
        return

    with op.batch_alter_table("shots", recreate="always") as batch_op:
        for column in columns_to_add:
            batch_op.add_column(column)
        if create_lipsync_foreign_key:
            batch_op.create_foreign_key(
                LIPSYNC_FOREIGN_KEY,
                "assets",
                ["lipsync_asset_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shots" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("shots")
    }
    columns_to_drop = [
        column_name
        for column_name in (
            "lipsync_asset_id",
            "speaker_visible",
            "requires_lip_sync",
        )
        if column_name in existing_columns
    ]
    foreign_keys_to_drop = [
        foreign_key["name"]
        for foreign_key in inspector.get_foreign_keys("shots")
        if _is_lipsync_foreign_key(foreign_key) and foreign_key.get("name")
    ]

    if not columns_to_drop and not foreign_keys_to_drop:
        return

    with op.batch_alter_table("shots", recreate="always") as batch_op:
        for foreign_key_name in foreign_keys_to_drop:
            batch_op.drop_constraint(foreign_key_name, type_="foreignkey")
        for column_name in columns_to_drop:
            batch_op.drop_column(column_name)
