"""Owner-uploaded brand images (the service QR background).

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-09-26

In the database, not on disk, so the upload survives a container rebuild and
comes back with a restore.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r5s6t7u8v9w0"
down_revision: str | None = "q4r5s6t7u8v9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brand_assets",
        sa.Column("key", sa.String(length=32), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("brand_assets")
