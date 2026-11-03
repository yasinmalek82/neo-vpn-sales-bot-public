"""Mark a renew that replaces the plan instead of extending it.

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t7
Create Date: 2026-09-26

A customer whose plan is gone (every account imported from the old bot, or an
unlimited switched to the tunnel product) picks a new plan for the same
account and link. That order resets the account to the new plan: expiry from
today, a fresh volume, the new plan's panel group and device limit. It is set
only when the customer chose it on a screen that said the leftover is dropped,
never inferred from a slug mismatch, which would wipe someone's leftover
because a plan was re-created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p3q4r5s6t7u8"
down_revision: str | None = "o2p3q4r5s6t7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("renew_resets", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("orders", "renew_resets")
