"""Remember which catalog plan an order bought.

Revision ID: n1o2p3q4r5s6
Revises: m0n1o2p3q4r5
Create Date: 2026-09-23

Publishing the store mints new package row ids. Renew has to find the same
plan in the live catalog, and the slug is the only identity that survives that.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n1o2p3q4r5s6"
down_revision: str | None = "m0n1o2p3q4r5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("store_package_slug", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "store_package_slug")
