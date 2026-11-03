"""Foundation hardening: order snapshots, force_new, renew intent

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-12

- orders.force_new (replaces panel_password __force_new__ sentinel)
- orders.delivery_* snapshot fields (nullable; no backfill / invent history)
- orders.renew_target_expire_unix / renew_target_data_limit
- migrate existing __force_new__ panel_password markers → force_new=true
SQLite + Postgres safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    json_type = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()

    op.add_column(
        "orders",
        sa.Column("force_new", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("orders", sa.Column("delivery_data_limit_gb", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("delivery_duration_days", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("delivery_hwid_limit", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("delivery_group_ids", json_type, nullable=True))
    op.add_column("orders", sa.Column("renew_target_expire_unix", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("renew_target_data_limit", sa.BigInteger(), nullable=True))

    # Migrate sentinel without inventing delivery history.
    op.execute(
        sa.text(
            "UPDATE orders SET force_new = true, panel_password = NULL "
            "WHERE panel_password = '__force_new__'"
        )
    )


def downgrade() -> None:
    # Best-effort restore sentinel for rows that still have force_new.
    op.execute(
        sa.text(
            "UPDATE orders SET panel_password = '__force_new__' "
            "WHERE force_new = true AND (panel_password IS NULL OR panel_password = '')"
        )
    )
    op.drop_column("orders", "renew_target_data_limit")
    op.drop_column("orders", "renew_target_expire_unix")
    op.drop_column("orders", "delivery_group_ids")
    op.drop_column("orders", "delivery_hwid_limit")
    op.drop_column("orders", "delivery_duration_days")
    op.drop_column("orders", "delivery_data_limit_gb")
    op.drop_column("orders", "force_new")
