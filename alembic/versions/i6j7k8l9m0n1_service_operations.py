"""Service operations: durable targets for manual extend / add-data

Revision ID: i6j7k8l9m0n1
Revises: h5i6j7k8l9m0
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i6j7k8l9m0n1"
down_revision: str | None = "h5i6j7k8l9m0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_expire_unix", sa.BigInteger(), nullable=True),
        sa.Column("target_data_limit", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["pasarguard_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_operations_account_id",
        "service_operations",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "uq_service_operations_pending_account_kind",
        "service_operations",
        ["account_id", "kind"],
        unique=True,
        sqlite_where=sa.text("state = 'pending'"),
        postgresql_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_service_operations_pending_account_kind",
        table_name="service_operations",
    )
    op.drop_index("ix_service_operations_account_id", table_name="service_operations")
    op.drop_table("service_operations")
