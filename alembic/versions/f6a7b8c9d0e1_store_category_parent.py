"""Nested product groups: store_categories.parent_id

Lets «حجمی» hold «ویژه» and «اقتصادی» while «نامحدود» stays flat. One level
only — the depth rule is enforced in ``services/store_admin.py`` so a customer
never taps more than three times to reach a price.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table keeps SQLite (local smoke) working; it is a no-op wrapper
    # on PostgreSQL, which supports ALTER TABLE ADD COLUMN directly.
    with op.batch_alter_table("store_categories", schema=None) as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_store_categories_parent_id",
            "store_categories",
            ["parent_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_store_categories_parent",
        "store_categories",
        ["parent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_store_categories_parent", table_name="store_categories")
    with op.batch_alter_table("store_categories", schema=None) as batch:
        batch.drop_constraint("fk_store_categories_parent_id", type_="foreignkey")
        batch.drop_column("parent_id")
