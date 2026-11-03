"""Marketing nudges: the owner's switches.

Revision ID: z4a5b6c7d8e9
Revises: y2z3a4b5c6d7
Create Date: 2026-10-10

``marketing_settings``: one row (id = 1). An empty table means every campaign
is off, so a running shop changes nothing until the owner opens the screen.
``<name>_enabled_at`` is NULL while a campaign is off. The offer columns are
stored from here and read from M3 on.

Plain DDL only, so SQLite and PostgreSQL both run it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z4a5b6c7d8e9"
down_revision: str | None = "y2z3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "marketing_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("cart_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_convert_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winback_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upgrade_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_convert_offer_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trial_convert_offer_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("winback_offer_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winback_offer_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("upgrade_offer_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upgrade_offer_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "trial_convert_offer_percent BETWEEN 0 AND 50"
            " AND winback_offer_percent BETWEEN 0 AND 50"
            " AND upgrade_offer_percent BETWEEN 0 AND 50",
            name="ck_marketing_offer_percent",
        ),
        sa.CheckConstraint(
            "trial_convert_offer_hours BETWEEN 1 AND 168"
            " AND winback_offer_hours BETWEEN 1 AND 168"
            " AND upgrade_offer_hours BETWEEN 1 AND 168",
            name="ck_marketing_offer_hours",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("marketing_settings")
