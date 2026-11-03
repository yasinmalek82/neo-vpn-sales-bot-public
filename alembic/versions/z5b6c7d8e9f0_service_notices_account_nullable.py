"""A notice that belongs to no service: ``service_notices.account_id`` becomes nullable.

Revision ID: z5b6c7d8e9f0
Revises: z4a5b6c7d8e9
Create Date: 2026-10-10

The cart reminder is about an open order, and the trial-ended nudge about an
order whose account may already be gone, so neither has a live service to point
at. Every existing row keeps its account.

The downgrade refuses while such a notice exists: putting NOT NULL back would
either fail or force a made-up account.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z5b6c7d8e9f0"
down_revision: str | None = "z4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    with op.batch_alter_table("service_notices") as batch:
        batch.alter_column("account_id", existing_type=_BIGINT, nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    stuck = bind.execute(
        sa.text("SELECT COUNT(*) FROM service_notices WHERE account_id IS NULL")
    ).scalar()
    if stuck:
        raise RuntimeError(
            f"Cannot downgrade {revision}: {stuck} notice(s) belong to no service, and "
            "account_id cannot be NOT NULL again while they exist."
        )
    with op.batch_alter_table("service_notices") as batch:
        batch.alter_column("account_id", existing_type=_BIGINT, nullable=False)
