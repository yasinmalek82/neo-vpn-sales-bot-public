"""One open checkout order per user.

Revision ID: k8l9m0n1o2p3
Revises: j7k8l9m0n1o2
Create Date: 2026-09-22

Does not delete historical duplicate open orders. If any user already has more
than one PENDING_PAYMENT or PAYMENT_REVIEW row, upgrade stops and leaves those
rows in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k8l9m0n1o2p3"
down_revision: str | None = "j7k8l9m0n1o2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _duplicate_open_user_ids() -> list[int]:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        statement = sa.text("""
            SELECT user_id
            FROM orders
            WHERE status::text IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW')
            GROUP BY user_id
            HAVING COUNT(*) > 1
            """)
    else:
        statement = sa.text("""
            SELECT user_id
            FROM orders
            WHERE status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW')
            GROUP BY user_id
            HAVING COUNT(*) > 1
            """)
    rows = bind.execute(statement).fetchall()
    return [int(row[0]) for row in rows]


def upgrade() -> None:
    duplicates = _duplicate_open_user_ids()
    if duplicates:
        listed = ", ".join(str(user_id) for user_id in duplicates)
        raise RuntimeError(
            "uq_orders_one_open_per_user refused: users "
            f"{listed} already have more than one open order. "
            "Historical rows were left in place."
        )
    op.create_index(
        "uq_orders_one_open_per_user",
        "orders",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW')"),
        postgresql_where=sa.text(
            "status IN ('PENDING_PAYMENT'::order_status, 'PAYMENT_REVIEW'::order_status)"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_orders_one_open_per_user", table_name="orders")
