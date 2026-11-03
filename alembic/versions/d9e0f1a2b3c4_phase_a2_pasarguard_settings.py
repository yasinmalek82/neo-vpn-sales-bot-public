"""Phase A2: pasarguard_settings singleton for in-bot panel config

Revision ID: d9e0f1a2b3c4
Revises: c7d8e9f0a1b2
Create Date: 2026-09-12

Schema decisions:
- Singleton row (id=1) stores optional overrides for PasarGuard connection.
- Env remains fallback when columns are NULL / row missing.
- admin_password stored as secret; never log or echo in Telegram.
- Fresh SQLite: prior revisions use Integer PK variant for AUTOINCREMENT.
  Existing broken SQLite DBs without AUTOINCREMENT should recreate
  (`rm data/bot.db && alembic upgrade head`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pasarguard_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("admin_username", sa.String(length=128), nullable=True),
        sa.Column("admin_password", sa.Text(), nullable=True),
        sa.Column("sub_path", sa.String(length=64), nullable=True),
        sa.Column(
            "default_group_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("timeout", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_by_admin_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("pasarguard_settings")
