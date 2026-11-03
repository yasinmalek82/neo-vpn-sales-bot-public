"""Broadcasts with one row per recipient

The per-recipient row is what makes a campaign survive a restart: a send to a
few thousand customers spans minutes, and without a durable record of who
already received it, resuming means spamming everyone again or abandoning the
rest.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "audience",
            sa.Enum(
                "all_customers",
                "with_service",
                "expiring_soon",
                "without_service",
                name="broadcast_audience",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "draft",
                "sending",
                "done",
                "cancelled",
                name="broadcast_state",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broadcasts_state", "broadcasts", ["state"], unique=False)

    op.create_table(
        "broadcast_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broadcast_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "sent",
                "blocked",
                "failed",
                name="broadcast_delivery_state",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["broadcast_id"], ["broadcasts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Re-running the worker after a crash cannot deliver the same campaign
        # to the same person twice.
        sa.UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_targets_pair"),
    )
    op.create_index(
        "ix_broadcast_targets_pending",
        "broadcast_targets",
        ["broadcast_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_targets_pending", table_name="broadcast_targets")
    op.drop_table("broadcast_targets")
    op.drop_index("ix_broadcasts_state", table_name="broadcasts")
    op.drop_table("broadcasts")
