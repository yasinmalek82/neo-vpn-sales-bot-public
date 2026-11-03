"""Add durable personal/reseller identity to trial orders.

Revision ID: j7k8l9m0n1o2
Revises: i6j7k8l9m0n1
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "j7k8l9m0n1o2"
down_revision: str | None = "i6j7k8l9m0n1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("trial_kind", sa.String(length=16), nullable=True))
    with op.batch_alter_table("orders") as batch:
        batch.create_check_constraint(
            "ck_orders_trial_kind",
            "trial_kind IS NULL OR trial_kind IN ('personal', 'reseller')",
        )

    # Only a claimed user with exactly one total historical trial is provably a
    # personal operation. Every other legacy row stays NULL and safe-denied.
    op.execute("""
        UPDATE orders
        SET trial_kind = 'personal'
        WHERE id IN (
            SELECT o.id
            FROM orders AS o
            JOIN users AS u ON u.id = o.user_id
            JOIN plans AS p ON p.id = o.plan_id
            WHERE u.trial_claimed_at IS NOT NULL
              AND p.name LIKE 'trial-%'
              AND (
                  SELECT COUNT(*)
                  FROM orders AS o2
                  JOIN plans AS p2 ON p2.id = o2.plan_id
                  WHERE o2.user_id = o.user_id
                    AND p2.name LIKE 'trial-%'
              ) = 1
        )
        """)
    op.create_index(
        "uq_orders_personal_trial_user",
        "orders",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("trial_kind = 'personal'"),
        postgresql_where=sa.text("trial_kind = 'personal'"),
    )


def downgrade() -> None:
    op.drop_index("uq_orders_personal_trial_user", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("ck_orders_trial_kind", type_="check")
    op.drop_column("orders", "trial_kind")
