"""Payment card the owner edits from Telegram.

Revision ID: t7u8v9w0x1y2
Revises: s6t7u8v9w0x1
Create Date: 2026-09-30

Singleton row (id = 1). A NULL column means «use the .env value», so an empty
table changes nothing for a running shop.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t7u8v9w0x1y2"
down_revision: str | None = "s6t7u8v9w0x1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("card_number", sa.String(length=16), nullable=True),
        sa.Column("card_holder_name", sa.String(length=64), nullable=True),
        sa.Column("bank_name", sa.String(length=64), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_admin_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("payment_settings")
