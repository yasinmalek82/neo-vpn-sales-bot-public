"""Track when a user last used the bot

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-19

The admin's user card could say what someone bought but not whether they are
still around. «آخرین زمان استفاده از ربات» is the difference between a dormant
account and an active customer who has simply not bought recently.

Nullable with no backfill: existing rows read «—» until their owner next opens
the bot, which is honest — we genuinely do not know when they were last here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_seen_at")
