"""Phase 7: trial_claimed_at, last_reminder_at, referral order unique

Revision ID: c7d8e9f0a1b2
Revises: b1c2d3e4f5a6
Create Date: 2026-09-12

Schema decisions:
- users.trial_claimed_at: one free trial per user (clear idempotency flag)
- orders.last_reminder_at: durable renewal-reminder dedupe across restarts
- uq_referral_logs_order_id: at most one referral reward log per order
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("trial_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Deduplicate any existing multi-logs per order before unique constraint
    op.execute("""
        DELETE FROM referral_logs
        WHERE id NOT IN (
            SELECT MIN(id) FROM referral_logs WHERE order_id IS NOT NULL GROUP BY order_id
        )
        AND order_id IS NOT NULL
        """)
    with op.batch_alter_table("referral_logs") as batch:
        batch.create_unique_constraint("uq_referral_logs_order_id", ["order_id"])


def downgrade() -> None:
    with op.batch_alter_table("referral_logs") as batch:
        batch.drop_constraint("uq_referral_logs_order_id", type_="unique")
    op.drop_column("orders", "last_reminder_at")
    op.drop_column("users", "trial_claimed_at")
