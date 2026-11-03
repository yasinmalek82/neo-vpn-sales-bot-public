"""Promo codes with usage rules: ``promo_redemptions``.

Revision ID: z7d8e9f0a1b2
Revises: z6c7d8e9f0a1
Create Date: 2026-10-10

One row per order that took a promo code, written in the order's own transaction. ``order_id``
is unique: an order redeems at most one code. ``code`` is the upper-case code string (the
store pricing document keys on it), so a catalog publish keeps the counts.

Plain DDL only, so SQLite and PostgreSQL both run it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z7d8e9f0a1b2"
down_revision: str | None = "z6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "promo_redemptions",
        sa.Column("id", _BIGINT, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("user_id", _BIGINT, nullable=False),
        sa.Column("order_id", _BIGINT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_redemptions_code", "promo_redemptions", ["code"])
    op.create_index("uq_promo_redemptions_order_id", "promo_redemptions", ["order_id"], unique=True)
    op.create_index("ix_promo_redemptions_user_id", "promo_redemptions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_promo_redemptions_user_id", table_name="promo_redemptions")
    op.drop_index("uq_promo_redemptions_order_id", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_code", table_name="promo_redemptions")
    op.drop_table("promo_redemptions")
