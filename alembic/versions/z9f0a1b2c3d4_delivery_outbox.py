"""Durable receipt and service notification delivery.

Revision ID: z9f0a1b2c3d4
Revises: z8e9f0a1b2c3
"""

import sqlalchemy as sa
from alembic import op

revision = "z9f0a1b2c3d4"
down_revision = "z8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(160), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(32)),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(64)),
    )
    op.create_index("ix_delivery_outbox_pending", "delivery_outbox", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("delivery_outbox")
