"""Reseller role and per-reseller pricing

Revision ID: e1f2a3b4c5d6r
Revises: d0e1f2a3b4c5
Create Date: 2026-09-19

The third role the bot was missing. A reseller is a mobile shop that buys at an
agreed price and hands the subscription link to their own customer, who never
opens this bot.

`reseller_plan_prices` keys on the package **slug** rather than its id:
publishing a catalog edit clones the version and mints new row ids, so an id
here would come unstuck from its plan the first time a price changed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6r"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("is_reseller", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.create_table(
        "reseller_plan_prices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("package_slug", sa.String(length=128), nullable=False),
        sa.Column("price_toman", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "package_slug", name="uq_reseller_plan_price"),
    )
    op.create_index(
        "ix_reseller_plan_prices_user_id", "reseller_plan_prices", ["user_id"], unique=False
    )

    op.create_table(
        "reseller_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("price_per_gb_toman", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("price_per_day_toman", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("trial_quota_monthly", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trial_price_toman", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_reseller_rate_user"),
    )


def downgrade() -> None:
    op.drop_table("reseller_rates")
    op.drop_index("ix_reseller_plan_prices_user_id", table_name="reseller_plan_prices")
    op.drop_table("reseller_plan_prices")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_reseller")
