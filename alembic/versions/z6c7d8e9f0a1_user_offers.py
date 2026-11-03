"""Personal offers: a discount for one user, with no code.

Revision ID: z6c7d8e9f0a1
Revises: z5b6c7d8e9f0
Create Date: 2026-10-10

``user_offers`` is written with a marketing nudge, keyed by the notice's
``event_key`` (unique, so a retried send reuses the row). ``order_id`` is unique:
one offer is used by one order. Nothing existing changes.

Plain DDL only, so SQLite and PostgreSQL both run it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z6c7d8e9f0a1"
down_revision: str | None = "z5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "user_offers",
        sa.Column("id", _BIGINT, autoincrement=True, nullable=False),
        sa.Column("user_id", _BIGINT, nullable=False),
        sa.Column("campaign", sa.String(length=24), nullable=False),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column("min_volume_gb", sa.Integer(), nullable=True),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", _BIGINT, nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("percent BETWEEN 1 AND 50", name="ck_user_offers_percent"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_user_offers_event_key", "user_offers", ["event_key"], unique=True)
    op.create_index("uq_user_offers_order_id", "user_offers", ["order_id"], unique=True)
    op.create_index("ix_user_offers_user_id", "user_offers", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_offers_user_id", table_name="user_offers")
    op.drop_index("uq_user_offers_order_id", table_name="user_offers")
    op.drop_index("uq_user_offers_event_key", table_name="user_offers")
    op.drop_table("user_offers")
