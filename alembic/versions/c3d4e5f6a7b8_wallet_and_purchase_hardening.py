"""Wallet ledger + purchase hardening schema

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-09-12

- users.wallet_balance_toman
- wallet_ledger_entries table
- OrderStatus.EXPIRED
- orders.wallet_applied_toman, service_name, renew_account_id
- pasarguard_accounts.display_name, service_name
- unique payments.order_id
SQLite + Postgres safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDER_STATUSES = (
    "PENDING_PAYMENT",
    "PAYMENT_REVIEW",
    "PAYMENT_APPROVED",
    "PROVISIONING",
    "ACTIVE",
    "REJECTED",
    "PROVISION_FAILED",
    "EXPIRED",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "users",
        sa.Column("wallet_balance_toman", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "wallet_ledger_entries",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "entry_type",
            sa.Enum("credit", "debit", name="wallet_entry_type", create_constraint=True),
            nullable=False,
        ),
        sa.Column("amount_toman", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("balance_after_toman", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wallet_ledger_user_id", "wallet_ledger_entries", ["user_id"])
    op.create_index("ix_wallet_ledger_order_id", "wallet_ledger_entries", ["order_id"])
    op.create_index(
        "ix_wallet_ledger_idempotency_key",
        "wallet_ledger_entries",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_wallet_ledger_created_at", "wallet_ledger_entries", ["created_at"])

    if dialect == "postgresql":
        op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'EXPIRED'")
        op.add_column(
            "orders",
            sa.Column("wallet_applied_toman", sa.BigInteger(), nullable=False, server_default="0"),
        )
        op.add_column("orders", sa.Column("service_name", sa.String(length=64), nullable=True))
        op.add_column("orders", sa.Column("renew_account_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            "fk_orders_renew_account_id",
            "orders",
            "pasarguard_accounts",
            ["renew_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.drop_index("ix_payments_order_id", table_name="payments")
        op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=True)
    else:
        with op.batch_alter_table("orders") as batch:
            batch.alter_column(
                "status",
                existing_type=sa.Enum(name="order_status", native_enum=False),
                type_=sa.Enum(*_ORDER_STATUSES, name="order_status", native_enum=False),
                existing_nullable=False,
                server_default="PENDING_PAYMENT",
            )
            batch.add_column(
                sa.Column(
                    "wallet_applied_toman",
                    sa.BigInteger(),
                    nullable=False,
                    server_default="0",
                )
            )
            batch.add_column(sa.Column("service_name", sa.String(length=64), nullable=True))
            batch.add_column(sa.Column("renew_account_id", sa.BigInteger(), nullable=True))
            batch.create_foreign_key(
                "fk_orders_renew_account_id",
                "pasarguard_accounts",
                ["renew_account_id"],
                ["id"],
                ondelete="SET NULL",
            )
        with op.batch_alter_table("payments") as batch:
            batch.drop_index("ix_payments_order_id")
            batch.create_index("ix_payments_order_id", ["order_id"], unique=True)

    op.add_column(
        "pasarguard_accounts",
        sa.Column("display_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "pasarguard_accounts",
        sa.Column("service_name", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_column("pasarguard_accounts", "service_name")
    op.drop_column("pasarguard_accounts", "display_name")

    if dialect == "postgresql":
        op.drop_constraint("fk_orders_renew_account_id", "orders", type_="foreignkey")
        op.drop_column("orders", "renew_account_id")
        op.drop_column("orders", "service_name")
        op.drop_column("orders", "wallet_applied_toman")
        op.drop_index("ix_payments_order_id", table_name="payments")
        op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=False)
    else:
        with op.batch_alter_table("payments") as batch:
            batch.drop_index("ix_payments_order_id")
            batch.create_index("ix_payments_order_id", ["order_id"], unique=False)
        with op.batch_alter_table("orders") as batch:
            batch.drop_constraint("fk_orders_renew_account_id", type_="foreignkey")
            batch.drop_column("renew_account_id")
            batch.drop_column("service_name")
            batch.drop_column("wallet_applied_toman")
            batch.alter_column(
                "status",
                existing_type=sa.Enum(name="order_status", native_enum=False),
                type_=sa.Enum(
                    "PENDING_PAYMENT",
                    "PAYMENT_REVIEW",
                    "PAYMENT_APPROVED",
                    "PROVISIONING",
                    "ACTIVE",
                    "REJECTED",
                    "PROVISION_FAILED",
                    name="order_status",
                    native_enum=False,
                ),
                existing_nullable=False,
                server_default="PENDING_PAYMENT",
            )

    op.drop_index("ix_wallet_ledger_created_at", table_name="wallet_ledger_entries")
    op.drop_index("ix_wallet_ledger_idempotency_key", table_name="wallet_ledger_entries")
    op.drop_index("ix_wallet_ledger_order_id", table_name="wallet_ledger_entries")
    op.drop_index("ix_wallet_ledger_user_id", table_name="wallet_ledger_entries")
    op.drop_table("wallet_ledger_entries")
    if dialect == "postgresql":
        op.execute("DROP TYPE IF EXISTS wallet_entry_type")
    op.drop_column("users", "wallet_balance_toman")
