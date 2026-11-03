"""Autonomous provisioning retry metadata.

Revision ID: za0b1c2d3e4f
Revises: z9f0a1b2c3d4
"""

import sqlalchemy as sa
from alembic import op

revision = "za0b1c2d3e4f"
down_revision = "z9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provision_recovery",
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(64)),
    )


def downgrade() -> None:
    op.drop_table("provision_recovery")
