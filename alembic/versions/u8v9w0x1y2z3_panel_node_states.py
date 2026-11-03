"""Panel and node watcher state.

Revision ID: u8v9w0x1y2z3
Revises: t7u8v9w0x1y2
Create Date: 2026-09-30

One row per PasarGuard node plus row 0 for the panel API. An empty table
changes nothing for a running shop: the watcher fills it on its first check.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u8v9w0x1y2z3"
down_revision: str | None = "t7u8v9w0x1y2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "panel_node_states",
        sa.Column("node_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checks_in_status", sa.Integer(), nullable=False),
        sa.Column("reported_status", sa.String(length=16), nullable=True),
        sa.Column("reported_since", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("node_id"),
    )


def downgrade() -> None:
    op.drop_table("panel_node_states")
