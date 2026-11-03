"""Keep the Telegram media type of payment receipt file IDs.

Revision ID: z8e9f0a1b2c3
Revises: z7d8e9f0a1b2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "z8e9f0a1b2c3"
down_revision = "z7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing stored receipts used sendPhoto; retain that legacy behavior.
    op.add_column(
        "payments",
        sa.Column("receipt_media_type", sa.String(16), nullable=False, server_default="photo"),
    )


def downgrade() -> None:
    op.drop_column("payments", "receipt_media_type")
