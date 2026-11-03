"""Customer-card button target on a queued report.

Revision ID: m0n1o2p3q4r5
Revises: l9m0n1o2p3q4
Create Date: 2026-09-23

The report body stays text. The button is attached at send time from this id,
so a group message is never rewritten into the customer card.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m0n1o2p3q4r5"
down_revision: str | None = "l9m0n1o2p3q4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_outbox",
        sa.Column("link_user_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("report_outbox", "link_user_id")
