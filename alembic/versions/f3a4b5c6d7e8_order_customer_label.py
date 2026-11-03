"""Order.customer_label — the shop's note, split from the panel name seed

Revision ID: f3a4b5c6d7e8
Revises: e1f2a3b4c5d6r
Create Date: 2026-09-19

`service_name` did two jobs: it seeded the panel username (`reza_a7f3`) and it
labelled the service in «مشتری‌های من». A reseller cannot use one string for
both — «رضا» sanitizes to nothing, so the panel username fell back to a
predictable `neo_<order id>` and the shop had to invent an English alias for
every walk-in customer.

This column takes the second job. Nothing backfills into it: an existing
service keeps showing its `service_name`, and the rename flow writes a real
label the first time the shop edits one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | None = "e1f2a3b4c5d6r"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("customer_label", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "customer_label")
