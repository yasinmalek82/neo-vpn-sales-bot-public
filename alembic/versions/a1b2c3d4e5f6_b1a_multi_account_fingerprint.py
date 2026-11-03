"""B1a: allow 1:N PasarGuardAccount per user + plan_fingerprint

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-09-12

- Drop UNIQUE on pasarguard_accounts.user_id; keep non-unique index.
- Keep UNIQUE on pasarguard_accounts.panel_username and panel_user_id.
- Drop UNIQUE on orders.panel_username (renew orders share panel user).
- Add nullable plan_fingerprint for same-plan renew matching.
SQLite + Postgres compatible (batch_alter for SQLite index rewrite).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("pasarguard_accounts") as batch:
            batch.drop_index("ix_pasarguard_accounts_user_id")
            batch.create_index("ix_pasarguard_accounts_user_id", ["user_id"], unique=False)
            batch.add_column(sa.Column("plan_fingerprint", sa.String(length=128), nullable=True))
        with op.batch_alter_table("orders") as batch:
            batch.drop_index("ix_orders_panel_username")
            batch.create_index("ix_orders_panel_username", ["panel_username"], unique=False)
    else:
        op.drop_index("ix_pasarguard_accounts_user_id", table_name="pasarguard_accounts")
        op.create_index(
            "ix_pasarguard_accounts_user_id",
            "pasarguard_accounts",
            ["user_id"],
            unique=False,
        )
        op.add_column(
            "pasarguard_accounts",
            sa.Column("plan_fingerprint", sa.String(length=128), nullable=True),
        )
        op.drop_index("ix_orders_panel_username", table_name="orders")
        op.create_index("ix_orders_panel_username", "orders", ["panel_username"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("orders") as batch:
            batch.drop_index("ix_orders_panel_username")
            batch.create_index("ix_orders_panel_username", ["panel_username"], unique=True)
        with op.batch_alter_table("pasarguard_accounts") as batch:
            batch.drop_column("plan_fingerprint")
            batch.drop_index("ix_pasarguard_accounts_user_id")
            batch.create_index("ix_pasarguard_accounts_user_id", ["user_id"], unique=True)
    else:
        op.drop_index("ix_orders_panel_username", table_name="orders")
        op.create_index("ix_orders_panel_username", "orders", ["panel_username"], unique=True)
        op.drop_column("pasarguard_accounts", "plan_fingerprint")
        op.drop_index("ix_pasarguard_accounts_user_id", table_name="pasarguard_accounts")
        op.create_index(
            "ix_pasarguard_accounts_user_id",
            "pasarguard_accounts",
            ["user_id"],
            unique=True,
        )
