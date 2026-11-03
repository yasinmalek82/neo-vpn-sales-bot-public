"""Report group wiring and the report outbox

A report is written inside the business transaction and delivered after it
commits, so a Telegram failure can never roll back a payment and a rolled-back
payment can never produce a report claiming it happened.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_title", sa.String(length=255), nullable=True),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("verified", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "report_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=False),
        sa.Column("topic_key", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending", "sent", "failed", name="report_outbox_state", create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # The unique key is what makes a duplicated call a no-op instead of a
        # second report for the same transition.
        sa.UniqueConstraint("event_key", name="uq_report_outbox_event_key"),
    )
    op.create_index("ix_report_outbox_state", "report_outbox", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_report_outbox_state", table_name="report_outbox")
    op.drop_table("report_outbox")
    op.drop_table("report_settings")
